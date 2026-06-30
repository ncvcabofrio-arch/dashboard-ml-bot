"""
Puxador Mercado Livre -> Supabase (versao automatica / GitHub Actions)

- Le o refresh_token (do banco; na 1a vez, do secret ML_REFRESH_TOKEN)
- Renova o acesso no Mercado Livre (e guarda o novo refresh_token no banco)
- Baixa vendas e reputacao e grava no Supabase
- Roda sozinho pelo GitHub Actions; nenhuma senha fica no codigo (vem por secrets)
"""

import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

# ---- Configuracao (vem dos secrets do GitHub) ----
CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DIAS = int(os.environ.get("DIAS", "7"))  # janela de vendas por execucao

API = "https://api.mercadolibre.com"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def renovar_token(refresh_token):
    r = requests.post(API + "/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Falha ao renovar token: " + str(d))
    return d


def ml_get(path, access):
    return requests.get(API + path,
                        headers={"Authorization": "Bearer " + access},
                        timeout=30).json()


def lista_refresh_tokens():
    """Pega os refresh tokens guardados no banco; se nao houver, usa o seed."""
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    tokens = [(c["seller_id"], c["refresh_token"])
              for c in (res.data or []) if c.get("refresh_token")]
    if not tokens and SEED_REFRESH:
        tokens = [(None, SEED_REFRESH)]  # bootstrap da 1a vez
    return tokens


def puxar_conta(access, seller_id):
    # dados da conta
    u = ml_get("/users/" + seller_id, access)
    sb.table("contas").upsert(
        {"seller_id": seller_id, "apelido": u.get("nickname")},
        on_conflict="seller_id").execute()

    # reputacao (1 registro por dia)
    rep = u.get("seller_reputation") or {}
    tr = rep.get("transactions") or {}
    rt = tr.get("ratings") or {}
    met = rep.get("metrics") or {}

    def taxa(k):
        return (met.get(k) or {}).get("rate")

    sb.table("reputacao").upsert({
        "seller_id": seller_id,
        "nivel": rep.get("level_id"),
        "transacoes_total": tr.get("total"),
        "positivas": rt.get("positive"),
        "neutras": rt.get("neutral"),
        "negativas": rt.get("negative"),
        "taxa_reclamacoes": taxa("claims"),
        "taxa_cancelamentos": taxa("cancellations"),
        "taxa_atrasos": taxa("delayed_handling_time"),
    }, on_conflict="seller_id,data").execute()

    # vendas
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)) \
        .strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    linhas, offset, total = [], 0, 1
    while offset < total and offset < 2000:
        path = ("/orders/search?seller=" + seller_id +
                "&order.date_created.from=" + urllib.parse.quote(desde) +
                "&sort=date_desc&limit=50&offset=" + str(offset))
        data = ml_get(path, access)
        total = (data.get("paging") or {}).get("total", 0)
        for o in data.get("results", []):
            pay = (o.get("payments") or [{}])[0]
            for it in o.get("order_items", []):
                item = it.get("item") or {}
                linhas.append({
                    "seller_id": seller_id,
                    "order_id": str(o.get("id")),
                    "status": o.get("status"),
                    "data_aprovacao": o.get("date_created"),
                    "total": o.get("total_amount"),
                    "forma_pagamento": pay.get("payment_method_id"),
                    "item_id": item.get("id"),
                    "sku": item.get("seller_sku"),
                    "titulo": item.get("title"),
                    "categoria_id": item.get("category_id"),
                    "quantidade": it.get("quantity"),
                    "valor_unitario": it.get("unit_price"),
                })
        offset += 50
        time.sleep(0.3)

    for i in range(0, len(linhas), 200):
        sb.table("vendas").upsert(
            linhas[i:i + 200],
            on_conflict="order_id,item_id,seller_id").execute()
    return len(linhas)


def main():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token. Configure o secret ML_REFRESH_TOKEN.")

    for seller_id, refresh in tokens:
        d = renovar_token(refresh)
        access = d["access_token"]
        novo_refresh = d.get("refresh_token", refresh)
        sid = str(d.get("user_id") or seller_id)

        # guarda o novo refresh token JA (antes de puxar), pra nunca perder
        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": novo_refresh},
            on_conflict="seller_id").execute()

        n = puxar_conta(access, sid)
        print(f"[{sid}] {n} vendas atualizadas em {datetime.now()}")


if __name__ == "__main__":
    main()
