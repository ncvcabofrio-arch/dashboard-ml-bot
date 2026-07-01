"""
Mutirao HISTORICO — puxa as vendas desde jan/2026 (ou o mês definido),
em janelas de 10 dias (para não estourar a paginação do ML mesmo em meses
grandes), e preenche frete, repasse, rebate e custo.

RESUMÍVEL e resistente a quedas: se parar, é só rodar de novo. Reaproveita o puxador.py.
"""

import os
import time
import urllib.parse
from datetime import datetime, timezone, date, timedelta

from puxador import (sb, ml_get, lista_refresh_tokens, renovar_token,
                     enriquecer_frete, enriquecer_repasse)

ANO_INI = int(os.environ.get("ANO_INI", "2026"))
MES_INI = int(os.environ.get("MES_INI", "1"))
PASSO = int(os.environ.get("PASSO_DIAS", "10"))   # tamanho da janela em dias


def janelas():
    d = date(ANO_INI, MES_INI, 1)
    hoje = datetime.now(timezone.utc).date()
    out = []
    while d <= hoje:
        fim = d + timedelta(days=PASSO)
        de = d.strftime("%Y-%m-%dT00:00:00.000-03:00")
        ate = fim.strftime("%Y-%m-%dT00:00:00.000-03:00")
        out.append((de, ate, d.strftime("%d/%m/%Y")))
        d = fim
    return out


def pull_range(access, sid, de_iso, ate_iso):
    linhas, offset, total = [], 0, 1
    while offset < total and offset < 1000:
        path = ("/orders/search?seller=" + sid +
                "&order.date_created.from=" + urllib.parse.quote(de_iso) +
                "&order.date_created.to=" + urllib.parse.quote(ate_iso) +
                "&sort=date_asc&limit=50&offset=" + str(offset))
        data = ml_get(path, access)
        total = (data.get("paging") or {}).get("total", 0)
        for o in data.get("results", []):
            ship = (o.get("shipping") or {}).get("id")
            pay = (o.get("payments") or [{}])[0]
            for it in o.get("order_items", []):
                item = it.get("item") or {}
                linhas.append({
                    "seller_id": sid,
                    "order_id": str(o.get("id")),
                    "status": o.get("status"),
                    "data_aprovacao": o.get("date_created"),
                    "total": o.get("total_amount"),
                    "forma_pagamento": pay.get("payment_method_id"),
                    "payment_id": str(pay.get("id")) if pay.get("id") else None,
                    "comissao": it.get("sale_fee"),
                    "shipping_id": str(ship) if ship else None,
                    "item_id": item.get("id"),
                    "sku": item.get("seller_sku") or item.get("seller_custom_field"),
                    "titulo": item.get("title"),
                    "categoria_id": item.get("category_id"),
                    "quantidade": it.get("quantity"),
                    "valor_unitario": it.get("unit_price"),
                })
        offset += 50
        time.sleep(0.3)

    for i in range(0, len(linhas), 200):
        _retry(lambda: sb.table("vendas").upsert(
            linhas[i:i + 200], on_conflict="order_id,item_id,seller_id").execute())
    skus = sorted({l["sku"] for l in linhas if l.get("sku")})
    if skus:
        try:
            sb.table("produtos").upsert([{"sku": s} for s in skus],
                                        on_conflict="sku", ignore_duplicates=True).execute()
        except Exception:
            pass
    return len(linhas)


def _retry(fn, tentativas=4):
    for i in range(tentativas):
        try:
            return fn()
        except Exception as e:
            if i == tentativas - 1:
                raise
            print("  retry por erro de conexao:", str(e)[:80])
            time.sleep(3 * (i + 1))


def main():
    js = janelas()
    for seller_id, refresh in lista_refresh_tokens():
        d = renovar_token(refresh)
        access = d["access_token"]
        sid = str(d.get("user_id") or seller_id)
        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
            on_conflict="seller_id").execute()

        for de, ate, label in js:
            try:
                n = pull_range(access, sid, de, ate)
                print(f"[{sid}] {label}: {n} itens")
            except Exception as e:
                print(f"[{sid}] {label}: ERRO {str(e)[:80]}")

        print(f"[{sid}] preenchendo frete...")
        for _ in range(200):
            try:
                if enriquecer_frete(access, sid) == 0:
                    break
            except Exception as e:
                print("  frete erro:", str(e)[:80]); time.sleep(3)

        print(f"[{sid}] preenchendo repasse/rebate...")
        for _ in range(200):
            try:
                if enriquecer_repasse(access, sid) == 0:
                    break
            except Exception as e:
                print("  repasse erro:", str(e)[:80]); time.sleep(3)

    try:
        sb.rpc("backfill_custos").execute()
        print("Custos preenchidos.")
    except Exception as e:
        print("Aviso custo:", str(e)[:80])

    print("✅ Historico concluido.")


if __name__ == "__main__":
    main()
