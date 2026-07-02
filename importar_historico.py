"""
Importa o histórico do Mercado Livre (2020–jun/2025) do arquivo historico_ml.csv
para a tabela 'vendas_hist' do Supabase.

Roda no GitHub Actions. Idempotente (upsert por ano,mes,sku,conta,status) —
pode rodar de novo sem duplicar.
"""

import os
import csv
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ARQ = os.environ.get("ARQ", "historico_ml.csv")
ARQ_UF = os.environ.get("ARQ_UF", "historico_uf.csv")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def num(s):
    try:
        return round(float(s), 2)
    except Exception:
        return 0.0


def main():
    linhas = []
    with open(ARQ, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            linhas.append({
                "ano": int(r["ano"]),
                "mes": int(r["mes"]),
                "sku": (r["sku"] or ""),
                "conta": (r["conta"] or "(sem conta)"),
                "seller_id": int(r["seller_id"]) if r.get("seller_id") not in (None, "") else None,
                "status": (r["status"] or "paid"),
                "unidades": num(r["unidades"]),
                "receita": num(r["receita"]),
                "comissao": num(r["comissao"]),
                "frete": num(r["frete"]),
                "custo": num(r["custo"]),
                "pedidos": int(float(r["pedidos"])),
            })
    print(f"Lidas {len(linhas)} linhas de {ARQ}")

    n = 0
    for i in range(0, len(linhas), 500):
        lote = linhas[i:i + 500]
        sb.table("vendas_hist").upsert(
            lote, on_conflict="ano,mes,sku,conta,status").execute()
        n += len(lote)
        print(f"  gravadas {n}/{len(linhas)}")
    print("✅ Histórico (vendas) importado.")

    # ---- histórico por estado (opcional, se o arquivo existir) ----
    if os.path.exists(ARQ_UF):
        rows = []
        with open(ARQ_UF, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "ano": int(r["ano"]), "mes": int(r["mes"]),
                    "estado": (r["estado"] or "(sem estado)"),
                    "status": (r["status"] or "paid"),
                    "receita": num(r["receita"]), "unidades": num(r["unidades"]),
                    "pedidos": int(float(r["pedidos"])),
                })
        print(f"Lidas {len(rows)} linhas de {ARQ_UF}")
        m = 0
        for i in range(0, len(rows), 500):
            lote = rows[i:i + 500]
            sb.table("vendas_hist_uf").upsert(
                lote, on_conflict="ano,mes,estado,status").execute()
            m += len(lote)
            print(f"  gravadas {m}/{len(rows)}")
        print("✅ Histórico por estado importado.")
    else:
        print(f"(sem {ARQ_UF} — pulei o histórico por estado)")


if __name__ == "__main__":
    main()
