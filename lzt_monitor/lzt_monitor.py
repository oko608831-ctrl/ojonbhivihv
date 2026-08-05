#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lzt_monitor.py — Monitor da busca do lzt.market (contas Valorant, Brasil, até R$50, com faca).

Sem API oficial: faz scraping HTTP puro, resolve manualmente o desafio
DDoS-Guard (/_dfjs/b.js + main("key","iv","data")) e notifica via webhook
do Discord (envio, edição de preço/venda, exclusão).

Eventos:
  * NOVA LISTAGEM  -> embed verde, detalhes completos da página do item
  * PREÇO CAIU     -> embed laranja editando a mensagem (antigo -> novo)
  * VENDIDA/REMOVIDA -> embed vermelho editando a mensagem (confirmado 2x)

Primeira execução = baseline silencioso (não envia nada).

Uso:
  python3 lzt_monitor.py --webhook "https://discord.com/api/webhooks/ID/TOKEN"
  python3 lzt_monitor.py --once --no-webhook
  python3 lzt_monitor.py --test-webhook --webhook "URL"
  python3 lzt_monitor.py --reset

Dependências: pip install requests pycryptodome
"""

import argparse
import html
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests
from Crypto.Cipher import AES

VERSION = "1.2.0"

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "lzt_state.json")
LOG_FILE = os.path.join(BASE_DIR, "lzt_monitor.log")

BASE_URL = "https://lzt.market"
SEARCH_URL = BASE_URL + "/riot?pmax=50&country[]=bra&knife=1"

DEFAULT_INTERVAL = 20          # segundos entre ciclos
JITTER = (0.9, 1.1)            # variação aleatória no intervalo
MISSES_BEFORE_SOLD_CHECK = 3   # ciclos sem aparecer antes de verificar a página
SOLD_CONFIRM_REQUIRED = 2      # confirmações consecutivas de 403 para marcar vendida
REQUEST_TIMEOUT = 30
MAX_NET_RETRIES = 3
MAX_CHALLENGES = 2
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.6,en;q=0.4",
}

COLORS = {
    "new": 0x2ECC71,
    "drop": 0xE67E22,
    "sold": 0xE74C3C,
    "test": 0x3498DB,
    "info": 0x95A5A6,
}

CURRENCY_SYMBOLS = {
    "brl": "R$", "rub": "₽", "usd": "US$", "eur": "€",
    "uah": "₴", "kzt": "₸", "byn": "BYN",
}

# Traduções RU -> PT (aplicadas como substituição de substring, maior primeiro)
RU2PT = [
    ("Стоимость инвентаря", "Valor do inventário"),
    ("Текущий ранг", "Rank atual"),
    ("Ранг прошлого сезона", "Rank anterior"),
    ("Последний ранг", "Último rank"),
    ("Последняя активность", "Última atividade"),
    ("Последний актив", "Última atividade"),
    ("Привязка к почте", "E-mail vinculado"),
    ("Доступ к почте", "Acesso ao e-mail"),
    ("Телефон привязан", "Telefone vinculado"),
    ("Почтовый домен", "Domínio do e-mail"),
    ("Уровень", "Nível"),
    ("Ножа", "Facas"),
    ("Скинов", "Skins"),
    ("Скины", "Skins"),
    ("Скин", "Skin"),
    ("скинов", "skins"),
    ("скины", "skins"),
    ("скин", "skin"),
    ("Агентов", "Agentes"),
    ("Агенты", "Agentes"),
    ("Агент", "Agente"),
    ("Привязка", "Vínculo"),
    ("привязка к почте", "e-mail vinculado"),
    ("телефон привязан", "telefone vinculado"),
    ("инвентарь", "inventário"),
    ("ножей", "facas"),
    ("уровень", "nível"),
    ("бразилия", "Brasil"),
    ("бразил", "Brasil"),
    ("Регион", "Região"),
    ("Страна", "País"),
    ("Сегодня", "Hoje"),
    ("Вчера", "Ontem"),
    ("Понедельник", "Segunda-feira"),
    ("Вторник", "Terça-feira"),
    ("Среда", "Quarta-feira"),
    ("Четверг", "Quinta-feira"),
    ("Пятница", "Sexta-feira"),
    ("Суббота", "Sábado"),
    ("Воскресенье", "Domingo"),
    ("Да", "Sim"),
    ("Нет", "Não"),
    ("Бразилия", "Brasil"),
    ("Valorant Points", "VP"),
    ("Radiant Points", "RP"),
    ("Free Agents", "Agentes grátis"),
    (" в ", " às "),
]
RU2PT.sort(key=lambda kv: len(kv[0]), reverse=True)

# ----------------------------------------------------------------------------
# Helpers gerais
# ----------------------------------------------------------------------------
log = logging.getLogger("lzt_monitor")


class LZTError(Exception):
    pass


class NetworkError(LZTError):
    pass


class ChallengeError(LZTError):
    pass


def translate(text):
    """Aplica as traduções RU->PT (substituição de substring, maior primeiro)."""
    if not text:
        return text
    out = text
    for ru, pt in RU2PT:
        out = out.replace(ru, pt)
    return out


def clean_html(s):
    """Remove tags HTML e entidades, normaliza espaços."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")
_CYR_RUN_RE = re.compile(r"[а-яА-ЯёЁ][а-яА-ЯёЁ\s.,'’\-]*")
_TRANS_CACHE = {}
_RUN_CACHE = {}


def translate_to_pt(text):
    """Traduz texto em russo para português (dicionário + API Google gtx).

    Usa primeiro a tabela RU2PT (instantânea e offline); se ainda restar
    cirílico, chama a API de tradução com cache. Nunca levanta exceção:
    em caso de falha, devolve o texto com o que o dicionário traduziu.
    """
    if not text or not CYRILLIC_RE.search(text):
        return text
    cached = _TRANS_CACHE.get(text)
    if cached is not None:
        return cached
    out = translate(text)
    if not CYRILLIC_RE.search(out):
        _TRANS_CACHE[text] = out
        return out
    # Traduz via API somente os trechos que ainda têm cirílico,
    # preservando o que o dicionário já traduziu e as siglas latinas
    # (VP/RP) sem passá-las pelo tradutor (que as corrompe).
    def _run_pt(ru_run):
        cached = _RUN_CACHE.get(ru_run)
        if cached is not None:
            return cached
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "ru", "tl": "pt",
                        "dt": "t", "q": ru_run},
                timeout=12,
                headers={"User-Agent": UA},
            )
            r.raise_for_status()
            parts = r.json()[0]
            cached = "".join(p[0] for p in parts if p and p[0]).strip()
        except Exception:
            cached = ""
        _RUN_CACHE[ru_run] = cached
        return cached
    def _sub_run(m):
        ru_run = m.group(0)
        trail = ru_run[len(ru_run.rstrip()):]
        pt_run = _run_pt(ru_run)
        if pt_run and not CYRILLIC_RE.search(pt_run):
            return pt_run + trail
        return ru_run
    out = _CYR_RUN_RE.sub(_sub_run, out)
    _TRANS_CACHE[text] = out
    return out


def fmt_money(value, symbol="R$"):
    """Formata 32.3 -> 'R$ 32,30' (padrão brasileiro)."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    s = f"{num:,.2f}"
    return f"{symbol} {s}".replace(",", "X").replace(".", ",").replace("X", ".")


def ts_str(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def now_str():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# ----------------------------------------------------------------------------
# Solver do desafio DDoS-Guard (Python puro, sem Node)
# ----------------------------------------------------------------------------
# O desafio vem como uma página minúscula:
#   <script type="text/javascript" src="/_dfjs/b.js"></script>
#   <script>document.addEventListener('DOMContentLoaded',() => {
#     main("KEY", "IV", "DATA");});</script>
# O mapeamento (validado contra cookie real):
#   aes_key = bytes.fromhex(IV), ct = bytes.fromhex(KEY), iv = bytes.fromhex(DATA)
#   para cada bloco de 16: P = AES-ECB_decrypt(ct_i) XOR prev; prev = ct_i
#   se o resultado tiver mais de 16 bytes, remove zeros finais
#   o cookie é o resultado em hex
MAIN_RE = re.compile(
    r'main\(\s*"([0-9a-fA-F]+)"\s*,\s*"([0-9a-fA-F]+)"\s*,\s*"([0-9a-fA-F]+)"\s*\)'
)


def solve_challenge(challenge_html):
    m = MAIN_RE.search(challenge_html)
    if not m:
        raise ChallengeError("padrão main(key, iv, data) não encontrado no desafio")
    key_param, iv_param, data_param = m.groups()
    aes_key = bytes.fromhex(iv_param)
    ct = bytes.fromhex(key_param)
    iv = bytes.fromhex(data_param)
    cipher = AES.new(aes_key, AES.MODE_ECB)
    prev = iv
    out = bytearray()
    for i in range(0, len(ct), 16):
        block = ct[i:i + 16]
        dec = cipher.decrypt(block)
        xored = bytes(a ^ b for a, b in zip(dec, prev))
        out += xored
        prev = block
    result = bytes(out)
    if len(result) > 16:
        result = result.rstrip(b"\x00")
    return result.hex()


def is_challenge(resp):
    try:
        text = resp.text
    except Exception:
        return False
    return resp.status_code == 200 and len(text) < 1500 and "_dfjs/b.js" in text


# ----------------------------------------------------------------------------
# Cliente HTTP
# ----------------------------------------------------------------------------
class LZTClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # O lzt.market exibe os preços na moeda definida pelo cookie
        # xf_market_currency. Sem fixar o cookie, o site pode servir os
        # anúncios em dólar (ou outra moeda) dependendo do visitante;
        # por isso forçamos BRL (reais) desde o início da sessão.
        self.session.cookies.set("xf_market_currency", "brl",
                                 domain="lzt.market", path="/")
        self._currency_fixed = False

    def _fix_currency_brl(self):
        """Reforço: confirma a preferência BRL via endpoint oficial do site."""
        if self._currency_fixed:
            return
        self._currency_fixed = True
        try:
            r = self.session.post(f"{BASE_URL}/user/0/currency",
                                  data={"currency": "brl"},
                                  timeout=REQUEST_TIMEOUT,
                                  allow_redirects=False)
            if r.status_code in (200, 301, 302, 303):
                log.info("Moeda da sessão fixada em BRL (reais)")
            else:
                log.warning("Não foi possível confirmar a moeda BRL (HTTP %d)",
                            r.status_code)
        except requests.RequestException as e:
            log.warning("Falha ao confirmar a moeda BRL: %s", e)

    def get(self, url):
        """GET com retry em falha de rede e resolução transparente do desafio."""
        net_fails = 0
        challenges = 0
        while True:
            try:
                r = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                net_fails += 1
                if net_fails >= MAX_NET_RETRIES:
                    raise NetworkError(f"sem resposta após {net_fails} tentativas: {e}")
                delay = 2 ** net_fails
                log.warning("Falha de rede (%s), tentando de novo em %ds", e, delay)
                time.sleep(delay)
                continue
            if is_challenge(r):
                challenges += 1
                if challenges > MAX_CHALLENGES:
                    raise ChallengeError("desafio DDoS-Guard não foi resolvido após "
                                         f"{MAX_CHALLENGES} tentativas")
                cookie = solve_challenge(r.text)
                self.session.cookies.set("__x", cookie, domain="lzt.market")
                log.info("Desafio DDoS-Guard resolvido (cookie __x atualizado)")
                time.sleep(1.0)
                continue
            self._fix_currency_brl()
            return r


# ----------------------------------------------------------------------------
# Parsers
# ----------------------------------------------------------------------------
CARD_START_RE = re.compile(r'<div\s+id="marketItem--')


def parse_search_page(text):
    """Extrai todos os cards da página de busca."""
    starts = [m.start() for m in CARD_START_RE.finditer(text)]
    cards = []
    for idx, s in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(text)
        chunk = text[s:end]
        if 'class="marketItemCard' not in chunk or 'itemTitle' not in chunk:
            continue
        card = {"id": None, "user_id": None, "title": None, "time": None,
                "price": None, "fee_price": None, "currency": "R$",
                "seller": None, "rank": None, "badges": [], "stats": []}

        m = re.search(r'id="marketItem--(\d+)"', chunk)
        if m:
            card["id"] = int(m.group(1))
        m = re.search(r'data-user-id="(\d+)"', chunk)
        if m:
            card["user_id"] = int(m.group(1))
        m = re.search(r'<span class="itemTitle">(.*?)</span>', chunk, re.S)
        if m:
            card["title"] = clean_html(m.group(1))
        m = re.search(r'<span class="itemTime">(.*?)</span>', chunk, re.S)
        if m:
            card["time"] = clean_html(m.group(1))
        m = re.search(r'<span class="Value" data-value="([\d.]+)" '
                      r'data-fee-value="([\d.]+)">', chunk)
        if m:
            card["price"] = float(m.group(1))
            card["fee_price"] = float(m.group(2))
        m = re.search(r'svgIcon--([a-z]{3})', chunk)
        if m:
            card["currency"] = CURRENCY_SYMBOLS.get(
                m.group(1), m.group(1).upper() + " ")
        m = re.search(r'styleUserNickname">(.*?)<', chunk, re.S)
        if not m:
            m = re.search(r'itemSellerAvatar[^>]*alt="([^"]+)"', chunk)
        if m:
            card["seller"] = clean_html(m.group(1))
        card["badges"] = [
            clean_html(b) for b in re.findall(
                r'<div class="marketIndexItem-Badge[^"]*">(.*?)</div>', chunk, re.S)
        ]
        card["stats"] = [
            clean_html(s) for s in re.findall(
                r'<span class="stat[^"]*"[^>]*>(.*?)</span>', chunk, re.S)
        ]
        m = re.search(r'valorantRank--img[^>]*alt="([^"]+)"', chunk)
        if m:
            card["rank"] = m.group(1)
        if card["id"]:
            cards.append(card)
    return cards


def classify_badges(badges):
    """Classifica os badges do card de busca em campos nomeados."""
    out = {}
    extras = []
    for b in badges:
        low = b.lower()
        if ("vp" in low and ("~" in b or "стоимость" in low
                             or "invent" in low or "valor" in low)):
            out.setdefault("inv_value", b)
        elif re.fullmatch(r"[\d\s]+vp", low):
            out.setdefault("vp", b)
        elif "скин" in low:
            out.setdefault("skins", b)
        elif "агент" in low:
            out.setdefault("agents", b)
        elif "уровень" in low:
            out.setdefault("level", b)
        elif "бразил" in low:
            out.setdefault("region", b)
        else:
            extras.append(b)
    if extras:
        out["extras"] = extras
    return out


# Bloco: <div class="counter[attrs]"> ... <div class="muted">NOME</div> </div>
# O \s*</div> final é o fechamento do próprio .counter (não o do .muted).
COUNTER_BLOCK_RE = re.compile(
    r'<div class="counter[^"]*"([^>]*)>(.*?)'
    r'<div class="muted[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.S)
LABEL_RE = re.compile(r'<div class="label[^"]*"[^>]*>(.*?)</div>', re.S)


def parse_item_page(text):
    """Extrai os dados completos da página de um item."""
    d = {}

    m = re.search(r'<span class="title-account">(.*?)</span>', text, re.S)
    if m:
        d["title"] = clean_html(m.group(1))

    m = re.search(r'<span class="value" id="price" data-value="([\d.]+)">', text)
    if m:
        d["price"] = float(m.group(1))
    m = re.search(r'svgIcon--([a-z]{3})', text)
    if m:
        d["currency"] = CURRENCY_SYMBOLS.get(m.group(1), m.group(1).upper() + " ")

    m = re.search(r'class="published_date Tooltip" data-value="(\d+)"', text)
    if m:
        d["published_ts"] = int(m.group(1))
    m = re.search(r'fa-eye"></i>\s*([\d\s]+)', text)
    if m:
        try:
            d["views"] = int(re.sub(r"\s", "", m.group(1)))
        except ValueError:
            pass

    m = re.search(r'styleUserNickname">(.*?)<', text, re.S)
    if m:
        d["seller"] = clean_html(m.group(1))
    m = re.search(r'members/(\d+)/', text)
    if m:
        d["seller_id"] = int(m.group(1))

    # Contadores: <div class="counter"> <div class="label">VALOR</div>
    #              <div class="muted">NOME</div> </div>
    counters = {}
    for cm in COUNTER_BLOCK_RE.finditer(text):
        attrs = cm.group(1)
        region = cm.group(2)          # parte entre .counter e .muted (contém o .label)
        lm = LABEL_RE.search(region)
        value = clean_html(lm.group(1)) if lm else None
        if not value:
            tm = re.search(r'title="([^"]*)"', attrs)
            if tm:
                value = clean_html(tm.group(1))
        name = clean_html(cm.group(3))  # texto do .muted
        if not name or not value:
            continue
        name = translate_to_pt(name)
        value = translate_to_pt(value)
        if name not in counters:
            counters[name] = value
    d["counters"] = counters

    d["sections"] = []
    for hm in re.finditer(r"<h4[^>]*>(.*?)</h4>", text, re.S):
        t = clean_html(hm.group(1))
        if t:
            d["sections"].append(t)

    d["inv_items"] = []
    for bm in re.finditer(r'<div class="([^"]*\bbold\b[^"]*)">(.*?)</div>',
                          text, re.S):
        cls, content = bm.group(1), bm.group(2)
        if "title" in cls:
            continue  # ex.: "Критерий сортировки"
        t = clean_html(content)
        if t:
            d["inv_items"].append(t)

    m = re.search(r'<article class="mn-15-0-0 itemDescription messageText">'
                  r'(.*?)</article>', text, re.S)
    if m:
        d["description"] = clean_html(m.group(1))

    return d


def item_status(client, item_id):
    """Verifica se um item ainda existe. 'sold' | 'active' | 'unknown'."""
    r = client.get(f"{BASE_URL}/{item_id}/")
    if r.status_code == 403 and ("Ошибка" in r.text):
        return "sold"
    if (r.status_code == 200 and 'id="price"' in r.text
            and "title-account" in r.text):
        return "active"
    return "unknown"


# ----------------------------------------------------------------------------
# Webhook Discord
# ----------------------------------------------------------------------------
class Webhook:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def send(self, embed, wait=True):
        r = requests.post(self.url + "?wait=true", json={"embeds": [embed]},
                          timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            raise LZTError(f"webhook POST falhou ({r.status_code}): {r.text[:200]}")
        try:
            return r.json().get("id")
        except ValueError:
            return None

    def patch(self, msg_id, embed):
        r = requests.patch(f"{self.url}/messages/{msg_id}",
                           json={"embeds": [embed]}, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            raise LZTError(f"webhook PATCH falhou ({r.status_code}): {r.text[:200]}")
        return True

    def delete(self, msg_id):
        r = requests.delete(f"{self.url}/messages/{msg_id}",
                            timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            raise LZTError(f"webhook DELETE falhou ({r.status_code}): {r.text[:200]}")
        return True


def _footer(item_id):
    return f"ID {item_id} · {now_str()}"


def _add_field(fields, name, value, inline=True):
    if value is None or value == "":
        return
    fields.append({"name": name, "value": str(value)[:1000], "inline": inline})


def build_new_embed(card, detail, entry):
    """Embed verde de nova listagem (detalhe do card + página do item)."""
    sym = card.get("currency") or "R$"
    title = f"🆕 Nova listagem · {fmt_money(card.get('price'), sym)}"
    fields = []
    _add_field(fields, "Título", translate_to_pt(card.get("title")))
    _add_field(fields, "Preço", fmt_money(card.get("price"), sym))
    _add_field(fields, "Com taxa", fmt_money(card.get("fee_price"), sym))
    _add_field(fields, "Moeda", sym)
    _add_field(fields, "Vendedor", card.get("seller"))
    if detail:
        _add_field(fields, "Publicado",
                   ts_str(detail.get("published_ts")) or card.get("time"))
        if detail.get("views") is not None:
            _add_field(fields, "Visualizações", detail.get("views"))
    else:
        _add_field(fields, "Publicado", card.get("time"))

    badges = classify_badges(card.get("badges") or [])
    if card.get("rank"):
        _add_field(fields, "Rank", translate_to_pt(card["rank"]))
    if badges.get("level"):
        lvl = badges["level"].replace("Уровень", "").strip()
        _add_field(fields, "Nível", translate_to_pt(lvl))
    if badges.get("region"):
        _add_field(fields, "Região", translate_to_pt(badges["region"]))
    if badges.get("inv_value"):
        inv = translate_to_pt(badges["inv_value"])
        for _p in ("Valor do inventário", "Стоимость инвентаря",
                   "Inventory value"):
            if inv.startswith(_p):
                inv = inv[len(_p):].lstrip(" ~:-–—|").strip()
                break
        _add_field(fields, "Valor do inventário", inv)
    if badges.get("vp"):
        _add_field(fields, "VP", translate_to_pt(badges["vp"]))
    if badges.get("skins"):
        _add_field(fields, "Skins", translate_to_pt(badges["skins"]))
    if badges.get("agents"):
        _add_field(fields, "Agentes", translate_to_pt(badges["agents"]))

    if detail:
        c = detail.get("counters") or {}
        for name, value in c.items():
            _add_field(fields, translate_to_pt(name),
                       translate_to_pt(value))
        desc = translate_to_pt((detail.get("description") or "")[:1000])
        if len(desc) > 1000:
            desc = desc[:997] + "..."
    else:
        desc = ""

    embed = {
        "title": title[:256],
        "color": COLORS["new"],
        "url": entry["url"],
        "footer": {"text": _footer(card.get("id"))},
        "fields": fields[:24],
    }
    if desc:
        embed["description"] = desc
    if entry.get("detail_gone"):
        embed.setdefault("fields", []).insert(
            0, {"name": "Status", "value": "Página indisponível no momento "
                 "(possível venda rápida)", "inline": False})
    return embed


def build_drop_embed(entry, old_price, old_fee, new_price, new_fee):
    sym = entry.get("currency") or "R$"
    fields = []
    _add_field(fields, "Título", translate_to_pt(entry.get("title")))
    _add_field(fields, "Preço antigo", fmt_money(old_price, sym))
    _add_field(fields, "Preço novo", fmt_money(new_price, sym))
    _add_field(fields, "Com taxa (antiga)", fmt_money(old_fee, sym))
    _add_field(fields, "Com taxa (nova)", fmt_money(new_fee, sym))
    _add_field(fields, "Link", entry.get("url"), inline=False)
    return {
        "title": f"💸 Preço caiu · {fmt_money(new_price, sym)}",
        "color": COLORS["drop"],
        "url": entry.get("url"),
        "footer": {"text": _footer(entry.get("id"))},
        "fields": fields[:24],
    }


def build_sold_embed(entry):
    sym = entry.get("currency") or "R$"
    fields = []
    _add_field(fields, "Título", translate_to_pt(entry.get("title")))
    _add_field(fields, "Preço", fmt_money(entry.get("price"), sym))
    _add_field(fields, "Vendedor", entry.get("seller"))
    _add_field(fields, "Status", "Vendida ou removida do marketplace")
    _add_field(fields, "Link", entry.get("url"), inline=False)
    _titulo = translate_to_pt(entry.get("title") or "")
    return {
        "title": f"❌ Vendida/Removida · {_titulo[:100]}",
        "color": COLORS["sold"],
        "url": entry.get("url"),
        "footer": {"text": _footer(entry.get("id"))},
        "fields": fields[:24],
    }


# ----------------------------------------------------------------------------
# Estado persistente
# ----------------------------------------------------------------------------
def default_state():
    return {"items": {}, "baseline_done": False, "webhook_url": None,
            "baseline_at": None, "created_at": time.time()}


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Estado corrompido (%s) — recomeçando do zero", e)
        os.replace(STATE_FILE, STATE_FILE + ".corrupt")
        return default_state()
    if "items" not in state:
        state["items"] = {}
    state.setdefault("baseline_done", False)
    state.setdefault("webhook_url", None)
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------------
class Monitor:
    def __init__(self, client, state, webhook, webhook_enabled):
        self.client = client
        self.state = state
        self.webhook = webhook
        self.webhook_enabled = webhook_enabled
        self.items = state["items"]

    # -- utilidades ----------------------------------------------------------
    def _new_entry(self, card, now):
        return {
            "id": card["id"],
            "url": f"{BASE_URL}/{card['id']}/",
            "title": card.get("title"),
            "price": card.get("price"),
            "fee_price": card.get("fee_price"),
            "currency": card.get("currency", "R$"),
            "seller": card.get("seller"),
            "rank": card.get("rank"),
            "badges": card.get("badges") or [],
            "stats": card.get("stats") or [],
            "card_time": card.get("time"),
            "detail": None,
            "detail_gone": False,
            "msg_id": None,
            "first_seen": now,
            "last_seen": now,
            "misses": 0,
            "sold_checks": 0,
            "status": "active",
            "sold_at": None,
        }

    def _fetch_detail(self, item_id, url):
        r = self.client.get(url)
        if r.status_code == 403 and "Ошибка" in r.text:
            return {"gone": True}
        return parse_item_page(r.text)

    # -- eventos -------------------------------------------------------------
    def handle_new(self, card, now):
        cid = str(card["id"])
        entry = self._new_entry(card, now)
        self.items[cid] = entry
        if not self.webhook_enabled:
            log.info("NOVO item %s: %s (sem webhook)", cid, card.get("title"))
            return

        detail = None
        try:
            detail = self._fetch_detail(card["id"], entry["url"])
        except (NetworkError, ChallengeError) as e:
            log.warning("Falha ao buscar detalhes do item %s: %s", cid, e)
        except Exception as e:
            log.warning("Erro inesperado ao buscar detalhes do item %s: %s",
                        cid, e)
        entry["detail"] = detail if detail and not detail.get("gone") else None
        entry["detail_gone"] = bool(detail and detail.get("gone"))

        try:
            embed = build_new_embed(card, entry["detail"], entry)
            msg_id = self.webhook.send(embed)
            entry["msg_id"] = msg_id
            log.info("NOVO item %s enviado ao Discord (msg %s): %s",
                     cid, msg_id, card.get("title"))
        except Exception as e:
            log.error("Falha ao enviar novo item %s ao Discord: %s", cid, e)

    def update_existing(self, card, now):
        cid = str(card["id"])
        e = self.items[cid]
        if e["status"] == "sold":
            log.info("Item %s reapareceu na busca — revertendo para ativo", cid)
            e["status"] = "active"
            e["sold_at"] = None
            e["misses"] = 0
            e["sold_checks"] = 0

        old_price = e.get("price")
        old_fee = e.get("fee_price")
        new_price = card.get("price")
        new_fee = card.get("fee_price")

        if (old_price is not None and new_price is not None
                and new_price < old_price):
            log.info("DROP %s: preço %s -> %s", cid, old_price, new_price)
            if self.webhook_enabled:
                try:
                    embed = build_drop_embed(e, old_price, old_fee,
                                             new_price, new_fee)
                    if e.get("msg_id"):
                        self.webhook.patch(e["msg_id"], embed)
                        log.info("Mensagem do item %s atualizada (preço caiu)",
                                 cid)
                    else:
                        msg_id = self.webhook.send(embed)
                        e["msg_id"] = msg_id
                        log.info("DROP %s publicado no Discord (msg %s)",
                                 cid, msg_id)
                except Exception as ex:
                    log.error("Falha ao notificar drop do item %s: %s", cid, ex)
        elif (old_price is not None and new_price is not None
                and new_price > old_price):
            log.info("Item %s: preço subiu %s -> %s", cid, old_price, new_price)

        e["price"] = new_price
        e["fee_price"] = new_fee
        e["currency"] = card.get("currency", e.get("currency", "R$"))
        e["seller"] = card.get("seller") or e.get("seller")
        e["rank"] = card.get("rank") or e.get("rank")
        if card.get("badges"):
            e["badges"] = card["badges"]
        if card.get("stats"):
            e["stats"] = card["stats"]
        if card.get("title") and card["title"] != e.get("title"):
            log.info("Item %s: título alterado para %r", cid, card["title"])
        e["title"] = card.get("title") or e.get("title")
        e["card_time"] = card.get("time") or e.get("card_time")
        e["last_seen"] = now

    def handle_missing(self, cid, entry, now):
        entry["misses"] = entry.get("misses", 0) + 1
        if entry.get("status") != "active":
            return
        if entry["misses"] < MISSES_BEFORE_SOLD_CHECK:
            return
        try:
            status = item_status(self.client, int(cid))
        except (NetworkError, ChallengeError) as e:
            log.warning("Falha de rede ao verificar item %s — não marcando "
                        "como vendido: %s", cid, e)
            return
        except Exception as e:
            log.warning("Erro inesperado ao verificar item %s: %s", cid, e)
            return
        if status == "sold":
            entry["sold_checks"] = entry.get("sold_checks", 0) + 1
            if entry["sold_checks"] >= SOLD_CONFIRM_REQUIRED:
                entry["status"] = "sold"
                entry["sold_at"] = now
                entry["misses"] = 0
                entry["sold_checks"] = 0
                log.info("Item %s VENDIDO/REMOVIDO (confirmado %dx): %s",
                         cid, SOLD_CONFIRM_REQUIRED, entry.get("title"))
                if self.webhook_enabled:
                    try:
                        embed = build_sold_embed(entry)
                        if entry.get("msg_id"):
                            self.webhook.patch(entry["msg_id"], embed)
                            log.info("Mensagem do item %s atualizada "
                                     "(vendida)", cid)
                        else:
                            msg_id = self.webhook.send(embed)
                            entry["msg_id"] = msg_id
                            log.info("Item %s vendido publicado no Discord "
                                     "(msg %s)", cid, msg_id)
                    except Exception as e:
                        log.error("Falha ao notificar venda do item %s: %s",
                                  cid, e)
            else:
                log.info("Item %s não está na busca (%dª confirmação de 403)",
                         cid, entry["sold_checks"])
        elif status == "active":
            entry["misses"] = 0
            entry["sold_checks"] = 0
            log.info("Item %s sumiu da busca mas ainda está ativo "
                     "(transitório)", cid)

    def backfill_messages(self):
        """Publica no Discord uma mensagem para cada item monitorado que
        ainda não tem msg_id (ex.: baseline criado sem webhook ativo).
        Depois disso, quedas de preço e vendas passam a atualizar a
        mensagem existente."""
        if not self.webhook_enabled:
            return
        pendentes = [e for e in self.items.values()
                     if not e.get("msg_id") and e.get("status") != "sold"]
        if not pendentes:
            return
        log.info("Backfill: publicando %d item(ns) sem mensagem no Discord...",
                 len(pendentes))
        for entry in pendentes:
            cid = entry.get("id")
            card = {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "price": entry.get("price"),
                "fee_price": entry.get("fee_price"),
                "currency": entry.get("currency"),
                "seller": entry.get("seller"),
                "rank": entry.get("rank"),
                "badges": entry.get("badges") or [],
                "stats": entry.get("stats") or [],
                "time": entry.get("card_time"),
            }
            try:
                embed = build_new_embed(card, entry.get("detail"), entry)
                msg_id = self.webhook.send(embed)
                entry["msg_id"] = msg_id
                log.info("Backfill: item %s publicado (msg %s): %s",
                         cid, msg_id, entry.get("title"))
            except Exception as e:
                log.error("Backfill falhou para o item %s: %s", cid, e)
        save_state(self.state)

    # -- ciclo ---------------------------------------------------------------
    def run_once(self):
        r = self.client.get(SEARCH_URL)
        if r.status_code != 200:
            log.warning("Resposta inesperada da busca: HTTP %d", r.status_code)
            return
        cards = parse_search_page(r.text)
        if not cards:
            log.error("0 cards encontrados na busca — possível bloqueio ou "
                      "mudança de layout")
            return
        now = time.time()

        if not self.state.get("baseline_done"):
            for c in cards:
                self.items[str(c["id"])] = self._new_entry(c, now)
            self.state["baseline_done"] = True
            self.state["baseline_at"] = now
            save_state(self.state)
            log.info("BASELINE definido com %d itens (nada foi enviado). "
                     "Próximos ciclos monitoram mudanças.", len(cards))
            return

        current_ids = set(str(c["id"]) for c in cards)
        for c in cards:
            cid = str(c["id"])
            if cid not in self.items:
                self.handle_new(c, now)
            else:
                self.update_existing(c, now)

        for cid, entry in list(self.items.items()):
            if cid not in current_ids:
                self.handle_missing(cid, entry, now)

        save_state(self.state)
        log.info("Ciclo concluído: %d itens na busca, %d monitorados",
                 len(cards), len(self.items))


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def setup_logging(verbose):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(ch)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Monitor da busca do lzt.market (Valorant BR ≤ R$50, faca) "
                    "com notificação via webhook Discord")
    p.add_argument("--once", action="store_true",
                   help="executa um único ciclo e encerra")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help=f"intervalo entre ciclos em segundos "
                        f"(padrão: {DEFAULT_INTERVAL})")
    p.add_argument("--no-webhook", action="store_true",
                   help="não envia nada ao Discord (útil para testes)")
    p.add_argument("--webhook", metavar="URL",
                   help="URL do webhook do Discord (ou variável "
                        "LZT_WEBHOOK_URL)")
    p.add_argument("--test-webhook", action="store_true",
                   help="envia, edita e apaga uma mensagem de teste e encerra")
    p.add_argument("--reset", action="store_true",
                   help="apaga o estado salvo (novo baseline na próxima execução)")
    p.add_argument("--max-runtime", type=float, default=0,
                   help="encerra após N minutos (0 = sem limite; "
                        "útil para GitHub Actions)")
    p.add_argument("--verbose", action="store_true", help="log detalhado")
    return p.parse_args(argv)


def test_webhook(webhook):
    log.info("Testando webhook: envio -> edição -> exclusão")
    test_embed = {
        "title": "🧪 Teste do lzt_monitor",
        "description": "Envio funcionando. Editando em instantes...",
        "color": COLORS["test"],
        "footer": {"text": f"lzt_monitor v{VERSION} · {now_str()}"},
    }
    msg_id = webhook.send(test_embed)
    log.info("Envio OK (msg %s)", msg_id)
    time.sleep(1.5)
    edited = {
        "title": "✅ Teste do lzt_monitor",
        "description": "Edição funcionando. Excluindo em instantes...",
        "color": COLORS["test"],
        "footer": {"text": f"lzt_monitor v{VERSION} · {now_str()}"},
    }
    webhook.patch(msg_id, edited)
    log.info("Edição OK")
    time.sleep(1.5)
    webhook.delete(msg_id)
    log.info("Exclusão OK")
    print("Webhook OK: envio, edição e exclusão funcionam.")


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.verbose)

    if args.reset:
        if os.path.exists(STATE_FILE):
            os.replace(STATE_FILE, STATE_FILE + ".old")
            log.info("Estado anterior movido para lzt_state.json.old")
        print("Estado resetado. A próxima execução fará um novo baseline.")

    state = load_state()
    webhook_url = (args.webhook
                   or os.environ.get("LZT_WEBHOOK_URL")
                   or state.get("webhook_url"))
    webhook_enabled = not args.no_webhook and bool(webhook_url)

    if args.webhook and not args.no_webhook:
        state["webhook_url"] = args.webhook

    if args.test_webhook:
        if not webhook_url:
            print("ERRO: --test-webhook precisa de uma URL "
                  "(--webhook URL ou LZT_WEBHOOK_URL)")
            return 2
        try:
            test_webhook(Webhook(webhook_url))
        except Exception as e:
            log.error("Teste do webhook falhou: %s", e)
            return 1
        return 0

    if webhook_enabled and not webhook_url:
        log.error("Webhook não configurado. Use --webhook URL, a variável "
                  "LZT_WEBHOOK_URL ou remova --no-webhook.")
        return 2
    if not webhook_enabled:
        log.info("Modo sem webhook (--no-webhook ou URL ausente). "
                 "Nada será enviado ao Discord.")
    if args.webhook or os.environ.get("LZT_WEBHOOK_URL"):
        save_state(state)

    client = LZTClient()
    monitor = Monitor(client, state,
                      Webhook(webhook_url) if webhook_url else None,
                      webhook_enabled)
    monitor.backfill_messages()

    if args.once:
        try:
            monitor.run_once()
        except (NetworkError, ChallengeError) as e:
            log.error("Falha no ciclo único: %s", e)
            return 1
        return 0

    stop = {"flag": False}

    def _stop(signum, frame):
        log.info("Sinal recebido — encerrando com graça")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("lzt_monitor v%s iniciado. Busca: %s", VERSION, SEARCH_URL)
    log.info("Intervalo: %.1fs (±%.0f%%). Webhook: %s",
             args.interval, (1 - JITTER[0]) * 100,
             "SIM" if webhook_enabled else "NÃO")
    start_time = time.time()
    if args.max_runtime:
        log.info("Limite de execução: %.1f minutos", args.max_runtime)

    while not stop["flag"]:
        if (args.max_runtime
                and time.time() - start_time >= args.max_runtime * 60):
            log.info("Tempo máximo (%.1f min) atingido — encerrando",
                     args.max_runtime)
            break
        try:
            monitor.run_once()
        except (NetworkError, ChallengeError) as e:
            log.error("Falha no ciclo (repetindo em %ds): %s",
                      args.interval, e)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Erro inesperado no ciclo")
        finally:
            save_state(state)
        if stop["flag"]:
            break
        delay = args.interval * random.uniform(*JITTER)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            break

    save_state(state)
    log.info("Encerrado. Estado salvo em %s", STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

