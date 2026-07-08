"""
Gerador de token do Bling (uma vez por conta/CNPJ).
Troca o 'code' (que aparece na URL depois de você autorizar) por um par de tokens
(access + refresh) e guarda no Supabase, na tabela bling_contas.

Usa SÓ Python puro (sem pip install) pra rodar rápido — o code do Bling expira em segundos.
O Client Secret vem dos Secrets do GitHub — nunca passa pelo chat.
"""
import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# URL de OAuth da API v3 do Bling.
TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"

CID   = os.environ["BLING_CLIENT_ID"]
CSEC  = os.environ["BLING_CLIENT_SECRET"]
CODE  = os.environ["CODE"].strip()
CONTA = os.environ["CONTA"].strip()
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]


def _post(url, data, headers, timeout=30):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1) Troca o code por tokens (Basic auth = base64("client_id:client_secret"))
basic = base64.b64encode(f"{CID}:{CSEC}".encode()).decode()
body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": CODE}).encode()
status, raw = _post(TOKEN_URL, body, {
    "Authorization": "Basic " + basic,
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
})
print("Bling HTTP", status)
d = json.loads(raw)
if "access_token" not in d:
    raise SystemExit("Falha ao trocar o code por token: " + str(d))

# 2) Grava no Supabase (upsert por 'conta') via API REST, sem biblioteca externa.
expira = (datetime.now(timezone.utc)
          + timedelta(seconds=int(d.get("expires_in", 21600)))).isoformat()
payload = json.dumps({
    "conta": CONTA,
    "refresh_token": d["refresh_token"],
    "access_token": d["access_token"],
    "access_expira_em": expira,
}).encode()
st2, raw2 = _post(
    f"{SB_URL}/rest/v1/bling_contas?on_conflict=conta",
    payload,
    {
        "apikey": SB_KEY,
        "Authorization": "Bearer " + SB_KEY,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    },
)
if st2 >= 300:
    raise SystemExit(f"Falha ao gravar no Supabase: HTTP {st2} {raw2[:300]}")

print(f"OK! Token guardado para a conta '{CONTA}'. "
      f"O access_token expira em ~6h; o robô renova sozinho depois.")
