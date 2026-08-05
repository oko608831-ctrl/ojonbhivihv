# Monitor Lolzteam Market — Tutorial Linux (Zorin OS)

Monitor de anúncios do [lzt.market](https://lzt.market) na categoria **Riot**
(`/riot?pmax=50&country[]=bra&knife=1`) **sem usar a API oficial** (sem login,
sem token). Detecta anúncios **novos**, **queda de preço** e **itens vendidos**
e envia avisos para o seu Discord via webhook.

---

## 1. Pré-requisitos

Abra o **Terminal** (no Zorin: menu de aplicativos → "Terminal", ou `Ctrl+Alt+T`).

```bash
# Verifica se o Python 3 está instalado
python3 --version

# Se aparecer "python3: comando não encontrado", instale:
sudo apt update
sudo apt install -y python3 python3-pip python3-venv

# Instala as dependências do monitor
pip3 install --break-system-packages requests pycryptodome
```

> No Zorin (base Ubuntu 22.04+), o `pip3` pode exigir a flag
> `--break-system-packages` por causa do PEP 668. Se preferir um ambiente
> isolado (recomendado), use:
>
> ```bash
> python3 -m venv ~/lzt-venv
> source ~/lzt-venv/bin/activate
> pip install requests pycryptodome
> ```

---

## 2. Colocando o programa no lugar

```bash
mkdir -p ~/lzt_monitor
# Copie o arquivo lzt_monitor.py para essa pasta
cp lzt_monitor.py ~/lzt_monitor/
cd ~/lzt_monitor
```

---

## 3. Teste rápido (sem webhook)

O primeiro ciclo **define a linha de base** (lista os anúncios atuais e NÃO
envia nada). Os ciclos seguintes monitoram mudanças.

```bash
python3 lzt_monitor.py --once --no-webhook --verbose
```

O que você deve ver:

```
INFO  Desafio DDoS-Guard resolvido (cookie __x atualizado)
INFO  BASELINE definido com 17 itens (nada foi enviado). Próximos ciclos monitoram mudanças.
INFO  Ciclo concluído: 17 itens na busca, 17 monitorados
```

O estado é salvo em `lzt_state.json` (na mesma pasta).

---

## 4. Configurando o webhook do Discord

1. No Discord, abra o servidor → **Configurações do servidor** →
   **Integrações** → **Webhooks** → **Novo webhook**.
2. Dê um nome (ex.: `LZT Monitor`), escolha o canal e **Copiar URL do webhook**.
3. A URL tem o formato:
   `https://discord.com/api/webhooks/ID/TOKEN`

Teste o envio:

```bash
cd ~/lzt_monitor
python3 lzt_monitor.py --test-webhook --webhook "https://discord.com/api/webhooks/ID/TOKEN"
```

Você deve receber uma mensagem de teste no canal.

---

## 5. Rodando o monitor

### 5.1 Manualmente (com webhook)

```bash
cd ~/lzt_monitor
python3 lzt_monitor.py --webhook "https://discord.com/api/webhooks/ID/TOKEN"
```

Padrões: verifica a cada **20 segundos** (com variação aleatória de ±10% para
não parecer robô). Para mudar o intervalo:

```bash
python3 lzt_monitor.py --webhook "URL_DO_WEBHOOK" --interval 30
```

> Dica: em vez de colocar a URL toda vez, exporte a variável de ambiente:
>
> ```bash
> echo 'export LZT_WEBHOOK_URL="https://discord.com/api/webhooks/ID/TOKEN"' >> ~/.bashrc
> source ~/.bashrc
> ```
>
> A partir daí basta `python3 lzt_monitor.py`.

### 5.2 Flags úteis

| Flag | Função |
|------|--------|
| `--once` | Executa um único ciclo e encerra |
| `--interval SEG` | Intervalo entre ciclos (padrão 20 s) |
| `--no-webhook` | Nada é enviado ao Discord (teste) |
| `--test-webhook` | Envia mensagem de teste e sai |
| `--reset` | Apaga o estado salvo e redefine a linha de base |
| `--verbose` | Log detalhado (mais informação no terminal) |
| `--webhook URL` | URL do webhook (tem precedência sobre a env) |

Encerre com `Ctrl+C` — o estado é salvo com segurança.

---

## 6. Rodando 24/7 (systemd — recomendado)

Para o monitor sobreviver a logout e reinicialização, crie um serviço systemd.

Crie o arquivo de serviço:

```bash
sudo nano /etc/systemd/system/lzt-monitor.service
```

Conteúdo (troque `SEU_USUARIO` e a URL do webhook):

```ini
[Unit]
Description=Monitor Lolzteam Market
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=SEU_USUARIO
WorkingDirectory=/home/SEU_USUARIO/lzt_monitor
Environment=LZT_WEBHOOK_URL=https://discord.com/api/webhooks/ID/TOKEN
ExecStart=/usr/bin/python3 /home/SEU_USUARIO/lzt_monitor/lzt_monitor.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Ative e gerencie:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lzt-monitor
sudo systemctl status lzt-monitor     # ver status
sudo systemctl restart lzt-monitor    # reiniciar
sudo systemctl stop lzt-monitor       # parar
journalctl -u lzt-monitor -f          # acompanhar os logs ao vivo
```

> Descubra o caminho do Python com: `which python3` (normalmente
> `/usr/bin/python3`).

---

## 7. Alternativa simples: tmux

Se não quiser systemd, use `tmux` (sessão que continua depois de fechar o terminal):

```bash
sudo apt install -y tmux
tmux new -s lzt           # cria sessão chamada "lzt"
cd ~/lzt_monitor
python3 lzt_monitor.py --webhook "URL_DO_WEBHOOK"
```

Para sair **sem matar** o monitor: `Ctrl+B` depois `D`.
Para voltar à sessão: `tmux attach -t lzt`.

---

## 8. O que é enviado ao Discord

- 🟢 **Anúncio novo** — card verde com título, preço (com e sem taxa), vendedor,
  link direto e botão "Abrir".
- 🟠 **Queda de preço** — a mesma mensagem é *editada* para refletir o novo
  preço (o aviso original continua no canal).
- 🔴 **Vendido** — quando o anúncio some da busca por vários ciclos seguidos, a
  mensagem é *editada* para "VENDIDO".

> O webhook precisa de permissão de **editar mensagens** (o bot "webhook" do
> Discord tem isso por padrão; se você criou um bot manualmente, dê a permissão
> `Manage Messages` no canal).

---

## 9. Solução de problemas

| Sintoma | Causa provável / solução |
|---------|--------------------------|
| `Name or service not known` | Sem internet — verifique a rede. |
| `Falha ao buscar pagina (tentativa N)` | lzt.market bloqueou temporariamente (DDoS-Guard). O monitor tenta de novo sozinho nos próximos ciclos. |
| Desafio DDoS-Guard não resolve | Aguarde alguns minutos e reinicie; reduza a frequência com `--interval 30`. |
| `pip: externally-managed-environment` | Use `--break-system-packages` ou o venv (seção 1). |
| Nenhum anúncio novo | O primeiro ciclo é a linha de base; novas ocorrências só aparecem a partir do 2º ciclo. |

---

## 10. Arquivos gerados

| Arquivo | Função |
|---------|--------|
| `lzt_state.json` | Estado persistente (itens conhecidos, preços, status). Pode ser apagado com `--reset`. |
| `lzt_monitor.log` | Log do programa (gira automaticamente em 1 MB × 3 arquivos). |

---

Feito com 💚 para rodar no Zorin OS. Para celular, veja o tutorial do Termux.

