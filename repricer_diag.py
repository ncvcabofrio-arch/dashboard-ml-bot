"""
DIAGNÓSTICO COMPLETO DE PROMOÇÕES DE UM ANÚNCIO (somente leitura — não altera NADA).
Roda no GitHub Actions (lá tem o token OAuth) e despeja TUDO no LOG, usando todos os GETs.
Esta versão SONDA cada campanha por várias vias (sem filtro, status=started, status=candidate,
e PAGINANDO a lista inteira) pra descobrir se a participação 'started' está sendo perdida por
paginação ou por falta de filtro.
Uso (inputs do workflow): ITEM_ID (obrigatório) e SELLER_ID (conta).
"""
import os
import json
import repricer_sugestoes as rec
from ml_auth import obter_access
sb = rec.sb
ITEM = (os.environ.get("ITEM_ID") or os.environ.get("DIAG_ITEM") or "").strip()
SELLER = (os.environ.get("SELLER_ID") or "").strip()
TIPOS_SO_TIPO = {"PRICE_DISCOUNT", "LIGHTNING", "DOD"}
TIPOS_COM_OFFER = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "MARKETPLACE_CAMPAIGN",
                   "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "VOLUME"}
def como_sair(iid, ptipo, pid, offer_id, item_status):
    t = (ptipo or "").upper()
    if t in ("LIGHTNING", "DOD") and (item_status or "").lower() == "started":
        return f"⚠️ {t} ATIVA não sai por API (só pausando o anúncio)"
    if t in TIPOS_SO_TIPO:
        return f"DELETE /items/{iid}?promotion_type={t}"
    base = f"DELETE /items/{iid}?promotion_type={t}&promotion_id={pid}"
    if t in TIPOS_COM_OFFER:
        return base + f"&offer_id={offer_id or '??FALTA_OFFER_ID??'}"
    return base
def _achar(res, iid):
    for x in (res or []):
        if str(x.get("id")) == str(iid):
            return x
    return None
def sonda_campanha(pid, tipo, iid, access):
    """Procura o item na campanha por VÁRIAS vias. Retorna dict {via: (status, offer_id)}
    e, se as vias com item_id não acharem 'started', PAGINA a lista inteira procurando."""
    out = {}
    base = f"/seller-promotions/promotions/{pid}/items?promotion_type={tipo}&item_id={iid}&app_version=v2"
    for nome, extra in (("item_id", ""), ("+started", "&status=started"),
                        ("+candidate", "&status=candidate"), ("+active", "&status_item=active")):
        st, d = rec.get(base + extra, access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        it = _achar(res, iid)
        out[nome] = (it.get("status"), it.get("offer_id")) if it else (None, None)
    achou_started = any(v[0] == "started" for v in out.values())
    if not achou_started:
        # PAGINAÇÃO: varre a lista TODA (sem item_id) procurando o item, até 15 páginas
        sa, npag, achado = None, 0, None
        for _ in range(15):
            npag += 1
            url = f"/seller-promotions/promotions/{pid}/items?promotion_type={tipo}&app_version=v2&limit=50"
            if sa:
                url += f"&search_after={sa}"
            st, d = rec.get(url, access)
            if not isinstance(d, dict):
                break
            res = d.get("results") or []
            it = _achar(res, iid)
            if it:
                achado = (it.get("status"), it.get("offer_id"), npag)
                break
            pag = d.get("paging") or {}
            sa = d.get("search_after") or pag.get("search_after") or pag.get("searchAfter")
            if not sa:
                break
        out["PAGINANDO"] = (f"{achado[0]} (pág {achado[2]}, offer={achado[1]})" if achado
                            else f"não achou em {npag} pág", None)
    return out
def dump(label, obj, corte=6000):
    print(f"\n===== {label} =====", flush=True)
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:corte], flush=True)
    except Exception:
        print(str(obj)[:corte], flush=True)
def main():
    if not ITEM:
        print("!! defina ITEM_ID", flush=True)
        return
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
        print("!! não consegui token", flush=True)
        return
    print(f"################ DIAGNÓSTICO {ITEM} — conta {SID} ################", flush=True)
    # 1) o anúncio
    st, it = rec.get(f"/items/{ITEM}?attributes=id,title,price,base_price,status,"
                     f"listing_type_id,category_id,seller_sku,seller_custom_field,available_quantity", access)
    dump("1) ANÚNCIO (/items/{id})", it, 2000)
    # 2) mapa do item — CONTANDO quantos vêm (pra ver se está truncado/paginado)
    st, of = rec.get(f"/seller-promotions/items/{ITEM}?app_version=v2", access)
    n = len(of) if isinstance(of, list) else "?"
    print(f"\n===== 2) /seller-promotions/items/{{id}} — devolveu {n} promoções (started = participando) =====", flush=True)
    dump("   conteúdo", of)
    # 3) todas as campanhas do vendedor
    todas = rec.promocoes_do_vendedor(SID, access)
    print(f"\n===== 3) CAMPANHAS DA CONTA: {len(todas)} =====", flush=True)
    for p in todas:
        print(f"  {(p.get('name') or '?')} [{p.get('type')}/{p.get('status')}/{p.get('id')}]", flush=True)
    # 4) SONDA a participação do item em CADA campanha started/pending, por várias vias + paginação
    print("\n===== 4) SONDA DA PARTICIPAÇÃO (item_id x status=started x candidate x active x PAGINANDO) =====", flush=True)
    ativas_reais = []
    for p in todas:
        if (p.get("status") or "").lower() not in ("started", "pending"):
            continue
        pid, tipo = p.get("id"), (p.get("type") or "")
        vias = sonda_campanha(pid, tipo, ITEM, access)
        # só imprime se apareceu em ALGUMA via
        apareceu = any(v[0] for v in vias.values())
        if not apareceu:
            continue
        resumo = " | ".join(f"{k}={v[0]}" for k, v in vias.items())
        print(f"\n  >>> {(p.get('name') or '?')} [{tipo}/{pid}]", flush=True)
        print(f"      vias: {resumo}", flush=True)
        # se alguma via disse 'started', é participação REAL -> mostra como sair
        started_via = next((v for v in vias.values() if v[0] == "started"), None)
        if started_via:
            oid = started_via[1]
            ativas_reais.append((p.get("name"), tipo, pid, oid))
            print(f"      >>> PARTICIPAÇÃO ATIVA (started). COMO SAIR: {como_sair(ITEM, tipo, pid, oid, 'started')}", flush=True)
    print("\n===== RESUMO: participações ATIVAS (started) encontradas =====", flush=True)
    if ativas_reais:
        for nome, tipo, pid, oid in ativas_reais:
            print(f"  ✓ {nome} [{tipo}/{pid}] offer={oid}", flush=True)
    else:
        print("  (nenhuma via encontrou o item como 'started')", flush=True)
    print("\n################ FIM — nada foi alterado (só leitura) ################", flush=True)
if __name__ == "__main__":
    main()
