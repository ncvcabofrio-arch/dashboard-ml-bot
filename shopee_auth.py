"""
Autenticação da Shopee Open Platform (v2).
Dois modos:
  MODE=url    -> gera o LINK de autorização (assinado) pra você abrir e autorizar a loja.
  MODE=token  -> troca o 'code' (+ shop_id) por access/refresh token e guarda no Supabase.

Python puro (sem pip install). Partner Key vem dos Secrets do GitHub — nunca no chat.
"""
import os
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

HOST = "https://partner.shopeemobile.com"
# Live Partner ID — NÃO é secreto (aparece na URL de autorização). Fica fixo aqui de propósito:
# se ficar nos Secrets do GitHub, o log "mascara" o número (vira ***) e o link sai quebrado.
PID = int(os.environ.get("SHOPEE_PARTNER_ID") or "2039646")
PKEY = os.environ["SHOPEE_PARTNER_KEY"].encode()
REDIRECT = os.environ.get("SHOPEE_REDIRECT",
                          "https://painelbi.ncvcabofrio.workers.dev/shopee-callback")
MODE = os.environ.get("MODE", "url")


def sign_public(path, ts):
    # assinatura das APIs públicas: HMAC-SHA256(partner_key, partner_id + path + timestamp)
    base = f"{PID}{path}{ts}".encode()
    return hmac.new(PKEY, base, hashlib.sha256).hexdigest()


def http(method, url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


if MODE == "url":
    ts = int(time.time())
    path = "/api/v2/shop/auth_partner"
    sign = sign_public(path, ts)
    link = (f"{HOST}{path}?partner_id={PID}&timestamp={ts}&sign={sign}"
            f"&redirect={urllib.parse.quote(REDIRECT, safe='')}")
    print("=== ABRA ESTE LINK (vale ~5 min) ===")
    print("Logado na sua loja Shopee, abra o link, autorize, e copie o 'code' e o 'shop_id'")
    print("que aparecem no endereço depois do redirecionamento.\n")
    print(link)

elif MODE == "token":
    code = os.environ["CODE"].strip()
    shop_id = int(os.environ["SHOP_ID"].strip())
    ts = int(time.time())
    path = "/api/v2/auth/token/get"
    sign = sign_public(path, ts)
    url = f"{HOST}{path}?partner_id={PID}&timestamp={ts}&sign={sign}"
    body = json.dumps({"code": code, "shop_id": shop_id, "partner_id": PID}).encode()
    st, raw = http("POST", url, {"Content-Type": "application/json"}, body)
    print("Shopee HTTP", st)
    d = json.loads(raw)
    if not d.get("access_token"):
        raise SystemExit("Falha ao obter token: " + str(d))

    expira = (datetime.now(timezone.utc)
              + timedelta(seconds=int(d.get("expire_in", 14400)))).isoformat()
    SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
    SB_KEY = os.environ["SUPABASE_KEY"]
    payload = json.dumps({
        "shop_id": shop_id,
        "refresh_token": d["refresh_token"],
        "access_token": d["access_token"],
        "access_expira_em": expira,
    }).encode()
    st2, raw2 = http("POST", f"{SB_URL}/rest/v1/shopee_contas?on_conflict=shop_id", {
        "apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }, payload)
    if st2 >= 300:
        raise SystemExit(f"Falha ao gravar no Supabase: HTTP {st2} {raw2[:200]}")
    print(f"OK! Loja {shop_id} conectada. O access_token expira em ~4h; o robô renova sozinho.")

else:
    raise SystemExit("MODE inválido (use 'url' ou 'token').")
