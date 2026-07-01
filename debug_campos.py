"""
Debug (só leitura de dados) — imprime 1 pedido e 1 envio reais pra confirmarmos
de onde vêm: estado/cidade, tipo de anúncio (clássico/premium) e forma/tipo de
pagamento.

Roda no GitHub Actions usando o MESMO fluxo dos robôs: renova o token E salva o
novo de volta na tabela 'contas' (sem quebrar o robô diário).
Não grava nada em 'vendas'.
"""

import json
from puxador import (sb, API, ml_get, lista_refresh_tokens, renovar_token)


def main():
    contas = lista_refresh_tokens()
    if not contas:
        print("Nenhuma conta encontrada em 'contas'.")
        return

    seller_id, refresh = contas[0]           # só a primeira conta basta
    d = renovar_token(refresh)
    access = d["access_token"]
    sid = str(d.get("user_id") or seller_id)

    # SALVA o novo refresh_token de volta (igual aos robôs) pra não quebrar nada
    sb.table("contas").upsert(
        {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
        on_conflict="seller_id").execute()
    print("Conta:", sid, "| token renovado e salvo de volta.")

    o = ml_get(f"/orders/search?seller={sid}&sort=date_desc&limit=5", access)
    results = o.get("results", []) if isinstance(o, dict) else []
    print("Pedidos recebidos:", len(results))

    ped = None
    for x in results:
        if (x.get("shipping") or {}).get("id"):
            ped = x
            break
    ped = ped or (results[0] if results else None)
    if not ped:
        print("Nenhum pedido encontrado.")
        return

    it0 = (ped.get("order_items") or [{}])[0]
    pay0 = (ped.get("payments") or [{}])[0]
    print("\n=== CAMPOS-CHAVE DO PEDIDO ===")
    print("listing_type_id (tipo anuncio):", it0.get("listing_type_id"))
    print("payment_method_id (forma):     ", pay0.get("payment_method_id"))
    print("payment_type_id (tipo):        ", pay0.get("payment_type_id"))
    ship_id = (ped.get("shipping") or {}).get("id")
    print("shipping.id:                   ", ship_id)
    print("receiver_address no pedido?    ", bool((ped.get("shipping") or {}).get("receiver_address")))

    print("\n=== PEDIDO (JSON recortado) ===")
    print(json.dumps(ped, indent=2, ensure_ascii=False)[:6000])

    if ship_id:
        try:
            sj = ml_get(f"/shipments/{ship_id}", access)
            ra = (sj or {}).get("receiver_address") or {}
            print("\n=== ENVIO /shipments/%s ===" % ship_id)
            print("receiver_address.state:", ra.get("state"))
            print("receiver_address.city: ", ra.get("city"))
            print("receiver_address.zip:  ", ra.get("zip_code"))
            print("\n--- ENVIO (JSON recortado) ---")
            print(json.dumps(sj, indent=2, ensure_ascii=False)[:4000])
        except Exception as e:
            print("erro lendo envio:", e)


if __name__ == "__main__":
    main()
