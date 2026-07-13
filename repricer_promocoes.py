"""
Confere o campo de FRETE do anúncio contra o painel — SOMENTE LEITURA.
Acha o teclado Casio CT-X5000 (painel mostrou Custo de envio R$70,25) e imprime
base_cost e list_cost da opção de frete grátis, pra a gente travar qual campo usar.
Se não achar o teclado, imprime os primeiros itens ativos como referência.
"""
import os
import time
import json
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
CEP = os.environ.get("CEP", "01310100")
# item do teclado CT-X5000 (painel mostrou Custo de envio R$70,25 / Você recebe R$1.881,17)
ITEM_ID = os.environ.get("ITEM_ID", "MLB3923203247")
ALVO = os.environ.get("ALVO", "CTX5000,CT-X5000,CT X5000,CTX 5000").split(",")
MAX_ITENS = int(os.environ.get("MAX_ITENS", "120"))
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


def frete_campos(item_id, access):
    st, so = get(f"/items/{item_id}/shipping_options?zip_code={CEP}", access)
    opts = so.get("options") if isinstance(so, dict) else None
    if not isinstance(opts, list) or not opts:
        return None
    gratis = [o for o in opts if float(o.get("cost") or 0) == 0]
    escolha = gratis or opts
    o = min(escolha, key=lambda x: float(x.get("list_cost") or x.get("base_cost") or 1e9))
    return {
        "name": o.get("name"),
        "cost": o.get("cost"),
        "list_cost": o.get("list_cost"),
        "base_cost": o.get("base_cost"),
        "n_opcoes": len(opts),
    }


def bate(titulo):
    t = (titulo or "").upper().replace("-", "").replace(" ", "")
    for a in ALVO:
        if a.upper().replace("-", "").replace(" ", "") in t:
            return True
    return False


def mostra(item_id, access, sid):
    st, it = get(f"/items/{item_id}", access)
    if not isinstance(it, dict) or it.get("id") is None:
        return False
    titulo = it.get("title") or ""
    preco = it.get("price")
    fc = frete_campos(item_id, access)
    print(f"\n>>> {item_id} | R${preco} | {titulo} (conta {sid})", flush=True)
    print(f"    frete: {json.dumps(fc, ensure_ascii=False)}", flush=True)
    print(f"    painel esperava: Custo de envio = R$70,25", flush=True)
    return True


def main():
    achou = False
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        print(f"\n===== CONTA {sid} =====", flush=True)

        # 1) tenta o item exato do teclado direto (mais rápido e certeiro)
        if ITEM_ID and mostra(ITEM_ID, access, sid):
            achou = True

        # 2) e também varre por título, caso o ITEM_ID seja de outra conta
        st, busca = get(f"/users/{sid}/items/search?status=active&limit={MAX_ITENS}", access)
        ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []
        for item_id in ids:
            if item_id == ITEM_ID:
                continue
            st, it = get(f"/items/{item_id}", access)
            titulo = it.get("title") if isinstance(it, dict) else ""
            if not bate(titulo):
                continue
            preco = it.get("price")
            fc = frete_campos(item_id, access)
            print(f"\n>>> TECLADO ENCONTRADO: {item_id} | R${preco} | {titulo}", flush=True)
            print(f"    frete: {json.dumps(fc, ensure_ascii=False)}", flush=True)
            print(f"    painel esperava: Custo de envio = R$70,25", flush=True)
            achou = True
            time.sleep(0.2)
    if not achou:
        print("\n(nao achei o teclado nos itens ativos; ajuste ALVO ou aumente MAX_ITENS)", flush=True)
    print("\n=== conferencia concluida (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
