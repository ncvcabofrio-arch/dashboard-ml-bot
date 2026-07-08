"""
Gerador de token do Bling — uma vez por conta/CNPJ.
Cada conta tem seu PRÓPRIO app (client_id + client_secret).
- client_id: vem como input do workflow (não é secreto) e é gravado no banco.
- client_secret: vem de um Secret do GitHub chamado BLING_SECRET_<CONTA>.

Troca o 'code' por tokens (access + refresh) e guarda no Supabase (tabela bling_contas).
Usa só Python puro (sem pip install) pra rodar rápido — o code do Bling expira em segundos.
"""
import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"

CONTA = os.environ["CONTA"].strip()
CID   = os.environ["CLIENT_ID"].strip()
CODE  = os.environ["CODE"].strip()
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]

# O secret desta conta vem de BLING_SECRET_<CONTA> (ex.: BLING_SECRET_CABOFRIO).
sec_var = "BLING_SECRET_" + CONTA
CSEC = os.environ.get(sec_var)
if not CSEC:
    raise SystemExit(f"Não achei o secret '{sec_var}' no GitHub. "
                     f"Crie um Secret com esse nome e o Client Secret do app da conta {CONTA}.")


def _post(url, data, headers, timeout=30):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1) Troca o code por tokens.
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

# 2) Grava no Supabase (upsert por 'conta'), incluindo o client_id.
expira = (datetime.now(timezone.utc)
          + timedelta(seconds=int(d.get("expires_in", 21600)))).isoformat()
payload = json.dumps({
    "conta": CONTA,
    "client_id": CID,
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
