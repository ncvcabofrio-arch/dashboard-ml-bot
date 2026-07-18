"""
DIAGNÓSTICO COMPLETO DE PROMOÇÕES DE UM ANÚNCIO (somente leitura — não altera NADA).
Roda no GitHub Actions (lá tem o token OAuth) e despeja no LOG tudo que a API de
promoções sabe sobre um item, usando TODOS os GETs disponíveis:

  1) /items/{id}                                   -> dados do anúncio (preço, estoque, sku...)
  2) /seller-promotions/items/{id}                 -> MAPA do item: toda promoção ligada a ele
                                                      com o status DELE (candidate/started/pending),
                                                      ref_id (OFFER-...), preços e faixas.
  3) /seller-promotions/users/{seller}             -> TODAS as campanhas da conta (id/tipo/status).
  4) /seller-promotions/promotions/{pid}/items?item_id=...   -> participação REAL do item em cada
       (+ &status_item=active e + /promotions/{pid} detalhe)   campanha: status, offer_id, datas,
                                                      preços min/max/sugerido + detalhe da campanha.

Uso (inputs do workflow): ITEM_ID (obrigatório) e SELLER_ID (conta). Lê tudo, imprime tudo.
"""
import os
import json
import repricer_sugestoes as rec
from ml_auth import obter_access
sb = rec.sb
ITEM = (os.environ.get("ITEM_ID") or os.environ.get("DIAG_ITEM") or "").strip()
SELLER = (os.environ.get("SELLER_ID") or "").strip()
# Forma do DELETE por tipo (confirmado na doc de CADA campanha):
TIPOS_SO_TIPO = {"PRICE_DISCOUNT", "LIGHTNING", "DOD"}                         # só promotion_type
TIPOS_COM_OFFER = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "MARKETPLACE_CAMPAIGN",
                   "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "VOLUME"}             # type+id+offer_id
def como_sair(iid, ptipo, pid, offer_id, item_status):
    """Descreve a forma EXATA de sair dessa promoção (o DELETE que o robô montaria)."""
    t = (ptipo or "").upper()
    if t in ("LIGHTNING", "DOD") and (item_status or "").lower() == "started":
        return f"⚠️ {t} ATIVA não sai por API (só pausando o anúncio)"
    if t in TIPOS_SO_TIPO:
        return f"DELETE /items/{iid}?promotion_type={t}&app_version=v2"
    base = f"DELETE /items/{iid}?promotion_type={t}&promotion_id={pid}&app_version=v2"
    if t in TIPOS_COM_OFFER:
        return base + f"&offer_id={offer_id or '??FALTA_OFFER_ID??'}"
    return base
def dump(label, obj, corte=6000):
    print(f"\n===== {label} =====", flush=True)
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:corte], flush=True)
    except Exception:
        print(str(obj)[:corte], flush=True)
def main():
    if not ITEM:
        print("!! defina ITEM_ID (o anúncio MLB... a diagnosticar)", flush=True)
        return
    # resolve o token da conta certa
    access, SID = None, None
    for seller_id, refresh in rec.contas():
        try:
            a, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"  token de {seller_id} falhou: {e}", flush=True)
            continue
        if not SELLER or str(sid) == SELLER:
            access, SID = a, str(sid)
            if SELLER:
                break
    if not access:
        print("!! não consegui token de nenhuma conta", flush=True)
        return
    print(f"################ DIAGNÓSTICO {ITEM} — conta {SID} ################", flush=True)
    # 1) o anúncio
    st, it = rec.get(f"/items/{ITEM}?attributes=id,title,price,base_price,status,"
                     f"listing_type_id,category_id,seller_sku,seller_custom_field,available_quantity", access)
    dump("1) ANÚNCIO (/items/{id})", it, 2000)
    # 2) mapa do item
    st, of = rec.get(f"/seller-promotions/items/{ITEM}?app_version=v2", access)
    dump("2) PROMOÇÕES DO ITEM (/seller-promotions/items) — 'status: started' = PARTICIPANDO", of)
    # 3) todas as campanhas do vendedor
    todas = rec.promocoes_do_vendedor(SID, access)
    print(f"\n===== 3) CAMPANHAS DA CONTA: {len(todas)} =====", flush=True)
    for p in todas:
        print(f"  {(p.get('name') or '?')} [{p.get('type')}/{p.get('status')}/{p.get('id')}]", flush=True)
    # 4) participação do item em cada campanha started/pending + detalhe da campanha
    print("\n===== 4) PARTICIPAÇÃO DO ITEM (por campanha started/pending) =====", flush=True)
    achou = 0
    for p in todas:
        if (p.get("status") or "").lower() not in ("started", "pending"):
            continue
        pid, tipo = p.get("id"), (p.get("type") or "")
        st, d = rec.get(f"/seller-promotions/promotions/{pid}/items"
                        f"?promotion_type={tipo}&item_id={ITEM}&app_version=v2", access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        mine = [x for x in res if str(x.get("id")) == str(ITEM)]
        if not mine:
            continue
        achou += 1
        st2, d2 = rec.get(f"/seller-promotions/promotions/{pid}/items"
                          f"?promotion_type={tipo}&item_id={ITEM}&status_item=active&app_version=v2", access)
        act = (d2.get("results") if isinstance(d2, dict) else None)
        actY = ("sim" if (act and any(str(x.get('id')) == str(ITEM) for x in act))
                else ("400/erro" if act is None else "não"))
        st3, det = rec.get(f"/seller-promotions/promotions/{pid}?promotion_type={tipo}&app_version=v2", access)
        it0 = mine[0]
        istatus = (it0.get("status") or "")
        oid = it0.get("offer_id")
        print(f"\n  >>> {(p.get('name') or '?')}  [{tipo} / {pid}]   item_status={istatus}   status_item=active devolve? {actY}", flush=True)
        print(f"      ITEM na campanha: {json.dumps(it0, ensure_ascii=False)}", flush=True)
        print(f"      DETALHE campanha: {json.dumps(det if isinstance(det, dict) else {}, ensure_ascii=False)}", flush=True)
        if istatus.lower() == "started":
            print(f"      COMO SAIR:        {como_sair(ITEM, tipo, pid, oid, istatus)}", flush=True)
        else:
            print(f"      (item aqui é '{istatus}', não é participação ativa — não precisa sair)", flush=True)
    if not achou:
        print("  (o item não apareceu como participante em nenhuma campanha started/pending)", flush=True)
    print("\n################ FIM — nada foi alterado (só leitura) ################", flush=True)
if __name__ == "__main__":
    main()
