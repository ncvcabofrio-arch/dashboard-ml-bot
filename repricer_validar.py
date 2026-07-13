"""
Fase 0 do repricer — VALIDAÇÃO (SOMENTE LEITURA — não muda preço de nada).
Reaproveita a autenticação do projeto (ml_auth.obter_access), igual aos robôs.

Para uma amostra de anúncios de cada conta, mostra:
  - preço atual, tipo de anúncio, categoria
  - custo (de produtos), comissão (sale_fee via API) e frete estimado (histórico)
  - status de catálogo e "preço pra ganhar" (price_to_win), quando for catálogo
  - o PISO calculado pela regra de margem mínima (padrão 18%)

Objetivo: conferir na vida real se a conta de margem fecha e se conseguimos
enxergar os concorrentes. Nada é alterado no Mercado Livre.
"""
import os
import time
import json
import requests
from supabase import create_client
from ml_auth import obter_access  # mesma autenticação dos robôs

API = "https://api.mercadolibre.com"
SITE = os.environ.get("ML_SITE", "MLB")
MARGEM_MIN = float(os.environ.get("MARGEM_MIN", "18"))   # % líquido sobre o preço
AMOSTRA = int(os.environ.get("AMOSTRA", "5"))            # itens por conta
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get(path, access, tentativas=3):
    r = None
    for i in range(tentativas):
        r = requests.get(API + path, headers={"Authorization": "Bearer " + access}, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": (r.text or "")[:300]}


def lista_contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    contas = [(c["seller_id"], c.get("refresh_token"))
              for c in (res.data or []) if c.get("refresh_token")]
    if not contas and SEED_REFRESH:
        contas = [(None, SEED_REFRESH)]
    return contas


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


def frete_hist_de(item_id):
    """Frete médio histórico desse anúncio (da tabela vendas), como estimativa."""
    if not item_id:
        return None
    try:
        r = (sb.table("vendas").select("frete")
             .eq("item_id", item_id).not_.is_("frete", "null")
             .limit(20).execute().data)
        fretes = [float(x["frete"]) for x in (r or []) if x.get("frete") is not None]
        if fretes:
            return round(sum(fretes) / len(fretes), 2)
    except Exception:
        pass
    return None


def comissao_para_preco(preco, category_id, listing_type_id, access):
    """sale_fee do ML para um preço/categoria/tipo (endpoint listing_prices)."""
    path = f"/sites/{SITE}/listing_prices?price={preco}"
    if listing_type_id:
        path += f"&listing_type_id={listing_type_id}"
    if category_id:
        path += f"&category_id={category_id}"
    st, d = get(path, access)
    if isinstance(d, list) and d:
        d = d[0]
    sale_fee = d.get("sale_fee_amount") if isinstance(d, dict) else None
    return st, sale_fee, d


def price_to_win(item_id, access):
    for suf in ("?version=v2", ""):
        st, d = get(f"/items/{item_id}/price_to_win{suf}", access)
        if st == 200 and isinstance(d, dict):
            return st, d
    return st, d


def piso(custo, frete, comissao_pct):
    """Preço mínimo pra sobrar MARGEM_MIN% depois de comissão e frete.
       preço*(1 - com% - marg%) = custo + frete  ->  preço = (custo+frete)/(1-com%-marg%)."""
    denom = 1 - comissao_pct - (MARGEM_MIN / 100.0)
    if denom <= 0:
        return None
    return round((custo + frete) / denom, 2)


def validar_conta(seller_id, refresh):
    access, sid, refresh = obter_access(sb, seller_id, refresh)
    print(f"\n================ CONTA {sid} ================", flush=True)

    st, busca = get(f"/users/{sid}/items/search?limit={AMOSTRA}", access)
    ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []
    print(f"itens encontrados: {len(ids)} (status busca {st})", flush=True)

    for n, item_id in enumerate(ids):
        st, it = get(f"/items/{item_id}", access)
        if not isinstance(it, dict):
            print(f"- {item_id}: falha ao ler item (status {st})", flush=True)
            continue

        preco = it.get("price")
        ltid = it.get("listing_type_id")
        cat = it.get("category_id")
        sku = it.get("seller_sku") or it.get("seller_custom_field")
        eh_catalogo = bool(it.get("catalog_listing"))
        cat_prod = it.get("catalog_product_id")

        custo = custo_de(sku)
        frete = frete_hist_de(item_id) or 0.0
        st_c, sale_fee, raw_com = comissao_para_preco(preco, cat, ltid, access)
        com_pct = (sale_fee / preco) if (sale_fee and preco) else None

        print(f"\n- {item_id} | {(it.get('title') or '')[:50]}", flush=True)
        print(f"    SKU={sku}  preço=R${preco}  tipo={ltid}  cat={cat}  catálogo={eh_catalogo}", flush=True)
        print(f"    custo={custo}  frete_hist={frete}  sale_fee={sale_fee} ({(com_pct*100 if com_pct else 0):.2f}%)", flush=True)

        if custo is not None and com_pct is not None:
            p = piso(custo, frete, com_pct)
            print(f"    >>> PISO (margem {MARGEM_MIN:.0f}%) = R${p}", flush=True)
            if preco is not None and p is not None:
                folga = preco - p
                print(f"    >>> folga até o piso = R${folga:.2f}", flush=True)
        else:
            print("    >>> sem custo cadastrado ou comissão indisponível — piso não calculado", flush=True)

        if eh_catalogo:
            stw, ptw = price_to_win(item_id, access)
            print(f"    catálogo: status_ptw={stw}  produto={cat_prod}", flush=True)
            if n == 0:  # imprime o JSON cru só do primeiro, pra vermos os campos reais
                print("    price_to_win (JSON cru):", json.dumps(ptw, ensure_ascii=False)[:600], flush=True)

        if n == 0:  # idem pra comissão
            print("    listing_prices (JSON cru):", json.dumps(raw_com, ensure_ascii=False)[:500], flush=True)

        time.sleep(0.3)


def main():
    contas = lista_contas()
    if not contas:
        raise SystemExit("Nenhuma conta com refresh_token.")
    so = os.environ.get("SO_SELLER", "").strip()
    for seller_id, refresh in contas:
        if so and str(seller_id) != so:
            continue
        try:
            validar_conta(seller_id, refresh)
        except Exception as e:
            print(f"Erro na conta {seller_id}: {e}", flush=True)
    print("\n=== validação concluída (nada foi alterado no Mercado Livre) ===", flush=True)


if __name__ == "__main__":
    main()
