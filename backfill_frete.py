"""
Mutirao de FRETE (rodar 1x, manualmente).

1) Re-baixa um periodo amplo (DIAS do env, ex.: 120) para preencher shipping_id,
   comissao e corrigir SKU das vendas antigas.
2) Busca o frete (custo do envio) de TODOS os pedidos que ainda estao sem.
3) Roda o backfill de custo (preenche custo_unitario das vendas).

Reaproveita as funcoes do puxador.py (precisa estar no mesmo repositorio).
"""

import time
from collections import defaultdict

from puxador import (sb, ml_get, lista_refresh_tokens, renovar_token, puxar_conta)


def pega_pendentes(sid, limite=300):
    """Vendas pagas, sem frete e COM shipping_id (as que dao pra enriquecer)."""
    try:
        rows = (sb.table("vendas")
                .select("id, order_id, shipping_id, valor_unitario, quantidade")
                .eq("seller_id", sid).eq("status", "paid")
                .is_("frete", "null").filter("shipping_id", "not.is", "null")
                .limit(limite).execute().data) or []
    except Exception:
        rows = (sb.table("vendas")
                .select("id, order_id, shipping_id, valor_unitario, quantidade")
                .eq("seller_id", sid).eq("status", "paid")
                .is_("frete", "null").limit(limite).execute().data) or []
    return [r for r in rows if r.get("shipping_id")]


def enriquece(access, sid):
    total = 0
    vistos = set()
    while True:
        pend = [r for r in pega_pendentes(sid, 300) if r["order_id"] not in vistos]
        if not pend:
            break
        pedidos = defaultdict(list)
        for r in pend:
            pedidos[r["order_id"]].append(r)
        for oid, itens in pedidos.items():
            vistos.add(oid)
            ship_id = itens[0]["shipping_id"]
            try:
                c = ml_get(f"/shipments/{ship_id}/costs", access)
            except Exception:
                continue
            senders = c.get("senders") or []
            frete_ped = 0
            if senders:
                match = [s for s in senders if str(s.get("user_id")) == str(sid)]
                frete_ped = (match[0] if match else senders[0]).get("cost") or 0
            total_val = sum((it["valor_unitario"] or 0) * (it["quantidade"] or 1)
                            for it in itens) or 1
            for it in itens:
                val = (it["valor_unitario"] or 0) * (it["quantidade"] or 1)
                sb.table("vendas").update(
                    {"frete": round(frete_ped * (val / total_val), 2)}
                ).eq("id", it["id"]).execute()
            total += 1
            if total % 25 == 0:
                print(f"[{sid}] {total} pedidos com frete preenchido...")
            time.sleep(0.35)
    print(f"[{sid}] frete concluido: {total} pedidos.")


def main():
    for seller_id, refresh in lista_refresh_tokens():
        d = renovar_token(refresh)
        access = d["access_token"]
        sid = str(d.get("user_id") or seller_id)
        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
            on_conflict="seller_id").execute()

        print(f"[{sid}] re-baixando pedidos (janela ampla) para preencher shipping_id...")
        puxar_conta(access, sid)     # usa DIAS do env (ex.: 120)

        enriquece(access, sid)

    # preenche o custo das vendas (SKU ja corrigido)
    try:
        sb.rpc("backfill_custos").execute()
        print("Custos preenchidos.")
    except Exception as e:
        print("Aviso ao preencher custo:", e)

    print("✅ Mutirao concluido.")


if __name__ == "__main__":
    main()
