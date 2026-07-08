"""
Gerador de token do Bling (uma vez por conta/CNPJ).
Troca o 'code' (que aparece na URL depois de você autorizar) por um par de tokens
(access + refresh) e guarda no Supabase, na tabela bling_contas.

Roda no GitHub Actions (workflow "Bling - gerar token"). O Client Secret vem dos
Secrets do GitHub — nunca passa pelo chat.
"""
import os
import base64
from datetime import datetime, timezone, timedelta
import requests
from supabase import create_client

# URLs de OAuth da API v3 do Bling (confirmamos na 1ª rodada pelo log).
TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"

CID   = os.environ["BLING_CLIENT_ID"]
CSEC  = os.environ["BLING_CLIENT_SECRET"]
CODE  = os.environ["CODE"].strip()
CONTA = os.environ["CONTA"].strip()
SB_URL = os.environ["SUPABASE_URL"]
SB_KEY = os.environ["SUPABASE_KEY"]

# Autenticação Basic: base64("client_id:client_secret")
basic = base64.b64encode(f"{CID}:{CSEC}".encode()).decode()

r = requests.post(
    TOKEN_URL,
    headers={
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    },
    data={"grant_type": "authorization_code", "code": CODE},
    timeout=30,
)
print("HTTP", r.status_code)
try:
    d = r.json()
except Exception:
    raise SystemExit("Resposta não-JSON do Bling: " + r.text[:300])

if "access_token" not in d:
    # mostra o erro pra gente entender (code expirado, URL errada, secret errado, etc.)
    raise SystemExit("Falha ao trocar o code por token: " + str(d))

expira = datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expires_in", 21600)))

sb = create_client(SB_URL, SB_KEY)
sb.table("bling_contas").upsert({
    "conta": CONTA,
    "refresh_token": d["refresh_token"],
    "access_token": d["access_token"],
    "access_expira_em": expira.isoformat(),
}, on_conflict="conta").execute()

print(f"OK! Token guardado para a conta '{CONTA}'. "
      f"O access_token expira em ~6h; o robô renova sozinho depois.")
