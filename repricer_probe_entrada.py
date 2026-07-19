"""
PROBE DE ENTRADA v3 — CAÇA A START_DATE + tenta entrar.
Ideia (do Danillo): o problema pode não ser o FORMATO do POST, e sim a CONSULTA — várias
consultas da doc trazem start_date a nível de item; a nossa pode estar vindo vazia porque é
a consulta errada. Então este probe primeiro CONSULTA o anúncio de TODAS as formas possíveis
e mostra onde a data aparece; depois usa essa data (a que ele achar) pra tentar entrar.

Fluxo:
 1) offer_id fresco do candidato.
 2) CAÇA DATAS: bate em ~8 consultas diferentes e imprime start_date/finish_date de cada uma.
 3) Usa a 1ª janela encontrada (ou WIN_START/WIN_FINISH do env) e tenta os formatos de POST,
    parando no 1º que o ML aceitar. Loga tudo.

⚠️ MEXE NO ANÚNCIO ao tentar entrar. Sendo SMART é reversível. Rode com SAIR_DEPOIS=1 pra testar e desfazer.
Inputs (env): ITEM_ID, PROMO_ID (obrig.), PROMO_TIPO (SMART), SELLER_ID, SAIR_DEPOIS(0/1),
              WIN_START, WIN_FINISH (força a janela), SO_CACAR(1 = só consulta datas, NÃO tenta entrar).
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
SO_CACAR = (os.environ.get("SO_CACAR") or "0").strip() == "1"
WIN_START = (os.environ.get("WIN_START") or "").strip()
WIN_FINISH = (os.environ.get("WIN_FINISH") or "").strip()
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
def _datas_de(obj):
    """Extrai qualquer par de datas (start/finish/end) de um dict, em qualquer nome conhecido."""
    if not isinstance(obj, dict):
        return None, None
    s = obj.get("start_date") or obj.get("start_time") or obj.get("start") or (obj.get("dates") or {}).get("start_date")
    f = (obj.get("finish_date") or obj.get("end_date") or obj.get("finish_time") or obj.get("end")
         or (obj.get("dates") or {}).get("finish_date"))
    # às vezes vem dentro de offers[]
    if not s and isinstance(obj.get("offers"), list) and obj["offers"]:
        of0 = obj["offers"][0]
        s = s or of0.get("start_date")
        f = f or of0.get("end_date") or of0.get("finish_date")
    return s, f
def _achar_item(d, item_id):
    res = (d.get("results") if isinstance(d, dict) else None) or (d if isinstance(d, list) else [])
    for it in res:
        if isinstance(it, dict) and str(it.get("id")) == str(item_id):
            return it
    return None
def cacar_datas(item_id, promo_id, tipo, offer_id, access):
    """Bate em VÁRIAS consultas e mostra onde aparece start_date. Retorna (win_start, win_finish)."""
    print("\n===== CAÇA À START_DATE (todas as consultas possíveis pro item) =====", flush=True)
    achou = []
    consultas = [
        ("1) /items/{id} (mapa do item)", f"/seller-promotions/items/{item_id}?app_version=v2", True),
        ("2) /promotions/{pid}/items?item_id", f"/seller-promotions/promotions/{promo_id}/items?promotion_type={tipo}&item_id={item_id}&app_version=v2", False),
        ("3) ...&status=candidate", f"/seller-promotions/promotions/{promo_id}/items?promotion_type={tipo}&item_id={item_id}&status=candidate&app_version=v2", False),
        ("4) ...&status=pending", f"/seller-promotions/promotions/{promo_id}/items?promotion_type={tipo}&item_id={item_id}&status=pending&app_version=v2", False),
        ("5) ...&status=started", f"/seller-promotions/promotions/{promo_id}/items?promotion_type={tipo}&item_id={item_id}&status=started&app_version=v2", False),
        ("6) ...&status_item=active", f"/seller-promotions/promotions/{promo_id}/items?promotion_type={tipo}&item_id={item_id}&status_item=active&app_version=v2", False),
        ("7) /candidates/{offer_id}", f"/seller-promotions/candidates/{offer_id}?app_version=v2", "self"),
        ("8) /offers/{offer_id}", f"/seller-promotions/offers/{offer_id}?app_version=v2", "self"),
        ("9) detalhe da campanha", f"/seller-promotions/promotions/{promo_id}?promotion_type={tipo}&app_version=v2", "self"),
    ]
    for nome, url, modo in consultas:
        st, d = rec.get(url, access)
        alvo = None
        if modo is True:            # lista do mapa do item -> acha a promo pelo id
            if isinstance(d, list):
                alvo = next((o for o in d if isinstance(o, dict) and o.get("id") == promo_id), None)
        elif modo == "self":        # a própria resposta é o objeto
            alvo = d if isinstance(d, dict) else None
        else:                        # results[] -> acha o item
            alvo = _achar_item(d, item_id)
        s, f = _datas_de(alvo) if alvo else (None, None)
        marca = "  <<< TEM DATA!" if s else ""
        print(f"  {nome}: HTTP {st} | start={s} finish={f}{marca}", flush=True)
        if alvo and (s or f):
            achou.append((nome, s, f))
        # dump curto quando é o candidate/offer (pode ter campo novo)
        if modo == "self" and isinstance(alvo, dict):
            print(f"       -> {json.dumps(alvo, ensure_ascii=False)[:300]}", flush=True)
    # varredura paginada (sem item_id) — o item na lista completa pode ter data
    print("  10) PAGINANDO a lista inteira procurando o item...", flush=True)
    sa = None
    for _ in range(15):
        url = f"/seller-promotions/promotions/{promo_id}/items?promotion_type={tipo}&app_version=v2&limit=50"
        if sa:
            url += f"&search_after={sa}"
        st, d = rec.get(url, access)
        it = _achar_item(d, item_id)
        if it:
            s, f = _datas_de(it)
            print(f"      achou na paginação: start={s} finish={f} status={it.get('status')}", flush=True)
            if s or f:
                achou.append(("10) paginação", s, f))
            break
        pag = (d.get("paging") if isinstance(d, dict) else {}) or {}
        sa = (d.get("search_after") if isinstance(d, dict) else None) or pag.get("search_after")
        if not sa:
            break
    print("  ----- RESUMO da caça -----", flush=True)
    if achou:
        for nome, s, f in achou:
            print(f"    ✓ {nome}: start={s} finish={f}", flush=True)
        win_s = WIN_START or achou[0][1]
        win_f = WIN_FINISH or achou[0][2]
        print(f"    >>> vou usar a janela: start={win_s} finish={win_f}", flush=True)
        return win_s, win_f
    print("    (NENHUMA consulta trouxe start_date pro item)", flush=True)
    return (WIN_START or None), (WIN_FINISH or None)
def offer_id_fresco(item_id, promo_id, tipo, access):
    st, of = rec.get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
    if isinstance(of, list):
        for o in of:
            if o.get("id") == promo_id and (o.get("status") or "").lower() == "candidate":
                oid = o.get("ref_id") or o.get("offer_id")
                if oid:
                    return oid, o
    for extra in ("&status=candidate", ""):
        st, d = rec.get(f"/seller-promotions/promotions/{promo_id}/items"
                        f"?promotion_type={tipo}&item_id={item_id}&app_version=v2{extra}", access)
        it = _achar_item(d, item_id)
        if it:
            return (it.get("offer_id") or it.get("ref_id")), it
    return None, None
def formatos(promo_id, tipo, offer_id, price, win_s, win_f):
    """Formatos de POST — usa a JANELA ENCONTRADA (win_s/win_f) na data."""
    base = {"promotion_id": promo_id, "promotion_type": tipo, "offer_id": offer_id}
    fmts = [("A) DOC — id+type+offer (sem datas)", dict(base))]
    if win_s:
        S = win_s if len(win_s) > 10 else win_s + "T00:00:00"
        F = (win_f if (win_f and len(win_f) > 10) else ((win_f or win_s) + "T23:59:59"))
        Sz = S if S.endswith("Z") or "+" in S[10:] or "-" in S[10:] else S + "-03:00"
        Fz = F if F.endswith("Z") or "+" in F[10:] or "-" in F[10:] else F + "-03:00"
        fmts += [
            ("B) start+finish (data achada, local)", {**base, "start_date": S, "finish_date": F}),
            ("C) start+finish com fuso -03:00", {**base, "start_date": Sz, "finish_date": Fz}),
            ("D) offers[] com id+datas achadas", {"promotion_id": promo_id, "promotion_type": tipo,
                                                  "offers": [{"id": offer_id, "start_date": S, "finish_date": F}]}),
            ("E) offer_id + offers[] datas", {**base, "offers": [{"start_date": S, "finish_date": F}]}),
        ]
    return fmts
def main():
    if not ITEM or not PROMO_ID:
        print("!! defina ITEM_ID e PROMO_ID", flush=True)
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
    print(f"################ PROBE v3 {ITEM} -> {PROMO_ID} [{PROMO_TIPO}] — conta {SID} ################", flush=True)
    offer_id, cand = offer_id_fresco(ITEM, PROMO_ID, PROMO_TIPO, access)
    if not offer_id:
        print("  >>> ABORTA: item não é candidato nessa promoção agora.", flush=True)
        return
    print(f"  offer_id do candidato: {offer_id}", flush=True)
    win_s, win_f = cacar_datas(ITEM, PROMO_ID, PROMO_TIPO, offer_id, access)
    if SO_CACAR:
        print("\n  (SO_CACAR=1 — só cacei datas, NÃO tentei entrar)", flush=True)
        print("################ FIM ################", flush=True)
        return
    price = (cand or {}).get("price")
    fmts = formatos(PROMO_ID, PROMO_TIPO, offer_id, price, win_s, win_f)
    print(f"\n  === TENTANDO {len(fmts)} FORMATOS (para no 1º OK) | SAIR_DEPOIS={'1' if SAIR_DEPOIS else '0'} ===\n", flush=True)
    venceu = None
    for nome, body in fmts:
        sc, resp = post(f"/seller-promotions/items/{ITEM}?app_version=v2", access, body)
        okp = sc in (200, 201)
        print(f"  [{'OK ✓' if okp else ' x '}] {nome}", flush=True)
        print(f"       enviei: {json.dumps(body, ensure_ascii=False)}", flush=True)
        print(f"       ML {sc}: {json.dumps(resp, ensure_ascii=False)[:350]}", flush=True)
        if okp:
            venceu = (nome, body, resp)
            break
        time.sleep(0.6)
    if not venceu:
        print("\n  >>> NENHUM formato entrou (veja os erros + a caça de datas acima).", flush=True)
        return
    nome, body, resp = venceu
    print(f"\n  >>> ENTROU ✓ pelo formato: {nome} | corpo: {json.dumps(body, ensure_ascii=False)}", flush=True)
    if SAIR_DEPOIS:
        new_offer = (resp.get("offer_id") if isinstance(resp, dict) else None) or offer_id
        dc, dresp = req_delete(f"/seller-promotions/items/{ITEM}?promotion_type={PROMO_TIPO}"
                               f"&promotion_id={PROMO_ID}&offer_id={new_offer}&app_version=v2", access)
        print(f"      DESFEZ (SAIR_DEPOIS=1) -> {dc}: {json.dumps(dresp, ensure_ascii=False)[:200]}", flush=True)
    print("\n################ FIM ################", flush=True)
if __name__ == "__main__":
    main()
