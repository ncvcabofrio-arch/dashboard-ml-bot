"""
Debug — descobre até onde o Mercado Livre devolve pedidos pela API.
Para cada conta, pergunta o 'paging.total' de /orders/search mês a mês,
de 2026 voltando até 2024. Onde aparecer 0, é o limite do histórico da API.

Roda no GitHub (mesmo fluxo dos robôs: renova e salva o token de volta).
Não grava nada em 'vendas'.
"""

import urllib.parse
from puxador import (sb, ml_get, lista_refresh_tokens, renovar_token)


def total_do_mes(access, sid, ano, mes):
    de = f"{ano:04d}-{mes:02d}-01T00:00:00.000-03:00"
    prox = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    ate = f"{prox[0]:04d}-{prox[1]:02d}-01T00:00:00.000-03:00"
    path = ("/orders/search?seller=" + sid +
            "&order.date_created.from=" + urllib.parse.quote(de) +
            "&order.date_created.to=" + urllib.parse.quote(ate) +
            "&limit=1&offset=0")
    data = ml_get(path, access)
    if not isinstance(data, dict):
        return "?", str(data)[:80]
    if "paging" not in data:
        # provavelmente veio uma mensagem de erro do ML
        return "ERRO", str(data)[:120]
    return (data.get("paging") or {}).get("total", 0), ""


def main():
    # meses a checar: 2026 (jul..jan), 2025 (dez..jan), 2024 (dez..jan)
    checar = []
    for ano in (2026, 2025, 2024):
        for mes in range(12, 0, -1):
            if ano == 2026 and mes > 7:
                continue
            checar.append((ano, mes))

    for seller_id, refresh in lista_refresh_tokens():
        d = renovar_token(refresh)
        access = d["access_token"]
        sid = str(d.get("user_id") or seller_id)
        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
            on_conflict="seller_id").execute()
        print(f"\n===== CONTA {sid} =====", flush=True)
        for ano, mes in checar:
            total, obs = total_do_mes(access, sid, ano, mes)
            print(f"  {ano}-{mes:02d}: total = {total}   {obs}", flush=True)


if __name__ == "__main__":
    main()
