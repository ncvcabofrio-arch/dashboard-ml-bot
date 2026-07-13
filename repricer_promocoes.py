"""
Sonda de PROMOÇÕES COMPARTILHADAS — SOMENTE LEITURA.
Só itens ATIVOS. Para cada promoção onde o ML banca parte (meli_percentage),
busca o detalhe da oferta: preço original, preço com desconto, quanto o ML paga
e quanto VOCÊ recebe — e compara com custo/comissão pra estimar a margem final.
Nada é aplicado nem alterado.
"""
import os
import time
import json
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
AMOSTRA = int(os.environ.get("AMOSTRA", "20"))
MARGEM_MIN = float(os.environ.get("MARGEM_MIN", "18"))
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get(path, access, tent=3):
    r = None
    for i in range(tent):
        r = requests.get(API + path, headers={"Authorization": "Bearer " + access}, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": (r.text or "")[:300]}


def contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    cs = [(c["seller_id"], c.get("refresh_token")) for c in (res.data or []) if c.get("refresh_token")]
    if not cs and SEED_REFRESH:
        cs = [(None, SEED_REFRESH)]
    return cs


def custo_de(sku):
    if not sku:
        return None
    try:
        r = sb.table("produtos").select("custo").eq("sku", sku).limit(1).execute().data
        if r and r[0].get("custo") is not None:
            return float(r[0]["custo"])
    except Exception:
        pass
    return None


def comissao(preco, cat, ltid, access):
    if not preco:
        return None
    path = f"/sites/MLB/listing_prices?price={preco}"
    if ltid:
        path += f"&listing_type_id={ltid}"
    if cat:
        path += f"&category_id={cat}"
    st, d = get(path, access)
    if isinstance(d, list) and d:
        d = d[0]
    return d.get("sale_fee_amount") if isinstance(d, dict) else None


def main():
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        print(f"\n===== CONTA {sid} =====", flush=True)

        st, busca = get(f"/users/{sid}/items/search?status=active&limit={AMOSTRA}", access)
        ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []
        print(f"itens ativos na amostra: {len(ids)}", flush=True)

        detalhes_mostrados = 0
        for item_id in ids:
            st, promos = get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
            lista = promos if isinstance(promos, list) else []
            comp = [p for p in lista if isinstance(p, dict) and p.get("meli_percentage")]
            if not comp:
                continue

            st, it = get(f"/items/{item_id}", access)
            preco = it.get("price") if isinstance(it, dict) else None
            ltid = it.get("listing_type_id") if isinstance(it, dict) else None
            cat = it.get("category_id") if isinstance(it, dict) else None
            sku = (it.get("seller_sku") or it.get("seller_custom_field")) if isinstance(it, dict) else None
            custo = custo_de(sku)

            nomes = ", ".join(f"{p.get('type')}({p.get('id')}, ML {int((p.get('meli_percentage') or 0)*100)}%)" for p in comp)
            print(f"\n- {item_id} | {(it.get('title') or '')[:45] if isinstance(it, dict) else ''}", flush=True)
            print(f"    preço=R${preco}  custo={custo}  compartilhadas: {nomes}", flush=True)

            # detalhe da 1ª compartilhada (preço proposto e quanto você recebe)
            if detalhes_mostrados < 4:
                p = comp[0]
                pid, ptype = p.get("id"), p.get("type")
                st, det = get(f"/seller-promotions/items/{item_id}?promotion_id={pid}&promotion_type={ptype}&app_version=v2", access)
                print(f"    detalhe (status {st}):", json.dumps(det, ensure_ascii=False)[:900], flush=True)
                detalhes_mostrados += 1
            time.sleep(0.2)

    print("\n=== sonda concluída (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
