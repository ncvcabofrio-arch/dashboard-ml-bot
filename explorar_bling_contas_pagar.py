"""
Explorador do Bling: testa o refresh do token e mostra a ESTRUTURA CRUA de contas a pagar.
Não grava contas a pagar ainda — só imprime, pra gente ver os campos reais e montar o robô certo.
Roda com Python puro (sem pip install).
"""
import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

BASE = "https://www.bling.com.br/Api/v3"
TOKEN_URL = BASE + "/oauth/token"

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]


def http(method, url, headers, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def sb_get(path):
    st, raw = http("GET", f"{SB_URL}/rest/v1/{path}",
                   {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    return json.loads(raw) if st < 300 else []


def sb_patch(conta, fields):
    body = json.dumps(fields).encode()
    http("PATCH",
         f"{SB_URL}/rest/v1/bling_contas?conta=eq.{urllib.parse.quote(conta)}",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
          "Content-Type": "application/json", "Prefer": "return=minimal"}, body)


def refresh(conta, client_id, refresh_token):
    secret = os.environ.get("BLING_SECRET_" + conta)
    if not secret:
        raise RuntimeError(f"Sem secret BLING_SECRET_{conta} no GitHub.")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}).encode()
    st, raw = http("POST", TOKEN_URL, {
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"}, body)
    d = json.loads(raw)
    if "access_token" not in d:
        raise RuntimeError(f"Falha no refresh: HTTP {st} {d}")
    expira = (datetime.now(timezone.utc)
              + timedelta(seconds=int(d.get("expires_in", 21600)))).isoformat()
    fields = {"access_token": d["access_token"], "access_expira_em": expira}
    if d.get("refresh_token"):
        fields["refresh_token"] = d["refresh_token"]   # Bling pode rotacionar
    sb_patch(conta, fields)
    return d["access_token"]


contas = sb_get("bling_contas?select=conta,client_id,refresh_token")
print("Contas encontradas:", [c["conta"] for c in contas])

for c in contas:
    conta = c["conta"]
    print("\n=========== ", conta, " ===========")
    try:
        access = refresh(conta, c["client_id"], c["refresh_token"])
        print("refresh OK (token renovado e salvo)")
    except Exception as e:
        print("ERRO no refresh:", e)
        continue

    # Amostra pequena de contas a pagar (só pra ver a estrutura).
    st, raw = http("GET", BASE + "/contas/pagar?pagina=1&limite=3",
                   {"Authorization": "Bearer " + access, "Accept": "application/json"})
    print("GET /contas/pagar -> HTTP", st)
    print(raw[:3000])
