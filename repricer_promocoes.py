"""
SONDA (descartável) do repricer — SOMENTE LEITURA. Não altera nada no ML.
Uso atual: diagnóstico das PROMOÇÕES de um item, pra ver como o Mercado Livre
pareia nome/preço/percentuais quando o anúncio tem faixa de preço ou variações.
Item padrão: MLB6415054256 (Prato Zeus Custom Splash 12) — troque com ITEM_ID.
"""
import os
import json
import time
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
ITEM_ID = os.environ.get("ITEM_ID", "MLB6415054256")
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
SEED = os.environ.get("ML_REFRESH_TOKEN", "")


def get(path, access, tent=3):
    r = None
    for i in range(tent):
        r = requests.get(API + path, headers={"Authorization": "Bearer " + access}, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1)); continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    cs = [(c["seller_id"], c.get("refresh_token")) for c in (res.data or []) if c.get("refresh_token")]
    if not cs and SEED:
        cs = [(None, SEED)]
    return cs


def dono_do_item(item_id, contas_list):
    """Descobre em qual conta o item está (pra usar o access certo)."""
    for seller_id, refresh in contas_list:
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        st, it = get(f"/items/{item_id}", access)
        if isinstance(it, dict) and it.get("id") == item_id:
            return access, sid, it
    return None, None, None


def main():
    cs = contas()
    access, sid, it = dono_do_item(ITEM_ID, cs)
    if not access:
        print(f"nao achei o item {ITEM_ID} em nenhuma conta", flush=True)
        return
    print(f"===== ITEM {ITEM_ID} | conta {sid} =====", flush=True)
    print(f"title: {it.get('title')}", flush=True)
    print(f"price (item): {it.get('price')} | original_price: {it.get('original_price')}", flush=True)
    print(f"variations: {len(it.get('variations') or [])}", flush=True)

    # 1) RESUMO: lista de promoções do item
    st, resumo = get(f"/seller-promotions/items/{ITEM_ID}?app_version=v2", access)
    print(f"\n--- RESUMO (status {st}) ---", flush=True)
    print(json.dumps(resumo, ensure_ascii=False, indent=2), flush=True)

    # 2) DETALHE de CADA promoção (id + type)
    if isinstance(resumo, list):
        for p in resumo:
            pid = p.get("id"); ptype = p.get("type")
            nome = p.get("name")
            print(f"\n--- DETALHE promo '{nome}' id={pid} type={ptype} ---", flush=True)
            path = f"/seller-promotions/items/{ITEM_ID}?app_version=v2"
            if pid and ptype:
                path += f"&promotion_id={pid}&promotion_type={ptype}"
            st, det = get(path, access)
            print(f"(status {st})", flush=True)
            print(json.dumps(det, ensure_ascii=False, indent=2), flush=True)
            time.sleep(0.2)

    print("\n=== diagnostico concluido (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
