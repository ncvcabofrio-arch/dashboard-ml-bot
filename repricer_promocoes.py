"""
SONDA do repricer — SOMENTE LEITURA. Não altera nada no Mercado Livre.
Escolha o que fazer pelo menu do "Run workflow" (nada de editar código):

  MODO = promocoes  -> lista TODAS as promoções do item (status/preço/percentuais)
         frete      -> custo de envio do anúncio (base_cost / list_cost)
         item       -> dados crus do anúncio (preço, categoria, sku, envio)

  ITEM_ID = o anúncio (ex.: MLB4126262466)
"""
import os
import json
import time
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
MODO = (os.environ.get("MODO") or "promocoes").strip().lower()
ITEM_ID = (os.environ.get("ITEM_ID") or "").strip()
CEP = os.environ.get("CEP", "01310100")
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


def frete_campos(item_id, access):
    st, so = get(f"/items/{item_id}/shipping_options?zip_code={CEP}", access)
    opts = so.get("options") if isinstance(so, dict) else None
    if not isinstance(opts, list) or not opts:
        return None
    gratis = [o for o in opts if float(o.get("cost") or 0) == 0]
    escolha = gratis or opts
    o = min(escolha, key=lambda x: float(x.get("list_cost") or x.get("base_cost") or 1e9))
    return {"name": o.get("name"), "cost": o.get("cost"),
            "list_cost": o.get("list_cost"), "base_cost": o.get("base_cost"), "n_opcoes": len(opts)}


def modo_promocoes(item_id, access, it):
    st, resumo = get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
    if not isinstance(resumo, list):
        print(f"resposta inesperada (status {st}): {resumo}", flush=True)
        return
    print(f"\npromoções encontradas: {len(resumo)}", flush=True)
    print("-" * 92, flush=True)
    print(f"{'STATUS':<12} {'TIPO':<14} {'PREÇO':>9} {'ORIG':>9} {'SELLER%':>8} {'MELI%':>7}  NOME", flush=True)
    print("-" * 92, flush=True)
    for p in resumo:
        print(
            f"{str(p.get('status')):<12} {str(p.get('type')):<14} "
            f"{str(p.get('price')):>9} {str(p.get('original_price')):>9} "
            f"{str(p.get('seller_percentage')):>8} {str(p.get('meli_percentage')):>7}  "
            f"{p.get('name') or ''}",
            flush=True,
        )
    print("\n--- JSON cru (completo) ---", flush=True)
    print(json.dumps(resumo, ensure_ascii=False, indent=2), flush=True)


def modo_frete(item_id, access, it):
    print(f"\npreço do anúncio: {it.get('price')}", flush=True)
    print(f"frete (CEP {CEP}): {json.dumps(frete_campos(item_id, access), ensure_ascii=False)}", flush=True)


def modo_item(item_id, access, it):
    campos = ("id", "title", "price", "original_price", "available_quantity",
              "sold_quantity", "status", "listing_type_id", "category_id",
              "seller_sku", "seller_custom_field")
    print("\n" + json.dumps({k: it.get(k) for k in campos}, ensure_ascii=False, indent=2), flush=True)
    print(f"shipping: {json.dumps(it.get('shipping'), ensure_ascii=False)}", flush=True)
    print(f"variations: {len(it.get('variations') or [])}", flush=True)


MODOS = {"promocoes": modo_promocoes, "frete": modo_frete, "item": modo_item}


def main():
    if not ITEM_ID:
        print("Preencha o ITEM_ID no 'Run workflow'.", flush=True)
        return
    fn = MODOS.get(MODO)
    if not fn:
        print(f"MODO '{MODO}' inválido. Use: {', '.join(MODOS)}", flush=True)
        return
    access, sid, it = dono_do_item(ITEM_ID, contas())
    if not access:
        print(f"não achei o item {ITEM_ID} em nenhuma conta.", flush=True)
        return
    print(f"===== SONDA [{MODO}] | {ITEM_ID} | conta {sid} =====", flush=True)
    print(f"title: {it.get('title')}", flush=True)
    fn(ITEM_ID, access, it)
    print("\n=== sonda concluída (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
