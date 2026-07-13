"""
Sonda: achar o campo "VOCÊ RECEBE" das promoções — SOMENTE LEITURA.
Para poucos itens ativos com promoção compartilhada candidata, despeja o JSON
COMPLETO do detalhe e tenta o endpoint de oferta (offer_id/ref_id), procurando
o valor líquido que o painel mostra como "Você recebe". Nada é aplicado.
"""
import os
import time
import json
import re
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
SEED = os.environ.get("ML_REFRESH_TOKEN", "")
MAX_ITENS = int(os.environ.get("MAX_ITENS", "3"))


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
        st, busca = get(f"/users/{sid}/items/search?status=active&limit=25", access)
        ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []

        achei = 0
        for item_id in ids:
            st, resumo = get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
            if not isinstance(resumo, list) or not resumo:
                continue
            comp = [p for p in resumo if isinstance(p, dict) and p.get("meli_percentage") and p.get("status") == "candidate"]
            if not comp:
                continue

            pid, ptype = resumo[0].get("id"), resumo[0].get("type")
            st, det = get(f"/seller-promotions/items/{item_id}?app_version=v2&promotion_id={pid}&promotion_type={ptype}", access)
            print(f"\n### item {item_id} — DETALHE COMPLETO (sem cortar):", flush=True)
            print(json.dumps(det, ensure_ascii=False, indent=1)[:2500], flush=True)

            # tenta o endpoint de oferta com o ref_id / número
            alvo = None
            if isinstance(det, list):
                for o in det:
                    if isinstance(o, dict) and o.get("meli_percentage"):
                        alvo = o; break
            if alvo:
                ref = str(alvo.get("ref_id") or "")
                num = re.findall(r"(\d{6,})", ref)
                tentativas = []
                if ref: tentativas.append(ref)
                if num: tentativas.append(num[-1])
                for oid in tentativas:
                    for base in (f"/seller-promotions/offers/{oid}?app_version=v2",
                                 f"/seller-promotions/offers/{oid}"):
                        st, off = get(base, access)
                        print(f"  offers[{oid}] {base} -> status {st}: {json.dumps(off, ensure_ascii=False)[:700]}", flush=True)

            achei += 1
            if achei >= MAX_ITENS:
                break
        if achei:
            break  # uma conta com resultado já basta pra investigar

    print("\n=== sonda concluída (nada foi alterado) ===", flush=True)


if __name__ == "__main__":
    main()
