"""
Sincronizador Ideris -> Supabase (roda 1x/dia, 6h).
Atualiza CUSTO e ESTOQUE BASE de cada produto.

- Login no Ideris (POST /login com o token como texto puro)
- Percorre os modelos de anuncio (/listingModel/search) lendo sku, cost, quantity
- Atualiza 'custo' e 'estoque_base' (+ 'estoque_sync_em') na tabela 'produtos'
- Congela o custo nas vendas que ainda estao sem (funcao backfill_custos)

O estoque do dia a dia é calculado pela view 'estoque_atual'
(estoque_base - vendas pagas desde estoque_sync_em).
"""

import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client

IDERIS_TOKEN = os.environ["IDERIS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

BASE = "https://apiv3.ideris.com.br"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def login():
    r = requests.post(BASE + "/login", json=IDERIS_TOKEN, timeout=30)
    try:
        j = r.json()
        tok = j if isinstance(j, str) else (j.get("token") or j.get("obj") or j)
    except Exception:
        tok = r.text.strip().strip('"')
    if r.status_code != 200 or not tok:
        raise RuntimeError("Falha no login Ideris: " + str(r.status_code) + " " + r.text[:200])
    return str(tok)


def coletar(token):
    """Retorna dois dicts: custos {sku:custo} e estoques {sku:quantidade}."""
    H = {"Authorization": "Bearer " + token}
    custos, estoques = {}, {}
    offset, total, limit = 0, None, 100
    while total is None or offset < total:
        resp = requests.get(
            BASE + f"/listingModel/search?limit={limit}&offset={offset}",
            headers=H, timeout=60)
        if resp.status_code != 200:
            print("Aviso: busca falhou:", resp.status_code, resp.text[:200])
            break
        data = resp.json()
        total = data.get("total", 0)
        batch = data.get("obj", []) or []
        if not batch:
            break
        for item in batch:
            sku = item.get("sku")
            if not sku:
                continue
            if item.get("cost") is not None:
                custos[sku] = item.get("cost")
            if item.get("quantity") is not None:
                estoques[sku] = item.get("quantity")
        offset += len(batch)
        time.sleep(1.3)               # respeita o limite (50 chamadas/min)
    print(f"Coletado: {len(custos)} custos e {len(estoques)} estoques (de {total} modelos)")
    return custos, estoques


def main():
    token = login()
    custos, estoques = coletar(token)

    if not custos and not estoques:
        print("⚠️ Nada coletado. Me avise para ajustar.")
        return

    agora = datetime.now(timezone.utc).isoformat()

    # 1) atualiza ESTOQUE base (não toca no custo)
    linhas_est = [{"sku": s, "estoque_base": q, "estoque_sync_em": agora}
                  for s, q in estoques.items()]
    for i in range(0, len(linhas_est), 200):
        sb.table("produtos").upsert(linhas_est[i:i + 200], on_conflict="sku").execute()
    print(f"Estoque atualizado: {len(linhas_est)} produtos")

    # 2) atualiza CUSTO (não toca no estoque)
    linhas_custo = [{"sku": s, "custo": c} for s, c in custos.items()]
    for i in range(0, len(linhas_custo), 200):
        sb.table("produtos").upsert(linhas_custo[i:i + 200], on_conflict="sku").execute()
    print(f"Custo atualizado: {len(linhas_custo)} produtos")

    # 3) congela o custo nas vendas que ainda estão sem (nunca sobrescreve)
    try:
        sb.rpc("backfill_custos").execute()
        print("Backfill de custo nas vendas concluído.")
    except Exception as e:
        print("Aviso: backfill falhou:", e)


if __name__ == "__main__":
    main()
