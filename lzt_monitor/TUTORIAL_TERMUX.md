# Monitor Lolzteam Market — Tutorial Termux (Android)

Rode o monitor do [lzt.market](https://lzt.market) no **celular Android**
(Termux), **sem API oficial** (sem login/token), com avisos no Discord via
webhook. O celular precisa ficar com o app aberto (ou com o wake-lock ativo)
enquanto o monitor roda.

---

## 1. Instalar o Termux

1. Baixe o **Termux** do repositório oficial (a versão da Play Store está
   desatualizada):
   - **[F-Droid](https://f-droid.org/packages/com.termux/)** (recomendado) ou
   - **[GitHub](https://github.com/termux/termux-app/releases)** (APK)
2. Abra o app e aguarde o `$` aparecer.

---

## 2. Instalar Python e dependências

```bash
pkg update && pkg upgrade -y
pkg install -y python
pip install requests pycryptodome
```

> Se aparecer aviso sobre ambiente "externally managed", rode:
> `pip install --break-system-packages requests pycryptodome`

---

## 3. Colocar o programa no lugar

Copie o `lzt_monitor.py` para o celular (ex.: via USB, Google Drive, ou
WhatsApp) e mova para o Termux:

```bash
cd ~
mkdir -p lzt_monitor
cp /sdcard/Download/lzt_monitor.py ~/lzt_monitor/
cd ~/lzt_monitor
```

> O Termux acessa os arquivos do Android em `/sdcard/...`. Se o arquivo estiver
> na pasta Downloads: `/sdcard/Download/lzt_monitor.py`.
> Conceda acesso com: `termux-setup-storage` (e aceite a permissão no Android).

---

## 4. Teste rápido (sem webhook)

```bash
python3 lzt_monitor.py --once --no-webhook --verbose
```

Saída esperada:

```
INFO  Desafio DDoS-Guard resolvido (cookie __x atualizado)
INFO  BASELINE definido com 17 itens (nada foi enviado). Próximos ciclos monitoram mudanças.
INFO  Ciclo concluído: 17 itens na busca, 17 monitorados
```

O estado é salvo em `lzt_state.json` (na mesma pasta).

---

## 5. Configurar o webhook do Discord

1. No Discord: **Configurações do servidor** → **Integrações** → **Webhooks**
   → **Novo webhook** → **Copiar URL do webhook**.
2. URL no formato: `https://discord.com/api/webhooks/ID/TOKEN`

Teste o envio:

```bash
cd ~/lzt_monitor
python3 lzt_monitor.py --test-webhook --webhook "https://discord.com/api/webhooks/ID/TOKEN"
```

---

## 6. Rodando o monitor

```bash
cd ~/lzt_monitor
python3 lzt_monitor.py --webhook "https://discord.com/api/webhooks/ID/TOKEN"
```

> Em vez de digitar a URL toda vez, crie um atalho no `~/.bashrc`:
>
> ```bash
> echo 'export LZT_WEBHOOK_URL="https://discord.com/api/webhooks/ID/TOKEN"' >> ~/.bashrc
> source ~/.bashrc
> ```
>
> Depois é só: `python3 lzt_monitor.py`

### Flags úteis

| Flag | Função |
|------|--------|
| `--once` | Executa um único ciclo e encerra |
| `--interval SEG` | Intervalo entre ciclos (padrão 20 s) |
| `--no-webhook` | Nada é enviado ao Discord (teste) |
| `--test-webhook` | Envia mensagem de teste e sai |
| `--reset` | Apaga o estado salvo e redefine a linha de base |
| `--verbose` | Log detalhado |
| `--webhook URL` | URL do webhook (tem precedência sobre a env) |

Encerre com `Ctrl+C` — o estado é salvo com segurança.

---

## 7. Mantendo o monitor rodando com a tela desligada

O Android pausa processos em segundo plano. Use o **wake-lock** do Termux:

```bash
termux-wake-lock        # impede a suspensão (ativa trava de tela)
# ... rode o monitor ...
termux-wake-unlock      # libera quando terminar
```

### 7.1 Rodar em segundo plano com tmux

Com `tmux`, o monitor continua rodando mesmo se você trocar de app:

```bash
pkg install -y tmux
tmux new -s lzt            # cria sessão "lzt"
cd ~/lzt_monitor
python3 lzt_monitor.py --webhook "URL_DO_WEBHOOK"
```

- Sair **sem matar**: `Ctrl+B` e depois `D`
- Voltar: `tmux attach -t lzt`
- Ver sessões ativas: `tmux ls`

> **Dica de bateria:** deixe o celular carregando enquanto o monitor roda.

---

## 8. O que é enviado ao Discord

- 🟢 **Anúncio novo** — card verde com título, preço (com e sem taxa), vendedor,
  link direto e botão "Abrir".
- 🟠 **Queda de preço** — a mensagem é *editada* com o novo preço.
- 🔴 **Vendido** — após o anúncio sumir da busca por vários ciclos, a mensagem
  é *editada* para "VENDIDO".

---

## 9. Solução de problemas

| Sintoma | Causa provável / solução |
|---------|--------------------------|
| `Permission denied` ao copiar | Rode `termux-setup-storage` e aceite a permissão. |
| `Name or service not known` | Sem internet/Wi-Fi ativo. |
| `Falha ao buscar pagina (tentativa N)` | Bloqueio temporário do lzt.market; o monitor tenta de novo nos próximos ciclos. |
| Não recebe avisos com a tela desligada | Falta `termux-wake-lock` ou o Android matou o app — confira as permissões de bateria do Termux (Sem restrições). |
| `pip: externally-managed-environment` | Use `pip install --break-system-packages ...` |

---

## 10. Arquivos gerados

| Arquivo | Função |
|---------|--------|
| `lzt_state.json` | Estado persistente (itens conhecidos, preços, status). Pode ser apagado com `--reset`. |
| `lzt_monitor.log` | Log do programa (gira automaticamente em 1 MB × 3 arquivos). |

---

Feito com 💚 para rodar no Termux. Para PC, veja o tutorial do Linux (Zorin OS).

