"""
Explorador da Shopee: testa o refresh do token e mostra a ESTRUTURA CRUA de:
  - lista de pedidos (get_order_list)
  - detalhe de um pedido (get_order_detail)
  - escrow/financeiro de um pedido (get_escrow_detail)
Não grava nada — só imprime, pra gente montar o robô certo. Python puro.
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
PID = int(os.environ.get("SHOPEE_PARTNER_ID") or "2039646")
PKEY = os.environ["SHOPEE_PARTNER_KEY"].encode()
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]


def http(method, url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def sb_get(path):
    st, raw = http("GET", f"{SB_URL}/rest/v1/{path}",
                   {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    return json.loads(raw) if st < 300 and raw else []


def sb_patch(shop_id, fields):
    http("PATCH", f"{SB_URL}/rest/v1/shopee_contas?shop_id=eq.{shop_id}",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
          "Content-Type": "application/json", "Prefer": "return=minimal"},
         json.dumps(fields).encode())


def sign_public(path, ts):
    return hmac.new(PKEY, f"{PID}{path}{ts}".encode(), hashlib.sha256).hexdigest()


def sign_shop(path, ts, token, shop_id):
    base = f"{PID}{path}{ts}{token}{shop_id}".encode()
    return hmac.new(PKEY, base, hashlib.sha256).hexdigest()


def refresh(shop_id, refresh_token):
    ts = int(time.time())
    path = "/api/v2/auth/access_token/get"
    url = f"{HOST}{path}?partner_id={PID}&timestamp={ts}&sign={sign_public(path, ts)}"
    body = json.dumps({"refresh_token": refresh_token, "shop_id": shop_id, "partner_id": PID}).encode()
    st, raw = http("POST", url, {"Content-Type": "application/json"}, body)
    d = json.loads(raw)
    if not d.get("access_token"):
        raise RuntimeError(f"Falha no refresh: HTTP {st} {d}")
    expira = (datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expire_in", 14400)))).isoformat()
    fields = {"access_token": d["access_token"], "access_expira_em": expira}
    if d.get("refresh_token"):
        fields["refresh_token"] = d["refresh_token"]
    sb_patch(shop_id, fields)
    return d["access_token"]


def shop_get(path, token, shop_id, extra):
    ts = int(time.time())
    params = {"partner_id": PID, "timestamp": ts, "access_token": token,
              "shop_id": shop_id, "sign": sign_shop(path, ts, token, shop_id)}
    params.update(extra)
    url = f"{HOST}{path}?" + urllib.parse.urlencode(params)
    return http("GET", url, {"Content-Type": "application/json"})


contas = sb_get("shopee_contas?select=shop_id,refresh_token")
if not contas:
    raise SystemExit("Nenhuma loja em shopee_contas.")

c = contas[0]
shop_id = c["shop_id"]
print("Loja:", shop_id)
token = refresh(shop_id, c["refresh_token"])
print("refresh OK (token renovado e salvo)\n")

# Janela dos últimos 7 dias (Shopee aceita no máx. 15 dias por chamada).
agora = int(time.time())
de = agora - 7 * 24 * 3600
st, raw = shop_get("/api/v2/order/get_order_list", token, shop_id, {
    "time_range_field": "create_time", "time_from": de, "time_to": agora,
    "page_size": 10, "response_optional_fields": "order_status",
})
print("get_order_list HTTP", st)
print(raw[:1200], "\n")

try:
    ol = json.loads(raw).get("response", {}).get("order_list", [])
except Exception:
    ol = []
sns = [o.get("order_sn") for o in ol if o.get("order_sn")]
print("order_sn encontrados:", sns[:5], "\n")

if sns:
    st2, raw2 = shop_get("/api/v2/order/get_order_detail", token, shop_id, {
        "order_sn_list": ",".join(sns[:2]),
        "response_optional_fields": "item_list,total_amount,order_status,create_time,buyer_username,payment_method",
    })
    print("get_order_detail HTTP", st2)
    print(raw2[:3000], "\n")

    st3, raw3 = shop_get("/api/v2/payment/get_escrow_detail", token, shop_id, {"order_sn": sns[0]})
    print("get_escrow_detail HTTP", st3)
    print(raw3[:3000])
