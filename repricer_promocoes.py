"""
Sonda de FRETE pelo anúncio — SOMENTE LEITURA.
Para alguns itens ativos, tenta pegar o custo de envio (o que VOCÊ paga no frete
grátis) direto do anúncio, via /items/{id}/shipping_options com um CEP de destino.
Despeja o JSON pra a gente achar o campo que corresponde ao "Custo de envio" do painel.
"""
import os
import time
import json
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
CEP = os.environ.get("CEP", "01310100")   # Av. Paulista, SP (destino de referência)
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
SEED = os.environ.get("ML_REFRESH_TOKEN", "")
MAX_ITENS = int(os.environ.get("MAX_ITENS", "6"))


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


def main():
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        print(f"\n===== CONTA {sid} =====", flush=True)
        st, busca = get(f"/users/{sid}/items/search?status=active&limit={MAX_ITENS}", access)
        ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []

        for item_id in ids:
            st, it = get(f"/items/{item_id}", access)
            preco = it.get("price") if isinstance(it, dict) else None
            titulo = (it.get("title") or "")[:40] if isinstance(it, dict) else ""
            ship = it.get("shipping") if isinstance(it, dict) else None
            print(f"\n### {item_id} | R${preco} | {titulo}", flush=True)
            print(f"   shipping(item): {json.dumps(ship, ensure_ascii=False)[:200]}", flush=True)

            # 1) opções de envio com CEP de destino (traz custos)
            st1, so = get(f"/items/{item_id}/shipping_options?zip_code={CEP}", access)
            print(f"   shipping_options?zip={CEP} (status {st1}): {json.dumps(so, ensure_ascii=False)[:900]}", flush=True)

            # 2) frete grátis do site pra esse item (custo do vendedor)
            st2, fr = get(f"/sites/MLB/shipping_options/free?item_id={item_id}&zip_code={CEP}", access)
            print(f"   shipping_options/free (status {st2}): {json.dumps(fr, ensure_ascii=False)[:500]}", flush=True)

            time.sleep(0.3)
        break  # uma conta já basta pra achar o campo

    print("\n=== sonda de frete concluída (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
