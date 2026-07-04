"""
Backfill dos pedidos SEM repasse casado (recebido = 0), da(s) conta(s) que TÊM
repasse. Diferente do backfill_pagamentos.py (que só pega recebido>0), este pega
os pedidos cujo payment_id gravado não bate com nenhum id liquidado — típico de
meio combinado onde o ML liquidou sob ids "filhos".

Para cada pedido: GET /orders/{id} no ML, lê a lista 'payments' e grava TODOS os
payment_ids na 'ordem_pagamentos'. A conciliação (que já olha essa tabela) passa
a casar sozinha. Idempotente (upsert).

Modo TESTE: se a env ORDERS_TESTE tiver order_ids (separados por vírgula), roda
SÓ nesses pedidos e imprime os pagamentos achados — bom pra validar em 1 pedido
antes de rodar em todos.
Modo CHEIO: sem ORDERS_TESTE, chama a função pedidos_sem_repasse no banco.

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
DIAS = int(os.environ.get("DIAS", "30"))                       # hold mínimo pra considerar atrasado
ORDERS_TESTE = [o.strip() for o in os.environ.get("ORDERS_TESTE", "").split(",") if o.strip()]

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
    return {str(c["seller_id"]): c["refresh_token"]
            for c in (res.data or []) if c.get("refresh_token")}


def pagamentos_do_pedido(o, seller_id):
    oid = str(o.get("id"))
    out = []
    for p in (o.get("payments") or []):
        pid = p.get("id")
        if pid:
            out.append({
                "order_id": oid,
                "payment_id": str(pid),
                "seller_id": seller_id,
                # dados só pra log (não gravados):
                "_status": p.get("status"),
                "_amount": p.get("total_paid_amount") or p.get("transaction_amount"),
            })
    return out


def gravar_pagamentos(pags):
    if not pags:
        return
    vistos, uniq = set(), []
    for p in pags:
        k = (p["order_id"], p["payment_id"])
        if k not in vistos:
            vistos.add(k)
            uniq.append({"order_id": p["order_id"], "payment_id": p["payment_id"], "seller_id": p["seller_id"]})
    for i in range(0, len(uniq), 200):
        try:
            sb.table("ordem_pagamentos").upsert(
                uniq[i:i + 200], on_conflict="order_id,payment_id").execute()
        except Exception as e:
            print("  Aviso: falha ao gravar ordem_pagamentos:", str(e)[:120])


def seller_dos_pedidos(order_ids):
    """No modo teste, descobre a conta de cada pedido pela tabela vendas."""
    out = {}
    for i in range(0, len(order_ids), 100):
        chunk = order_ids[i:i + 100]
        res = sb.table("vendas").select("order_id, seller_id").in_("order_id", chunk).execute()
        for r in (res.data or []):
            out[str(r["order_id"])] = str(r["seller_id"])
    return out


def obter_lista_pedidos():
    if ORDERS_TESTE:
        print(f"MODO TESTE — {len(ORDERS_TESTE)} pedido(s): {ORDERS_TESTE}")
        mapa = seller_dos_pedidos(ORDERS_TESTE)
        return [{"order_id": oid, "seller_id": mapa.get(oid, "")} for oid in ORDERS_TESTE]
    res = sb.rpc("pedidos_sem_repasse", {"p_dias": DIAS}).execute()
    lst = res.data or []
    print(f"MODO CHEIO — pedidos sem repasse a re-checar: {len(lst)}")
    return lst


def main():
    pedidos = obter_lista_pedidos()
    if not pedidos:
        print("Nada a fazer.")
        return

    por_seller = defaultdict(list)
    for row in pedidos:
        sid = str(row.get("seller_id") or "")
        if sid:
            por_seller[sid].append(str(row["order_id"]))
        else:
            print(f"  Aviso: pedido {row.get('order_id')} sem seller_id — pulando.")

    tokens = lista_refresh_tokens()
    total_multi = 0

    for seller_id, ordens in por_seller.items():
        refresh = tokens.get(seller_id) or SEED_REFRESH
        if not refresh:
            print(f"[{seller_id}] sem refresh_token — pulando {len(ordens)} pedidos.")
            continue
        try:
            d = renovar_token(refresh)
        except Exception as e:
            print(f"[{seller_id}] falha ao renovar token: {e} — pulando.")
            continue
        access = d["access_token"]
        try:
            sb.table("contas").update(
                {"refresh_token": d.get("refresh_token", refresh)}
            ).eq("seller_id", int(seller_id)).execute()
        except Exception:
            pass

        print(f"[{seller_id}] re-checando {len(ordens)} pedidos...")
        pags = []
        for n, oid in enumerate(ordens, 1):
            r = ml_get(f"/orders/{oid}", access)
            if r is None or r.status_code != 200:
                print(f"  {oid}: falhou ({r.status_code if r else 'sem resposta'})")
                continue
            ps = pagamentos_do_pedido(r.json(), seller_id)
            if len(ps) > 1:
                total_multi += 1
            if ORDERS_TESTE:
                print(f"  {oid}: {len(ps)} pagamento(s) no ML:")
                for p in ps:
                    print(f"     payment_id={p['payment_id']} | status={p['_status']} | valor={p['_amount']}")
            pags.extend(ps)
            if n % 100 == 0:
                print(f"  {n}/{len(ordens)}...")
            time.sleep(0.05)
        gravar_pagamentos(pags)
        print(f"[{seller_id}] gravado. {len(pags)} pagamentos no total; {total_multi} pedidos com 2+ pagamentos.")

    print("✅ Concluído.")


if __name__ == "__main__":
    main()
