# 🚀 Hospedando o lzt_monitor no GitHub Actions

O monitor roda 24/7 na nuvem do GitHub **sem precisar deixar seu PC ligado**.
O GitHub Actions executa o script em servidores do GitHub e o estado
(`lzt_state.json`) é salvo de volta no repositório a cada execução, então o
monitor continua de onde parou.

---

## Estrutura de arquivos (já pronta)

```
lzt_monitor/
├── lzt_monitor.py              # o programa (v1.1.0, compatível com Actions)
├── requirements.txt            # dependências (requests, pycryptodome)
├── lzt_state.json              # estado atual — VAI para o repositório (é assim
│                               #   que o progresso persiste entre execuções)
├── .gitignore                  # ignora logs e __pycache__
└── .github/
    └── workflows/
        └── lzt-monitor.yml     # definição do job no GitHub Actions
```

> O `lzt_state.json` **deve** ser versionado no git. Ele já está sanitizado
> (a URL do webhook foi removida — o webhook entra pelo secret, ver abaixo).

---

## Passo a passo

### 1. Crie o repositório no GitHub

1. Acesse https://github.com/new
2. Nome: `lzt-monitor` (ou outro)
3. Recomendado: **Private** (o estado contém IDs de itens do seu monitor)
4. **Não** marque "Add a README" nem crie `.gitignore` — vamos enviar os nossos.
5. Clique em **Create repository**

### 2. Envie os arquivos

No terminal do Zorin, dentro da pasta `~/lzt_monitor`:

```bash
cd ~/lzt_monitor

git init
git add .
git commit -m "feat: lzt_monitor pronto para GitHub Actions"

# use a URL que o GitHub mostrar na tela do repositório:
git remote add origin https://github.com/SEU_USUARIO/lzt-monitor.git
git branch -M main
git push -u origin main
```

Se pedir usuário/senha: use seu **usuário** e um **Personal Access Token**
(GitHub → Settings → Developer settings → Personal access tokens → Generate
new token, marque a permissão `repo`) em vez da senha.

### 3. Adicione o webhook como secret

O webhook não vai no código — vai como variável secreta do GitHub:

1. No repositório: **Settings → Secrets and variables → Actions**
2. Clique em **New repository secret**
3. Name: `LZT_WEBHOOK_URL`
4. Secret: `https://discord.com/api/webhooks/1534602687637881002/2c9VUQQzxxjv168KsNQUzmT6ZHibXkERFp1xjmhJWe8kKsDaX3oNroYw_MjjzlhMbIUJ`
5. **Add secret**

> Se preferir, crie um webhook novo no Discord (Configurações do servidor →
> Integrações → Webhooks → Novo) e use a URL nova.

### 4. Rode manualmente a primeira vez

1. No repositório: aba **Actions**
2. Selecione o workflow **lzt-monitor**
3. Clique em **Run workflow** → **Run workflow** (verde)

O job vai rodar e você verá o log ao vivo clicando no job.

### 5. Confira o resultado

- O canal do Discord recebe as mensagens dos itens monitorados (backfill)
- Ao terminar, o workflow faz commit do `lzt_state.json` atualizado de volta
  ao repositório (veja a aba **Actions** → job → passo "Committar estado")

---

## Agenda automática

O workflow já vem agendado para rodar **a cada 6 horas** (arquivo
`.github/workflows/lzt-monitor.yml`, linha `cron: "0 */6 * * *"`).

Cada execução roda o monitor por até **5h40** (`--max-runtime 340`), então na
prática ele fica monitorando quase o dia todo. Para ajustar:

| Quero rodar... | cron |
|---|---|
| A cada 6 horas (padrão) | `0 */6 * * *` |
| 2x por dia | `0 */12 * * *` |
| 1x por dia (meia-noite UTC) | `0 0 * * *` |
| A cada hora | `0 * * * *` |

> Regra importante: `--max-runtime` (minutos) deve ser menor que
> `timeout-minutes` (355) para sobrar tempo de commitar o estado no fim.
> Horários do cron são em **UTC** (3h a mais que o horário de Brasília no
> verão, 3h no inverno → na prática Brasília = UTC−3).

---

## Como funciona a persistência

1. O Actions baixa o repositório (com o `lzt_state.json` da última vez)
2. Roda o monitor; a cada ciclo ele salva o estado no arquivo local
3. No fim, o workflow faz `git commit` + `git push` do estado atualizado
4. A próxima execução começa exatamente de onde parou

Se quiser **recomeçar do zero** (novo baseline, sem reenviar nada): apague o
`lzt_state.json` no repositório (ou rode com `--reset` localmente e envie o
arquivo vazio).

---

## Testar antes de subir (opcional)

No Zorin, localmente:

```bash
cd ~/lzt_monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# envia, edita e apaga uma mensagem de teste no Discord:
python3 lzt_monitor.py --test-webhook --webhook "SUA_URL_AQUI"

# um ciclo único (não envia nada — itens já são conhecidos):
python3 lzt_monitor.py --once --webhook "SUA_URL_AQUI"
```

---

## Ajustes úteis

- **Intervalo entre ciclos**: padrão 20s. No Actions, `pip` instala tudo e o
  job roda em rede do GitHub — se o lzt.market bloquear o IP do datacenter,
  aumente para 30–60s no workflow: `python3 lzt_monitor.py --max-runtime 340 --interval 45`
- **Logs**: aparecem na aba Actions → job → "Executar o monitor" (não vão
  para o repositório; o `.gitignore` cuida disso)
- **Rodar sem notificar** (teste): remova o secret ou use `--no-webhook`

---

## Resumo rápido

1. Suba a pasta `~/lzt_monitor` para um repositório no GitHub
2. Crie o secret `LZT_WEBHOOK_URL` com a URL do webhook
3. Aba Actions → **Run workflow** → pronto, está no ar 24/7

