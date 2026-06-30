"""
Explorador de REPASSE (rodar 1x, manual).
Pega um pedido recente e tenta varios endpoints financeiros para descobrir
onde estao: valor liquido recebido (repasse), taxas e estornos.
Reaproveita o puxador.py (precisa estar no repositorio).
"""

import json
from puxador import sb, ml_get, lista_refresh_tokens, renovar_token


def main():
    seller_id, refresh = lista_refresh_tokens()[0]
    d = renovar_token(refresh)
    access = d["access_token"]
    sid = str(d.get("user_id") or seller_id)
    sb.table("contas").upsert(
        {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
        on_conflict="seller_id").execute()

    # pega o pedido mais recente
    data = ml_get(f"/orders/search?seller={sid}&sort=date_desc&limit=1", access)
    o = (data.get("results") or [{}])[0]
    oid = o.get("id")
    pid = ((o.get("payments") or [{}])[0]).get("id")
    print("===== PEDIDO/REPASSE (DEBUG) =====")
    print("order:", oid, "| payment:", pid)

    # tenta varios endpoints e mostra o que retorna 200
    for ep in [
        f"/collections/{pid}",
        f"/v1/payments/{pid}",
        f"/payments/{pid}",
        f"/orders/{oid}/billing_info",
        f"/orders/{oid}/discounts",
        f"/billing/integration/periods",
    ]:
        try:
            r = ml_get(ep, access)
            print(f"\n----- GET {ep} -----")
            print(json.dumps(r, ensure_ascii=False, indent=2)[:2500])
        except Exception as e:
            print(f"\n----- GET {ep} -> ERRO: {e}")
    print("===== FIM DEBUG =====")


if __name__ == "__main__":
    main()
