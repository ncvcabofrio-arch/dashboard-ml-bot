"""
Sonda de PROMOÇÕES (Central de Promoções) — SOMENTE LEITURA.
Mostra, por conta e por item, quais promoções o Mercado Livre oferece agora —
destacando as COMPARTILHADAS (onde o ML banca parte do desconto).
Nada é aplicado nem alterado.
"""
import os
import time
import json
import requests
from supabase import create_client
from ml_auth import obter_access  # mesma autenticação dos robôs

API = "https://api.mercadolibre.com"
AMOSTRA = int(os.environ.get("AMOSTRA", "10"))
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


def resumo_promos(pr):
    """Extrai um resumo legível das promoções de um item (formato pode variar)."""
    out = []
    lista = pr if isinstance(pr, list) else (pr.get("results") if isinstance(pr, dict) else None)
    for p in (lista or []):
        if not isinstance(p, dict):
            continue
        out.append({
            "type": p.get("type"),
            "status": p.get("status"),
            "id": p.get("id"),
            "offer_id": p.get("offer_id"),
            # sinais de custo compartilhado (o ML banca parte):
            "meli_perc": p.get("meli_percentage"),
            "rebate_meli": p.get("rebate_meli_percentage") or p.get("rebate_meli"),
            "benefits": p.get("benefits"),
        })
    return out


def main():
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        print(f"\n===== CONTA {sid} =====", flush=True)

        # 1) campanhas/ofertas disponíveis pra conta inteira
        st, u = get(f"/seller-promotions/users/{sid}?app_version=v2", access)
        print(f"[campanhas da conta] status {st}:", json.dumps(u, ensure_ascii=False)[:1500], flush=True)

        # 2) promoções disponíveis por item (amostra)
        st, busca = get(f"/users/{sid}/items/search?limit={AMOSTRA}", access)
        ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []
        print(f"[itens] {len(ids)} na amostra (status {st})", flush=True)
        for k, item_id in enumerate(ids):
            st, pr = get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
            print(f"- {item_id} (status {st}): {json.dumps(resumo_promos(pr), ensure_ascii=False)[:500]}", flush=True)
            if k == 0:
                print("   (JSON cru do 1o item):", json.dumps(pr, ensure_ascii=False)[:1100], flush=True)
            time.sleep(0.2)

    print("\n=== sonda de promoções concluída (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
