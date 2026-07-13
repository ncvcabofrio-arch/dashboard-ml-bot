"""
APLICADOR (Fase 2) — escreve na Central de Promoções. Use com cuidado.
Segurança:
  - Só mexe em UM anúncio (ITEM_ID). Nunca em lote.
  - Por padrão roda em SIMULAÇÃO (dry-run): apenas MOSTRA as chamadas que faria.
  - Só executa de verdade se CONFIRMA=SIM.
  - Nunca toca no preço do anúncio — só entra/sai de promoções (sua regra de ouro).

Estratégia (validada no painel): o ML aplica só UMA promoção por vez (a ATIVA),
e a API não diz qual é a vencedora. Então a jogada é deixar o anúncio enrolado
SÓ na melhor: sair de todas as promoções ativas que não são o alvo, e entrar no alvo.

O alvo vem da sua sugestão APROVADA (repricer_sugestoes), ou de PROMO_ID/PROMO_TYPE.
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
PROMO_ID = (os.environ.get("PROMO_ID") or "").strip()          # opcional: forçar alvo
PROMO_TYPE = (os.environ.get("PROMO_TYPE") or "").strip()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
SEED = os.environ.get("ML_REFRESH_TOKEN", "")

STATUS_ATIVA = {"started", "active", "in_progress", "ongoing"}


def req(method, path, access, body=None, tent=2):
    url = API + path
    h = {"Authorization": "Bearer " + access, "Content-Type": "application/json"}
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


def chamada_sair(item_id, o):
    """Monta a DELETE pra sair de uma promoção (não executa)."""
    ptype = o.get("type")
    params = [f"promotion_type={ptype}", "app_version=v2"]
    if o.get("id"):
        params.append(f"promotion_id={o['id']}")
    ref = str(o.get("ref_id") or "")
    if ref.startswith("OFFER-"):
        params.append(f"offer_id={ref}")   # a doc pede offer_id; confirmamos na resposta
    return f"/seller-promotions/items/{item_id}?" + "&".join(params)


def chamada_entrar(item_id, o):
    """Monta a POST pra entrar numa promoção (path, body)."""
    body = {"promotion_id": o.get("id"), "promotion_type": o.get("type")}
    if o.get("type") in ("DEAL", "PRICE_DISCOUNT", "LIGHTNING"):
        # tipos de faixa: manda o preço-alvo (usa o sugerido do painel se houver)
        preco = o.get("price") or o.get("suggested_discounted_price")
        if preco:
            body["deal_price"] = preco
    return f"/seller-promotions/items/{item_id}?app_version=v2", body


def main():
    if not ITEM_ID:
        print("Defina ITEM_ID.", flush=True); return
    access, sid, it = dono(ITEM_ID)
    if not access:
        print(f"não achei o item {ITEM_ID}.", flush=True); return

    print(f"===== APLICADOR | {ITEM_ID} | conta {sid} =====", flush=True)
    print(f"título: {it.get('title')}", flush=True)
    print(f"MODO: {'⚠️  EXECUTAR (CONFIRMA=SIM)' if CONFIRMA else 'SIMULAÇÃO (dry-run) — nada será escrito'}", flush=True)

    promos = promos_do_item(ITEM_ID, access)
    ativas = [o for o in promos if isinstance(o, dict) and eh_ativa(o)]
    print(f"\nPromoções ATIVAS/enroladas agora ({len(ativas)}):", flush=True)
    for o in ativas:
        print(f"  - {o.get('name') or o.get('type')} | id={o.get('id')} type={o.get('type')} "
              f"preço R${o.get('price')} ref={o.get('ref_id')}", flush=True)

    # alvo: da sugestão aprovada ou de PROMO_ID/PROMO_TYPE
    alvo = None
    sug = sugestao_aprovada(ITEM_ID)
    alvo_id = PROMO_ID or (sug.get("promocao_id") if sug else None)
    alvo_type = PROMO_TYPE or (sug.get("promocao_tipo") if sug else None)
    if alvo_id:
        alvo = next((o for o in promos if o.get("id") == alvo_id), None)
        if not alvo and alvo_type:
            alvo = {"id": alvo_id, "type": alvo_type}
    if not alvo:
        print("\nSem alvo definido (nenhuma sugestão aprovada e sem PROMO_ID). "
              "Só listei o estado atual — nada a fazer.", flush=True)
        return
    print(f"\nALVO (entrar): {alvo.get('name') or alvo.get('id')} | id={alvo.get('id')} type={alvo.get('type')}", flush=True)
    if sug:
        print(f"  (sugestão aprovada: {sug.get('acao')} · você recebe R${sug.get('recebe_liquido')} · "
              f"ativa atual pagava R${sug.get('ativa_preco')} margem {sug.get('ativa_margem')}%)", flush=True)

    # plano: sair de todas as ativas que NÃO são o alvo; entrar no alvo se ainda não estiver
    sair = [o for o in ativas if o.get("id") != alvo.get("id")]
    ja_no_alvo = any(o.get("id") == alvo.get("id") for o in ativas)

    print("\n----- PLANO -----", flush=True)
    for o in sair:
        print(f"  SAIR  → DELETE {chamada_sair(ITEM_ID, o)}", flush=True)
    if not ja_no_alvo:
        path, body = chamada_entrar(ITEM_ID, alvo)
        print(f"  ENTRAR→ POST {path}\n           body {json.dumps(body, ensure_ascii=False)}", flush=True)
    else:
        print("  (o alvo já está entre as ativas — só sairia das outras)", flush=True)

    if not CONFIRMA:
        print("\n=== SIMULAÇÃO: nada foi escrito. Rode com CONFIRMA=SIM pra aplicar. ===", flush=True)
        return

    # ---- EXECUÇÃO REAL ----
    print("\n----- EXECUTANDO -----", flush=True)
    for o in sair:
        p = chamada_sair(ITEM_ID, o)
        st, resp = req("DELETE", p, access)
        print(f"  SAIR {o.get('name') or o.get('type')} → HTTP {st}: {json.dumps(resp, ensure_ascii=False)[:300]}", flush=True)
        time.sleep(0.4)
    if not ja_no_alvo:
        path, body = chamada_entrar(ITEM_ID, alvo)
        st, resp = req("POST", path, access, body=body)
        print(f"  ENTRAR {alvo.get('name') or alvo.get('id')} → HTTP {st}: {json.dumps(resp, ensure_ascii=False)[:400]}", flush=True)

    time.sleep(1.0)
    print("\n----- ESTADO DEPOIS -----", flush=True)
    for o in promos_do_item(ITEM_ID, access):
        if eh_ativa(o):
            print(f"  ATIVA agora: {o.get('name') or o.get('type')} R${o.get('price')} (id={o.get('id')})", flush=True)
    print("\n=== fim. Confira no painel do ML se a ATIVA e o 'Você recebe' mudaram. ===", flush=True)


if __name__ == "__main__":
    main()
