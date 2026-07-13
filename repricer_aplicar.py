"""
APLICADOR (Fase 2) — escreve na Central de Promoções. Use com cuidado.
Segurança:
  - Só mexe em UM anúncio (ITEM_ID). Nunca em lote.
  - Por padrão roda em SIMULAÇÃO (dry-run): apenas MOSTRA as chamadas que faria.
  - Só executa de verdade se CONFIRMA=SIM.
  - Nunca toca no preço do anúncio — só entra/sai de promoções (sua regra de ouro).
  - SO_ENTRAR=SIM: só ENTRA no alvo, não sai de nada (pra testar a entrada isolada).

Escrita pela API correta (docs de campanhas co-financiadas):
  POST/DELETE  /marketplace/seller-promotions/items/{item_id}?user_id={seller_id}
  headers: version: v2, X-Client-Id, X-Caller-Id
  entrar: body {promotion_id, promotion_type}   (SEM offer_id; o ML gera)
  sair:   query promotion_id, promotion_type, offer_id, user_id

Leitura continua na API pública normal (/seller-promotions/items/{id}?app_version=v2).
O alvo vem da sugestão APROVADA (repricer_sugestoes) ou de PROMO_ID/PROMO_TYPE.
"""
import os
import json
import time
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
ITEM_ID = (os.environ.get("ITEM_ID") or "").strip()
CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
SO_ENTRAR = (os.environ.get("SO_ENTRAR") or "").strip().upper() == "SIM"
PROMO_ID = (os.environ.get("PROMO_ID") or "").strip()
PROMO_TYPE = (os.environ.get("PROMO_TYPE") or "").strip()
CLIENT_ID = os.environ.get("ML_CLIENT_ID", "").strip()
# X-Caller-Id: por padrão o seller_id (padrão da API). Dá pra forçar via CALLER_ID.
CALLER_ID_ENV = os.environ.get("CALLER_ID", "").strip()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
SEED = os.environ.get("ML_REFRESH_TOKEN", "")

STATUS_ATIVA = {"started", "active", "in_progress", "ongoing"}


def req(method, path, access, body=None, headers=None, tent=2):
    url = API + path
    h = {"Authorization": "Bearer " + access, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = None
    for i in range(tent):
        r = requests.request(method, url, headers=h, json=body, timeout=25)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1)); continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)


def wheaders(sid):
    """Cabeçalhos exigidos pela escrita em /marketplace/seller-promotions.
    X-Caller-Id = client id do app (não o seller_id), conforme os exemplos da doc."""
    caller = CALLER_ID_ENV or CLIENT_ID
    return {"version": "v2", "X-Client-Id": CLIENT_ID, "X-Caller-Id": caller}


def contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    cs = [(c["seller_id"], c.get("refresh_token")) for c in (res.data or []) if c.get("refresh_token")]
    if not cs and SEED:
        cs = [(None, SEED)]
    return cs


def dono(item_id):
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        st, it = req("GET", f"/items/{item_id}", access)
        if isinstance(it, dict) and it.get("id") == item_id:
            return access, sid, it
    return None, None, None


def eh_ativa(o):
    if (o.get("status") or "").lower() in STATUS_ATIVA:
        return True
    return str(o.get("ref_id") or "").upper().startswith("OFFER-")


def offer_id_de(o):
    ref = str(o.get("ref_id") or "")
    if "-" in ref:
        tail = ref.rsplit("-", 1)[-1]
        if tail.isdigit():
            return tail
    return None


def promos_do_item(item_id, access):
    st, d = req("GET", f"/seller-promotions/items/{item_id}?app_version=v2", access)
    return d if isinstance(d, list) else []


def sugestao_aprovada(item_id):
    try:
        r = (sb.table("repricer_sugestoes").select("*")
             .eq("item_id", item_id).eq("status", "aprovada").limit(1).execute().data)
        return r[0] if r else None
    except Exception:
        return None


def path_sair(item_id, sid, o):
    params = [f"promotion_type={o.get('type')}", f"user_id={sid}"]
    if o.get("id"):
        params.append(f"promotion_id={o['id']}")
    oid = offer_id_de(o)
    if oid:
        params.append(f"offer_id={oid}")
    return f"/marketplace/seller-promotions/items/{item_id}?" + "&".join(params)


def entrar_body_path(item_id, sid, o):
    path = f"/marketplace/seller-promotions/items/{item_id}?user_id={sid}"
    body = {"promotion_id": o.get("id"), "promotion_type": o.get("type")}
    if o.get("type") in ("DEAL", "PRICE_DISCOUNT", "LIGHTNING"):
        preco = o.get("price") or o.get("suggested_discounted_price")
        if preco:
            body["deal_price"] = preco
    return path, body


def main():
    if not ITEM_ID:
        print("Defina ITEM_ID.", flush=True); return
    if not CLIENT_ID:
        print("Falta ML_CLIENT_ID no ambiente (necessário pro X-Client-Id).", flush=True); return
    access, sid, it = dono(ITEM_ID)
    if not access:
        print(f"não achei o item {ITEM_ID}.", flush=True); return

    modo = "SIMULAÇÃO (dry-run)"
    if CONFIRMA:
        modo = "⚠️ EXECUTAR (só entrar)" if SO_ENTRAR else "⚠️ EXECUTAR"
    print(f"===== APLICADOR | {ITEM_ID} | conta {sid} =====", flush=True)
    print(f"título: {it.get('title')}", flush=True)
    print(f"MODO: {modo}", flush=True)
    print(f"escrita em: /marketplace/seller-promotions (X-Caller-Id={'(client id do app)' if not CALLER_ID_ENV else CALLER_ID_ENV})", flush=True)

    promos = promos_do_item(ITEM_ID, access)
    ativas = [o for o in promos if isinstance(o, dict) and eh_ativa(o)]
    print(f"\nAtivas/enroladas agora ({len(ativas)}):", flush=True)
    for o in ativas:
        print(f"  - {o.get('name') or o.get('type')} | id={o.get('id')} type={o.get('type')} "
              f"R${o.get('price')} ref={o.get('ref_id')}", flush=True)

    alvo = None
    sug = sugestao_aprovada(ITEM_ID)
    alvo_id = PROMO_ID or (sug.get("promocao_id") if sug else None)
    alvo_type = PROMO_TYPE or (sug.get("promocao_tipo") if sug else None)
    if alvo_id:
        alvo = next((o for o in promos if o.get("id") == alvo_id), None)
        if not alvo and alvo_type:
            alvo = {"id": alvo_id, "type": alvo_type}
    if not alvo:
        print("\nSem alvo (nenhuma sugestão aprovada e sem PROMO_ID). Nada a fazer.", flush=True)
        return
    print(f"\nALVO (entrar): {alvo.get('name') or alvo.get('id')} | id={alvo.get('id')} type={alvo.get('type')}", flush=True)

    ja_no_alvo = any(o.get("id") == alvo.get("id") for o in ativas)
    sair = [] if SO_ENTRAR else [o for o in ativas if o.get("id") != alvo.get("id")]

    print("\n----- PLANO -----", flush=True)
    if not ja_no_alvo:
        path, body = entrar_body_path(ITEM_ID, sid, alvo)
        print(f"  ENTRAR→ POST {path}\n           body {json.dumps(body, ensure_ascii=False)}", flush=True)
    else:
        print("  (alvo já ativo — não entra de novo)", flush=True)
    for o in sair:
        print(f"  SAIR  → DELETE {path_sair(ITEM_ID, sid, o)}", flush=True)
    if SO_ENTRAR:
        print("  (SO_ENTRAR: não sai de nada)", flush=True)

    if not CONFIRMA:
        print("\n=== SIMULAÇÃO: nada foi escrito. CONFIRMA=SIM pra aplicar. ===", flush=True)
        return

    # ---- EXECUÇÃO: entrar primeiro; só sai depois se entrar der certo ----
    print("\n----- EXECUTANDO -----", flush=True)
    if not ja_no_alvo:
        path, body = entrar_body_path(ITEM_ID, sid, alvo)
        st, resp = req("POST", path, access, body=body, headers=wheaders(sid))
        print(f"  ENTRAR → HTTP {st}: {json.dumps(resp, ensure_ascii=False)[:500]}", flush=True)
        if not (200 <= st < 300):
            print("\n⚠️ ENTRAR falhou — não saio de nada. Item fica como está.", flush=True)
            return
    for o in sair:
        st, resp = req("DELETE", path_sair(ITEM_ID, sid, o), access, headers=wheaders(sid))
        print(f"  SAIR {o.get('name') or o.get('type')} → HTTP {st}: {json.dumps(resp, ensure_ascii=False)[:300]}", flush=True)
        time.sleep(0.4)

    time.sleep(4.0)
    print("\n----- ESTADO DEPOIS -----", flush=True)
    for o in promos_do_item(ITEM_ID, access):
        if eh_ativa(o):
            print(f"  ativa/enrolada: {o.get('name') or o.get('type')} R${o.get('price')} (id={o.get('id')})", flush=True)
    print("\n=== fim. Confira no painel. ===", flush=True)


if __name__ == "__main__":
    main()
