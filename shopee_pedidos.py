"""
Robô de pedidos da Shopee -> Supabase.
- Renova o token, baixa os pedidos (get_order_list) em janelas de 15 dias,
  pega o detalhe (get_order_detail) e o financeiro (get_escrow_detail),
  e grava em shopee_vendas (por item) e shopee_repasses (por pedido).
- DIAS (env) = quantos dias pra trás puxar (padrão 15). Pra backfill, use DIAS=180 etc.
Python puro, com freio de requisições.
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
DIAS = int(os.environ.get("DIAS", "15"))

_ultima = [0.0]
def _freio():
    esp = 0.4 - (time.time() - _ultima[0])
    if esp > 0:
        time.sleep(esp)
    _ultima[0] = time.time()


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


def sb_upsert(tabela, rows, conflito):
    for i in range(0, len(rows), 200):
        st, raw = http("POST", f"{SB_URL}/rest/v1/{tabela}?on_conflict={conflito}", {
            "apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }, json.dumps(rows[i:i + 200]).encode())
        if st >= 300:
            raise RuntimeError(f"Supabase {tabela} HTTP {st}: {raw[:200]}")


def sb_patch(shop_id, fields):
    http("PATCH", f"{SB_URL}/rest/v1/shopee_contas?shop_id=eq.{shop_id}",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
          "Content-Type": "application/json", "Prefer": "return=minimal"},
         json.dumps(fields).encode())


def sign_public(path, ts):
    return hmac.new(PKEY, f"{PID}{path}{ts}".encode(), hashlib.sha256).hexdigest()


def sign_shop(path, ts, token, shop_id):
    return hmac.new(PKEY, f"{PID}{path}{ts}{token}{shop_id}".encode(), hashlib.sha256).hexdigest()


def refresh(shop_id, refresh_token):
    ts = int(time.time())
    path = "/api/v2/auth/access_token/get"
    url = f"{HOST}{path}?partner_id={PID}&timestamp={ts}&sign={sign_public(path, ts)}"
    st, raw = http("POST", url, {"Content-Type": "application/json"},
                   json.dumps({"refresh_token": refresh_token, "shop_id": shop_id, "partner_id": PID}).encode())
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
    _freio()
    ts = int(time.time())
    params = {"partner_id": PID, "timestamp": ts, "access_token": token,
              "shop_id": shop_id, "sign": sign_shop(path, ts, token, shop_id)}
    params.update(extra)
    st, raw = http("GET", f"{HOST}{path}?" + urllib.parse.urlencode(params),
                   {"Content-Type": "application/json"})
    return st, json.loads(raw) if raw else {}


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None


def listar_order_sn(token, shop_id):
    """Todos os order_sn no período (janelas de 15 dias, com cursor)."""
    agora = int(time.time())
    inicio = agora - DIAS * 24 * 3600
    achados = {}
    janela = inicio
    while janela < agora:
        fim = min(janela + 15 * 24 * 3600, agora)
        cursor = ""
        while True:
            st, d = shop_get("/api/v2/order/get_order_list", token, shop_id, {
                "time_range_field": "create_time", "time_from": janela, "time_to": fim,
                "page_size": 100, "cursor": cursor, "response_optional_fields": "order_status",
            })
            resp = (d or {}).get("response") or {}
            for o in resp.get("order_list", []):
                if o.get("order_sn"):
                    achados[o["order_sn"]] = o.get("order_status")
            if resp.get("more") and resp.get("next_cursor"):
                cursor = resp["next_cursor"]
            else:
                break
        janela = fim
    return achados


def main():
    contas = sb_get("shopee_contas?select=shop_id,refresh_token")
    if not contas:
        raise SystemExit("Nenhuma loja em shopee_contas.")
    c = contas[0]
    shop_id = c["shop_id"]
    token = refresh(shop_id, c["refresh_token"])
    print(f"Loja {shop_id} — token renovado. Puxando {DIAS} dias...")

    sns = listar_order_sn(token, shop_id)
    print(f"{len(sns)} pedidos no período.")
    lista = list(sns.keys())

    vendas, repasses = [], []
    NAO_PAGO = {"CANCELLED", "UNPAID", "INVOICE_PENDING"}

    for i in range(0, len(lista), 50):
        lote = lista[i:i + 50]
        st, d = shop_get("/api/v2/order/get_order_detail", token, shop_id, {
            "order_sn_list": ",".join(lote),
            "response_optional_fields": "item_list,total_amount,order_status,create_time,buyer_username,payment_method",
        })
        for o in ((d or {}).get("response") or {}).get("order_list", []):
            osn = o.get("order_sn")
            data = iso(o.get("create_time"))
            for it in o.get("item_list", []):
                vendas.append({
                    "order_sn": osn, "item_id": it.get("item_id"),
                    "model_id": it.get("model_id") or 0, "shop_id": shop_id,
                    "status": o.get("order_status"), "data": data,
                    "sku": it.get("model_sku") or it.get("item_sku") or "",
                    "titulo": it.get("item_name"),
                    "quantidade": it.get("model_quantity_purchased"),
                    "valor_unitario": it.get("model_discounted_price"),
                    "forma_pagamento": o.get("payment_method"),
                    "comprador": o.get("buyer_username"),
                    "total_pedido": o.get("total_amount"),
                })

    if vendas:
        sb_upsert("shopee_vendas", vendas, "order_sn,item_id,model_id")

    # Escrow (financeiro) — só dos pedidos que não são cancelados/não pagos.
    pagos = [sn for sn, stt in sns.items() if stt not in NAO_PAGO]
    for sn in pagos:
        st, d = shop_get("/api/v2/payment/get_escrow_detail", token, shop_id, {"order_sn": sn})
        oi = (((d or {}).get("response") or {}).get("order_income") or {})
        if not oi:
            continue
        repasses.append({
            "order_sn": sn, "shop_id": shop_id, "status": sns.get(sn),
            "data": iso(((d or {}).get("response") or {}).get("create_time")),
            "buyer_total": oi.get("buyer_total_amount"),
            "comissao": oi.get("commission_fee"),
            "service_fee": oi.get("net_service_fee"),
            "frete": oi.get("final_shipping_fee") or oi.get("actual_shipping_fee"),
            "repasse": oi.get("escrow_amount_after_adjustment") or oi.get("escrow_amount"),
        })
    if repasses:
        sb_upsert("shopee_repasses", repasses, "order_sn")

    tot_rep = sum((r["repasse"] or 0) for r in repasses)
    print(f"OK. {len(vendas)} itens de venda, {len(repasses)} repasses. "
          f"Repasse somado (líquido): R$ {tot_rep:,.2f}")


if __name__ == "__main__":
    main()
