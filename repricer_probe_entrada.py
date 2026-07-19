"""
PROBE DE ENTRADA — tenta ENTRAR de verdade numa cofinanciada (SMART/PRICE_MATCHING) testando
VÁRIOS formatos de POST, NA ORDEM: o DOCUMENTADO (só promotion_id+promotion_type+offer_id, SEM
datas) primeiro; depois variações com datas. PARA no 1º formato que o ML aceitar (200/201) e
loga a resposta EXATA de cada tentativa (pra aprender o que o ML quer).

Aprendizados aplicados aqui:
 • A doc oficial da SMART/PRICE_MATCHING manda SÓ 3 campos (id, type, offer_id) — sem start_date.
 • Os candidatos são atualizados TODO DIA → re-consulto o offer_id do candidato NA HORA (2 fontes).

⚠️ ISTO MEXE NO ANÚNCIO (entra de verdade). Sendo SMART/PRICE_MATCHING é REVERSÍVEL (dá pra sair).
   NÃO sai das outras promoções do item — só testa a ENTRADA nesta.
Inputs (env): ITEM_ID (obrig.), PROMO_ID (obrig.), PROMO_TIPO (default SMART), SELLER_ID,
              SAIR_DEPOIS (0=fica / 1=desfaz na hora, pra teste puro).
"""
import os
import json
import time
import requests
import repricer_sugestoes as rec
from datetime import datetime, timezone, timedelta
from ml_auth import obter_access
API = rec.API
sb = rec.sb
ITEM = (os.environ.get("ITEM_ID") or "").strip()
SELLER = (os.environ.get("SELLER_ID") or "").strip()
PROMO_ID = (os.environ.get("PROMO_ID") or "").strip()
PROMO_TIPO = (os.environ.get("PROMO_TIPO") or "SMART").strip().upper()
SAIR_DEPOIS = (os.environ.get("SAIR_DEPOIS") or "0").strip() == "1"
def post(path, access, body):
    r = requests.post(API + path, headers={"Authorization": "Bearer " + access,
                                           "Content-Type": "application/json"}, json=body, timeout=25)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)
def req_delete(path, access):
    r = requests.delete(API + path, headers={"Authorization": "Bearer " + access}, timeout=25)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)
def offer_id_fresco(item_id, promo_id, tipo, access):
    """RE-consulta o offer_id ATUAL do candidato (muda todo dia), por 2 fontes independentes."""
    # fonte 1: mapa do item
    st, of = rec.get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
    if isinstance(of, list):
        for o in of:
            if o.get("id") == promo_id and (o.get("status") or "").lower() == "candidate":
                oid = o.get("ref_id") or o.get("offer_id")
                if oid:
                    print(f"  offer_id (mapa do item): {oid}", flush=True)
                    return oid
    # fonte 2: itens da campanha filtrando candidate
    for extra in ("&status=candidate", ""):
        st, d = rec.get(f"/seller-promotions/promotions/{promo_id}/items"
                        f"?promotion_type={tipo}&item_id={item_id}&app_version=v2{extra}", access)
        for it in ((d.get("results") if isinstance(d, dict) else None) or []):
            if str(it.get("id")) == str(item_id):
                oid = it.get("offer_id") or it.get("ref_id")
                print(f"  offer_id (itens da campanha{extra or ' sem filtro'}): {oid} (status={it.get('status')})", flush=True)
                if oid:
                    return oid
    return None
def detalhe_campanha(promo_id, tipo, access):
    st, d = rec.get(f"/seller-promotions/promotions/{promo_id}?promotion_type={tipo}&app_version=v2", access)
    return d if isinstance(d, dict) else {}
def formatos(promo_id, tipo, offer_id, camp):
    """Corpos a tentar, do DOCUMENTADO (sem datas) pro mais defensivo (com datas)."""
    hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d")   # hoje BRT
    ini_c = str(camp.get("start_date") or "")[:10]
    fim_c = str(camp.get("finish_date") or "")[:10]
    ini = hoje if (not ini_c or ini_c < hoje) else ini_c
    base = {"promotion_id": promo_id, "promotion_type": tipo, "offer_id": offer_id}
    fmts = [("A) DOC — só id+type+offer_id (SEM datas) [formato oficial]", dict(base))]
    b2 = dict(base); b2["start_date"] = ini + "T00:00:00"
    fmts.append(("B) + start_date (hoje, local)", b2))
    b3 = dict(b2)
    if fim_c:
        b3["finish_date"] = fim_c + "T23:59:59"
    fmts.append(("C) + start_date + finish_date (local)", b3))
    if ini_c and ini_c != ini:
        b4 = dict(base); b4["start_date"] = ini_c + "T00:00:00"
        if fim_c:
            b4["finish_date"] = fim_c + "T23:59:59"
        fmts.append((f"D) + datas DA CAMPANHA (start {ini_c})", b4))
    return fmts
def main():
    if not ITEM or not PROMO_ID:
        print("!! defina ITEM_ID e PROMO_ID (e PROMO_TIPO, default SMART)", flush=True)
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
    print(f"################ PROBE ENTRADA {ITEM} -> {PROMO_ID} [{PROMO_TIPO}] — conta {SID} ################", flush=True)
    print(f"  (SAIR_DEPOIS={'1 (desfaz na hora)' if SAIR_DEPOIS else '0 (fica)'})", flush=True)
    # 1) offer_id FRESCO do candidato (muda todo dia)
    offer_id = offer_id_fresco(ITEM, PROMO_ID, PROMO_TIPO, access)
    if not offer_id:
        print("  >>> ABORTA: o item NÃO está como candidato nessa promoção agora (candidato expirou? rode a sugestão).", flush=True)
        return
    camp = detalhe_campanha(PROMO_ID, PROMO_TIPO, access)
    print(f"  campanha: status={camp.get('status')} | start={camp.get('start_date')} | finish={camp.get('finish_date')}", flush=True)
    # 2) tenta os formatos, parando no 1º OK
    fmts = formatos(PROMO_ID, PROMO_TIPO, offer_id, camp)
    print(f"\n  Vou tentar {len(fmts)} formato(s), parando no 1º que o ML aceitar:\n", flush=True)
    venceu = None
    for nome, body in fmts:
        sc, resp = post(f"/seller-promotions/items/{ITEM}?app_version=v2", access, body)
        okp = sc in (200, 201)
        print(f"  [{'OK ✓' if okp else ' x '}] {nome}", flush=True)
        print(f"       enviei: {json.dumps(body, ensure_ascii=False)}", flush=True)
        print(f"       ML {sc}: {json.dumps(resp, ensure_ascii=False)[:400]}", flush=True)
        if okp:
            venceu = (nome, body, resp)
            break
        time.sleep(0.6)
    if not venceu:
        print("\n  >>> NENHUM formato entrou. As respostas acima mostram exatamente o que o ML reclamou.", flush=True)
        return
    nome, body, resp = venceu
    print(f"\n  >>> ENTROU ✓ pelo formato: {nome}", flush=True)
    # 3) confirma que virou started
    time.sleep(2)
    stx, d = rec.get(f"/seller-promotions/promotions/{PROMO_ID}/items"
                     f"?promotion_type={PROMO_TIPO}&item_id={ITEM}&app_version=v2", access)
    stt = None
    for it in ((d.get("results") if isinstance(d, dict) else None) or []):
        if str(it.get("id")) == str(ITEM):
            stt = it.get("status")
    print(f"      status do item na promoção agora: {stt}", flush=True)
    # 4) desfaz se for teste puro
    if SAIR_DEPOIS:
        new_offer = (resp.get("offer_id") if isinstance(resp, dict) else None) or offer_id
        dc, dresp = req_delete(f"/seller-promotions/items/{ITEM}?promotion_type={PROMO_TIPO}"
                               f"&promotion_id={PROMO_ID}&offer_id={new_offer}&app_version=v2", access)
        print(f"      DESFEZ (SAIR_DEPOIS=1) DELETE -> {dc}: {json.dumps(dresp, ensure_ascii=False)[:200]}", flush=True)
    else:
        print("      (mantido — o item ENTROU na promoção de verdade)", flush=True)
    print("\n################ FIM ################", flush=True)
if __name__ == "__main__":
    main()
