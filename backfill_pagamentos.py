"""
Backfill DIRECIONADO de meios combinados.
Pega SÓ os pedidos divergentes (função pedidos_divergentes no banco), re-consulta
cada um na API do Mercado Livre e grava TODOS os pagamentos na 'ordem_pagamentos'.
Assim a conciliação passa a somar os meios combinados dos pedidos antigos.

Idempotente (upsert) — pode rodar de novo sem duplicar.
Usa os mesmos secrets do robô (ML_* e SUPABASE_*).
"""

import os
import time
import requests
from collections import defaultdict
from supabase import create_client

CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GAP_MIN = float(os.environ.get("GAP_MIN", "0.02"))   # diferença mínima pra re-checar

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


def ml_get(path, access, tentativas=4):
    r = None
    for i in range(tentativas):
        r = requests.get(API + path,
                         headers={"Authorization": "Bearer " + access}, timeout=15)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    return r


def lista_refresh_tokens():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    tokens = {str(c["seller_id"]): c["refresh_token"]
              for c in (res.data or []) if c.get("refresh_token")}
    return tokens


def pagamentos_do_pedido(o, seller_id):
    oid = str(o.get("id"))
    out = []
    for p in (o.get("payments") or []):
        pid = p.get("id")
        if pid:
            out.append({"order_id": oid, "payment_id": str(pid), "seller_id": seller_id})
    return out


def gravar_pagamentos(pags):
    if not pags:
        return
    vistos, uniq = set(), []
    for p in pags:
        k = (p["order_id"], p["payment_id"])
        if k not in vistos:
            vistos.add(k); uniq.append(p)
    for i in range(0, len(uniq), 200):
        try:
            sb.table("ordem_pagamentos").upsert(
                uniq[i:i + 200], on_conflict="order_id,payment_id").execute()
        except Exception as e:
            print("  Aviso: falha ao gravar ordem_pagamentos:", str(e)[:120])


def main():
    # 1) pega a lista de pedidos divergentes (função no banco)
    res = sb.rpc("pedidos_divergentes", {"p_min": GAP_MIN}).execute()
    divergentes = res.data or []
    print(f"Pedidos divergentes a re-checar: {len(divergentes)}")
    if not divergentes:
        print("Nada a fazer — nenhum pedido divergente.")
        return

    # agrupa por conta (seller_id)
    por_seller = defaultdict(list)
    for row in divergentes:
        por_seller[str(row["seller_id"])].append(str(row["order_id"]))

    tokens = lista_refresh_tokens()
    total_corrigidos = 0

    for seller_id, pedidos in por_seller.items():
        refresh = tokens.get(seller_id) or SEED_REFRESH
        if not refresh:
            print(f"[{seller_id}] sem refresh_token — pulando {len(pedidos)} pedidos.")
            continue
        try:
            d = renovar_token(refresh)
        except Exception as e:
            print(f"[{seller_id}] falha ao renovar token: {e} — pulando.")
            continue
        access = d["access_token"]
        # salva o refresh novo (rotaciona)
        try:
            sb.table("contas").update(
                {"refresh_token": d.get("refresh_token", refresh)}
            ).eq("seller_id", int(seller_id)).execute()
        except Exception:
            pass

        print(f"[{seller_id}] re-checando {len(pedidos)} pedidos...")
        pags = []
        achados_multi = 0
        for n, oid in enumerate(pedidos, 1):
            r = ml_get(f"/orders/{oid}", access)
            if r is None or r.status_code != 200:
                continue
            o = r.json()
            ps = pagamentos_do_pedido(o, seller_id)
            if len(ps) > 1:
                achados_multi += 1
            pags.extend(ps)
            if n % 100 == 0:
                print(f"  {n}/{len(pedidos)}...")
            time.sleep(0.05)   # gentil com a API
        gravar_pagamentos(pags)
        total_corrigidos += achados_multi
        print(f"[{seller_id}] pronto. {achados_multi} pedidos com 2+ pagamentos capturados.")

    print(f"✅ Backfill direcionado concluído. Meios combinados capturados: {total_corrigidos}")


if __name__ == "__main__":
    main()
