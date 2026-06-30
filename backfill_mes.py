"""
Mutirao do MES (rodar 1x, manual).
Re-baixa as vendas do periodo (DIAS do env) e preenche, de uma vez:
frete, repasse, rebate e custo.
Reaproveita o puxador.py (precisa estar no repositorio).
"""

from puxador import (sb, lista_refresh_tokens, renovar_token, puxar_conta,
                     enriquecer_frete, enriquecer_repasse)


def main():
    for seller_id, refresh in lista_refresh_tokens():
        d = renovar_token(refresh)
        access = d["access_token"]
        sid = str(d.get("user_id") or seller_id)
        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
            on_conflict="seller_id").execute()

        print(f"[{sid}] re-baixando vendas do periodo...")
        puxar_conta(access, sid)          # usa DIAS do env (ex.: 31)

        print(f"[{sid}] preenchendo frete...")
        for _ in range(25):               # cada rodada faz ate 80 pedidos
            enriquecer_frete(access, sid)

        print(f"[{sid}] preenchendo repasse e rebate...")
        for _ in range(25):
            enriquecer_repasse(access, sid)

    # preenche o custo congelado das vendas
    try:
        sb.rpc("backfill_custos").execute()
        print("Custos preenchidos.")
    except Exception as e:
        print("Aviso ao preencher custo:", e)

    print("✅ Mutirao do periodo concluido.")


if __name__ == "__main__":
    main()
