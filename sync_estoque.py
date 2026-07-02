"""
Sincronizador de ESTOQUE Ideris -> Supabase (roda de 2 em 2 horas).

Versão leve do sync_ideris.py: só lê a QUANTIDADE de cada anúncio no Ideris
e atualiza 'estoque_base' (+ 'estoque_sync_em') na tabela 'produtos'.
Não mexe em custo, nome, modelo nem em vendas — por isso é rápido.

O estoque do dia a dia continua sendo calculado pela view 'estoque_atual'
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


def coletar_estoque(token):
    """Retorna {sku: quantidade}."""
    H = {"Authorization": "Bearer " + token}
    estoques = {}
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
            if sku and item.get("quantity") is not None:
                estoques[sku] = item.get("quantity")
        offset += len(batch)
        time.sleep(1.3)               # respeita o limite (50 chamadas/min)
    print(f"Coletado estoque de {len(estoques)} SKUs (de {total} modelos)")
    return estoques


def main():
    token = login()
    estoques = coletar_estoque(token)
    if not estoques:
        print("⚠️ Nada coletado. Estoque não foi atualizado.")
        return

    agora = datetime.now(timezone.utc).isoformat()
    linhas = [{"sku": s, "estoque_base": q, "estoque_sync_em": agora}
              for s, q in estoques.items()]
    for i in range(0, len(linhas), 200):
        sb.table("produtos").upsert(linhas[i:i + 200], on_conflict="sku").execute()
    print(f"✅ Estoque atualizado: {len(linhas)} produtos em {agora}")


if __name__ == "__main__":
    main()
