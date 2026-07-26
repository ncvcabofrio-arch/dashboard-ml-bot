"""
DIAGNÓSTICO dos PAUSADOS — mostra de onde vêm os anúncios com status=paused no ML.
Uso:  SELLER_ID=177795203  python diag_pausados.py
Só leitura. Não escreve nada no ML nem no Supabase.
"""
import os
from collections import Counter
import repricer_sugestoes as rec
from ml_auth import obter_access

SELLER = (os.environ.get("SELLER_ID") or "177795203").strip()

def multiget(ids, access, attrs="id,status,sub_status,seller_sku,seller_custom_field,available_quantity,catalog_listing"):
    """Detalhe de vários itens de uma vez (lotes de 20 — limite do ML)."""
    out = {}
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        st, d = rec.get(f"/items?ids={','.join(chunk)}&attributes={attrs}", access)
        if isinstance(d, list):
            for r in d:
                b = r.get("body") if isinstance(r, dict) else None
                if isinstance(b, dict) and b.get("id"):
                    out[b["id"]] = b
    return out

def main():
    access = None
    for sid, refresh in rec.contas():
        a, s, r = obter_access(rec.sb, sid, refresh)
        if str(s) == str(SELLER):
            access = a
            break
    if not access:
        print(f"não autentiquei a conta {SELLER}")
        return

    ids = rec.pausados_ids(SELLER, access)
    print(f"===== PAUSADOS da conta {SELLER} =====")
    print(f"Total com status=paused no ML: {len(ids)}")
    if not ids:
        return

    det = multiget(ids, access)
    print(f"Detalhe obtido de {len(det)}/{len(ids)} itens")

    sub = Counter()
    com_sku = 0
    for iid in ids:
        b = det.get(iid, {})
        ss = b.get("sub_status") or ["(sem sub_status)"]
        for s in ss:
            sub[s] += 1
        if b.get("seller_sku") or b.get("seller_custom_field"):
            com_sku += 1

    print("\n-- Por sub_status (o motivo REAL do 'paused') --")
    for k, v in sub.most_common():
        print(f"   {k}: {v}")

    sem_estoque = sum(1 for iid in ids if "out_of_stock" in (det.get(iid, {}).get("sub_status") or []))
    reais = len(ids) - sem_estoque
    print(f"\n-- Resumo --")
    print(f"   Com SKU: {com_sku}   |   Sem SKU: {len(ids) - com_sku}")
    print(f"   SEM ESTOQUE (out_of_stock): {sem_estoque}")
    print(f"   Pausa 'de verdade' (paused SEM out_of_stock): {reais}")

    # Cruzamento com o que o robô já viu ativo (repricer_log)
    try:
        vistos = set()
        r = (rec.sb.table("repricer_log").select("item_id")
             .eq("seller_id", str(SELLER)).limit(30000).execute().data) or []
        for x in r:
            vistos.add(x["item_id"])
        no_log = sum(1 for iid in ids if iid in vistos)
        print(f"   Já vistos pelo robô (em repricer_log): {no_log}   |   Nunca vistos: {len(ids) - no_log}")
    except Exception as e:
        print(f"   (cruzamento com repricer_log falhou: {e})")

    print("\n-- Amostra (15) --")
    for iid in ids[:15]:
        b = det.get(iid, {})
        print(f"   {iid}  sku={b.get('seller_sku') or b.get('seller_custom_field') or '—'}  "
              f"sub_status={b.get('sub_status')}  aq={b.get('available_quantity')}  "
              f"catalog={b.get('catalog_listing')}")

if __name__ == "__main__":
    main()
