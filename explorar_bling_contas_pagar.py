"""
Explorador do Bling (parte 2): confirma os códigos de 'situacao', busca o NOME do
fornecedor (contato) e mostra o DETALHE de uma conta a pagar (pra ver categoria, descrição etc.).
Não grava contas a pagar — só imprime. Python puro.
"""
import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter
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
        raise RuntimeError(f"Sem secret BLING_SECRET_{conta}.")
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
        fields["refresh_token"] = d["refresh_token"]
    sb_patch(conta, fields)
    return d["access_token"]


contas = sb_get("bling_contas?select=conta,client_id,refresh_token")
fez_detalhe = False

for c in contas:
    conta = c["conta"]
    print("\n===========", conta, "===========")
    try:
        access = refresh(conta, c["client_id"], c["refresh_token"])
    except Exception as e:
        print("ERRO refresh:", e)
        continue
    hdr = {"Authorization": "Bearer " + access, "Accept": "application/json"}

    st, raw = http("GET", BASE + "/contas/pagar?pagina=1&limite=100", hdr)
    print("GET /contas/pagar (limite 100) -> HTTP", st)
    try:
        arr = json.loads(raw).get("data", [])
    except Exception:
        print(raw[:600]); continue
    print("Registros nesta página:", len(arr))
    print("Contagem por 'situacao':", dict(Counter(x.get("situacao") for x in arr)))

    if arr and not fez_detalhe:
        fez_detalhe = True
        primeiro = arr[0]
        pid = primeiro.get("id")
        cid = (primeiro.get("contato") or {}).get("id")

        st2, raw2 = http("GET", f"{BASE}/contas/pagar/{pid}", hdr)
        print(f"\n-- DETALHE de uma conta a pagar (/contas/pagar/{pid}) HTTP {st2} --")
        print(raw2[:2800])

        if cid:
            st3, raw3 = http("GET", f"{BASE}/contatos/{cid}", hdr)
            print(f"\n-- FORNECEDOR (/contatos/{cid}) HTTP {st3} --")
            print(raw3[:1800])
