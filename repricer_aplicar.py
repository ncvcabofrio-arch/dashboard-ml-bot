"""
APLICADOR (Fase 2) — escreve na Central de Promoções via API pública.
Nunca mexe no preço do anúncio — só entra/sai de promoções.

TRÊS MODOS (decididos pelo que você preenche):
  1) ITEM_ID definido  -> UM item (bom pra testar). Aceita SO_ENTRAR / PROMO_ID / PROMO_TYPE.
  2) SELLER_ID definido -> LOTE: aplica as APROVADAS daquela conta (limite LIMITE).
  3) nenhum dos dois   -> DESCOBERTA: lista as contas (id, apelido, nº de aprovadas). Não escreve.

SEGURANÇA (modos 1 e 2):
  - Padrão SIMULAÇÃO (dry-run). Só escreve com CONFIRMA=SIM.
  - Ordem segura: ENTRA no alvo primeiro; só SAI das outras se o entrar der certo.
  - No lote, ao aplicar com sucesso marca a sugestão como 'aplicada' (não repete).

Chamadas (validadas, API doméstica):
  ENTRAR: POST /seller-promotions/items/{item}?app_version=v2
          body {promotion_id, promotion_type, offer_id=ref_id COMPLETO do candidato}
  SAIR:   DELETE /seller-promotions/items/{item}?promotion_type=..&promotion_id=..&offer_id=ref_id COMPLETO&app_version=v2
"""
import os
import json
import time
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
ITEM_ID = (os.environ.get("ITEM_ID") or "").strip()
SELLER_ID = (os.environ.get("SELLER_ID") or "").strip()
CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
SO_ENTRAR = (os.environ.get("SO_ENTRAR") or "").strip().upper() == "SIM"
SO_GANHO = (os.environ.get("SO_GANHO") or "").strip().upper() == "SIM"
PULAR_SAIR = (os.environ.get("PULAR_SAIR") or "").strip().upper() == "SIM"
PROMO_ID = (os.environ.get("PROMO_ID") or "").strip()
PROMO_TYPE = (os.environ.get("PROMO_TYPE") or "").strip()
LIMITE = int(os.environ.get("LIMITE", "5"))
# --- modo DESCONTO INDIVIDUAL (PRICE_DISCOUNT criado por nós) ---
MARGEM_ALVO = (os.environ.get("MARGEM_ALVO") or "").strip()   # ex "18" (margem % planejada)
DIAS = int(os.environ.get("DIAS", "14"))                       # duração do desconto (máx 14)
DEAL_PRICE = (os.environ.get("DEAL_PRICE") or "").strip()      # opcional: força o preço (teste cru)
TOP_DEAL_PRICE = (os.environ.get("TOP_DEAL_PRICE") or "").strip()  # opcional: preço p/ nível 3-6
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
SEED = os.environ.get("ML_REFRESH_TOKEN", "")

STATUS_ATIVA = {"started", "active", "in_progress", "ongoing", "pending"}


def req(method, path, access, body=None, tent=2):
    h = {"Authorization": "Bearer " + access, "Content-Type": "application/json"}
    r = None
    for i in range(tent):
        r = requests.request(method, API + path, headers=h, json=body, timeout=25)
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


def access_da_conta(seller_id_alvo):
    for seller_id, refresh in contas():
        a, s, refresh = obter_access(sb, seller_id, refresh)
        if str(s) == str(seller_id_alvo):
            return a, s
    return None, None


def eh_ativa(o):
    if (o.get("status") or "").lower() in STATUS_ATIVA:
        return True
    return str(o.get("ref_id") or "").upper().startswith("OFFER-")


def promos_do_item(item_id, access):
    st, d = req("GET", f"/seller-promotions/items/{item_id}?app_version=v2", access)
    return d if isinstance(d, list) else []


def path_sair(item_id, o):
    params = [f"promotion_type={o.get('type')}", "app_version=v2"]
    if o.get("id"):
        params.append(f"promotion_id={o['id']}")
    if o.get("ref_id"):
        params.append(f"offer_id={o['ref_id']}")
    return f"/seller-promotions/items/{item_id}?" + "&".join(params)


COM_OFFER = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL",
             "MARKETPLACE_CAMPAIGN", "VOLUME", "SELLER_CAMPAIGN", "PRE_NEGOTIATED", "BANK"}


def entrar_body_path(item_id, o, extra=None):
    """Corpo do POST por tipo de promoção (conforme docs da Central de Promoções)."""
    t = o.get("type")
    body = {"promotion_type": t}
    if t in COM_OFFER:
        # co-financiadas: promotion_id + offer_id (ref_id completo do candidato)
        body["promotion_id"] = o.get("id")
        if o.get("ref_id"):
            body["offer_id"] = o["ref_id"]
    elif t == "DEAL":
        body["promotion_id"] = o.get("id")
        p = o.get("price") or o.get("suggested_discounted_price")
        if p:
            body["deal_price"] = p
    elif t == "DOD":
        p = o.get("price") or o.get("suggested_discounted_price")
        if p:
            body["deal_price"] = p
    elif t == "LIGHTNING":
        p = o.get("price") or o.get("suggested_discounted_price")
        if p:
            body["deal_price"] = p
        stk = (o.get("stock") or {}).get("min")
        if stk:
            body["stock"] = stk
    elif t == "PRICE_DISCOUNT":
        p = o.get("suggested_discounted_price") or o.get("price")
        if p:
            body["deal_price"] = p
    else:
        body["promotion_id"] = o.get("id")
        if o.get("ref_id"):
            body["offer_id"] = o["ref_id"]
    if extra:
        body.update(extra)
    return f"/seller-promotions/items/{item_id}?app_version=v2", body


def datas_do_item(pid, ptype, item_id, access):
    """Janela (start_date/finish_date) daquele item na promoção — vem do endpoint de itens."""
    st, d = req("GET", f"/seller-promotions/promotions/{pid}/items?promotion_type={ptype}&item_id={item_id}&app_version=v2", access)
    results = d.get("results") if isinstance(d, dict) else None
    if results:
        r = results[0]
        return r.get("start_date"), (r.get("finish_date") or r.get("end_date"))
    return None, None


def fazer_entrar(item_id, o, access):
    """Entra na promoção; se reclamar de START_DATE, busca a janela do item e reenvia com as datas."""
    path, body = entrar_body_path(item_id, o)
    st, resp = req("POST", path, access, body=body)
    if st == 400 and "START_DATE" in json.dumps(resp, ensure_ascii=False):
        sd, fd = datas_do_item(o.get("id"), o.get("type"), item_id, access)
        extra = {}
        if sd:
            extra["start_date"] = sd
        if fd:
            extra["finish_date"] = fd
        if extra:
            path, body = entrar_body_path(item_id, o, extra)
            st, resp = req("POST", path, access, body=body)
    return st, resp


def marcar_status(item_id, novo):
    """Muda o status de TODAS as linhas aprovadas desse item (limpa duplicatas)."""
    try:
        (sb.table("repricer_sugestoes").update({"status": novo})
         .eq("item_id", item_id).eq("status", "aprovada").execute())
    except Exception as e:
        print(f"   (aviso: não atualizei status: {e})", flush=True)


def plano_item(item_id, access, alvo_id, alvo_type, acao, so_entrar=False):
    """Monta (entrar, sair, alvo) sem executar."""
    promos = promos_do_item(item_id, access)
    ativas = [o for o in promos if isinstance(o, dict) and eh_ativa(o)]
    alvo = None
    if alvo_id:
        alvo = next((o for o in promos if o.get("id") == alvo_id), None)
        if not alvo and alvo_type:
            alvo = {"id": alvo_id, "type": alvo_type}
    entrar = None
    sair = []
    a = (acao or "").lower()
    if a in ("entrar", "trocar") or (a == "" and alvo):
        if alvo:
            ja = any(o.get("id") == alvo.get("id") and eh_ativa(o) for o in ativas)
            entrar = None if ja else alvo
            sair = [] if so_entrar else [o for o in ativas if o.get("id") != alvo.get("id")]
    elif a == "sair":
        sair = [] if so_entrar else ativas
    return entrar, sair, alvo, ativas


def executar(item_id, access, entrar, sair):
    """Executa: entra primeiro; só sai se entrar der certo. Retorna (ok, log)."""
    log = []
    if entrar:
        st, resp = fazer_entrar(item_id, entrar, access)
        log.append(f"entrar {entrar.get('name') or entrar.get('type')}: HTTP {st}")
        if not (200 <= st < 300):
            log.append(f"  resp: {json.dumps(resp, ensure_ascii=False)[:200]}")
            return False, log
    for o in sair:
        st, resp = req("DELETE", path_sair(item_id, o), access)
        log.append(f"sair {o.get('name') or o.get('type')}: HTTP {st}")
        time.sleep(0.3)
    return True, log


# ---------------- MODO 1: UM ITEM (teste) ----------------
def modo_item():
    access, sid, it = dono(ITEM_ID)
    if not access:
        print(f"não achei o item {ITEM_ID}.", flush=True); return
    sug = None
    try:
        r = (sb.table("repricer_sugestoes").select("*")
             .eq("item_id", ITEM_ID).eq("status", "aprovada").limit(1).execute().data)
        sug = r[0] if r else None
    except Exception:
        pass
    alvo_id = PROMO_ID or (sug.get("promocao_id") if sug else None)
    alvo_type = PROMO_TYPE or (sug.get("promocao_tipo") if sug else None)
    acao = (sug.get("acao") if sug else None) or ("entrar" if alvo_id else "")

    modo = "SIMULAÇÃO (dry-run)"
    if CONFIRMA:
        modo = "⚠️ EXECUTAR (só entrar)" if SO_ENTRAR else "⚠️ EXECUTAR"
    print(f"===== APLICADOR | {ITEM_ID} | conta {sid} | {modo} =====", flush=True)
    print(f"título: {it.get('title')}", flush=True)

    entrar, sair, alvo, ativas = plano_item(ITEM_ID, access, alvo_id, alvo_type, acao, SO_ENTRAR)
    print(f"\nAtivas agora ({len(ativas)}): " +
          ", ".join(f"{o.get('name') or o.get('type')}" for o in ativas), flush=True)
    if not alvo and not sair:
        print("Sem alvo e nada a sair. Nada a fazer.", flush=True); return

    print("\n----- PLANO -----", flush=True)
    if entrar:
        p, b = entrar_body_path(ITEM_ID, entrar)
        print(f"  ENTRAR→ POST {p}\n           body {json.dumps(b, ensure_ascii=False)}", flush=True)
    for o in sair:
        print(f"  SAIR  → DELETE {path_sair(ITEM_ID, o)}", flush=True)

    if not CONFIRMA:
        print("\n=== SIMULAÇÃO: nada escrito. CONFIRMA=SIM pra aplicar. ===", flush=True); return
    print("\n----- EXECUTANDO -----", flush=True)
    ok, log = executar(ITEM_ID, access, entrar, sair)
    for l in log:
        print("  " + l, flush=True)
    if not ok:
        print("⚠️ entrar falhou — não saí de nada.", flush=True)
    print("\n=== fim. Confira no painel. ===", flush=True)


# ---------------- MODO 2: LOTE por conta ----------------
def modo_lote():
    access, sid = access_da_conta(SELLER_ID)
    if not access:
        print(f"não autentiquei a conta {SELLER_ID}.", flush=True); return
    q = (sb.table("repricer_sugestoes").select("*")
         .eq("seller_id", str(SELLER_ID)).eq("status", "aprovada"))
    if SO_GANHO:
        q = q.eq("acao", "trocar")
    elif PULAR_SAIR:
        q = q.neq("acao", "sair")   # aplica só ganhos (entrar/trocar); segura os "sair"
    todas = q.limit(500).execute().data or []
    # de-duplica por anúncio (linhas repetidas de rodadas diferentes) e corta no LIMITE
    seen = set()
    aprovadas = []
    for s in todas:
        if s["item_id"] in seen:
            continue
        seen.add(s["item_id"]); aprovadas.append(s)
        if len(aprovadas) >= LIMITE:
            break

    filtro = " | só trocas dinheiro-na-mesa" if SO_GANHO else (" | pulando os 'sair'" if PULAR_SAIR else "")
    modo = "⚠️ EXECUTAR" if CONFIRMA else "SIMULAÇÃO (dry-run)"
    print(f"===== APLICADOR LOTE | conta {sid} | {modo} =====", flush=True)
    print(f"aprovadas a processar (limite {LIMITE}): {len(aprovadas)}{filtro}\n", flush=True)

    cont = {}
    for sug in aprovadas:
        entrar, sair, alvo, ativas = plano_item(
            sug["item_id"], access, sug.get("promocao_id"), sug.get("promocao_tipo"), sug.get("acao"))
        if (sug.get("acao") or "").lower() in ("entrar", "trocar") and not alvo:
            res, det = "sem_alvo", "promoção-alvo não está mais disponível (rode as sugestões de novo)"
        elif not entrar and not sair:
            res, det = "nada", "já no estado desejado"
        elif not CONFIRMA:
            partes = ([f"ENTRAR {entrar.get('name') or entrar.get('type')}"] if entrar else []) + \
                     [f"SAIR {o.get('name') or o.get('type')}" for o in sair]
            res, det = "simulado", " | ".join(partes)
        else:
            ok, log = executar(sug["item_id"], access, entrar, sair)
            if ok:
                marcar_status(sug["item_id"], "aplicada"); res = "aplicado"
            elif any("START_DATE" in l for l in log):
                # promoção com janela de data manual (ex.: oferta-relâmpago): pula limpo
                marcar_status(sug["item_id"], "manual"); res = "manual"
            else:
                res = "erro_entrar"
            det = " | ".join(log)
        cont[res] = cont.get(res, 0) + 1
        tag = {"aplicado": "✅", "simulado": "•", "erro_entrar": "⛔",
               "sem_alvo": "⚠️", "manual": "⏭️"}.get(res, "·")
        print(f"{tag} [{res}] {sug['item_id']} {str(sug.get('titulo'))[:28]} -> {det}", flush=True)
        time.sleep(0.3)

    print("\n=== " + ", ".join(f"{k}: {v}" for k, v in cont.items()) + " ===", flush=True)
    if not CONFIRMA:
        print("SIMULAÇÃO: nada escrito. CONFIRMA=SIM pra aplicar.", flush=True)


# ---------------- MODO 3: DESCOBERTA ----------------
def modo_descoberta():
    print("=== DESCOBERTA (nenhuma escrita) ===", flush=True)
    print("Preencha ITEM_ID (um item) ou SELLER_ID (lote). Contas disponíveis:\n", flush=True)
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        st, u = req("GET", f"/users/{sid}", access)
        nick = u.get("nickname") if isinstance(u, dict) else "?"
        try:
            n = sb.table("repricer_sugestoes").select("id", count="exact").eq(
                "seller_id", str(sid)).eq("status", "aprovada").execute().count
        except Exception:
            n = "?"
        print(f"  seller_id={sid}  |  apelido: {nick}  |  aprovadas: {n}", flush=True)


# ---------------- MODO 4: DESCONTO INDIVIDUAL (PRICE_DISCOUNT nosso) ----------------
# Cria um desconto próprio no item, com o preço calculado para bater uma MARGEM-ALVO.
# Nunca mexe no preço do anúncio: é uma oferta na Central de Promoções (some quando expira).
# Chamada oficial (doc ML): POST /seller-promotions/items/{id}?app_version=v2
#   body {deal_price, top_deal_price?, start_date, finish_date, promotion_type:"PRICE_DISCOUNT"}
# Regras: reputação verde, item ativo/novo/exposição não-gratuita; desconto entre 5% e 80%;
#         prazo máx 14 dias; se o item estiver num DEAL, só aplica quando o DEAL acabar.

def _datas_desconto():
    from datetime import date, timedelta
    hoje = date.today()
    dias = max(1, min(DIAS, 14))
    fim = hoje + timedelta(days=dias - 1)   # start hoje 00:00 .. fim 23:59 => 'dias' dias corridos
    return f"{hoje.isoformat()}T00:00:00", f"{fim.isoformat()}T00:00:00"


def _margem_no_preco(rec, pb, cat, ltid, frete, custo, access):
    """Mesma fórmula do robô de sugestões, com meli%=0 (desconto próprio, sem cofinanciamento)."""
    com = rec.comissao(round(pb, 2), cat, ltid, access) or 0
    recebe = pb - com - frete
    margem = ((recebe - custo) / pb * 100) if pb else -999
    return margem, round(com, 2), round(recebe, 2)


def _preco_para_margem(rec, alvo_pct, cat, ltid, frete, custo, access, lo, hi):
    """Acha, por busca binária, o preço cujo 'Você recebe' rende a margem-alvo.
    A margem cresce com o preço, então dá pra bissecar entre lo(=maior desconto) e hi(=menor)."""
    m_lo, _, _ = _margem_no_preco(rec, lo, cat, ltid, frete, custo, access)
    m_hi, _, _ = _margem_no_preco(rec, hi, cat, ltid, frete, custo, access)
    if m_hi < alvo_pct:          # nem no menor desconto (5%) a margem alcança o alvo
        return None, m_hi
    if m_lo >= alvo_pct:         # já no maior desconto (80%) a margem passa do alvo
        return round(lo, 2), m_lo
    for _ in range(38):
        mid = (lo + hi) / 2
        m, _, _ = _margem_no_preco(rec, mid, cat, ltid, frete, custo, access)
        if m < alvo_pct:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 2), alvo_pct


def modo_desconto():
    import repricer_sugestoes as rec
    rec.preload()   # carrega custos (produtos) e pisos (etiquetas/grupos)

    access, sid, it = dono(ITEM_ID)
    if not access:
        print(f"não achei o item {ITEM_ID} em nenhuma conta.", flush=True); return

    p0 = float(it.get("price") or 0)
    cat = it.get("category_id")
    ltid = it.get("listing_type_id")
    sku = it.get("seller_sku") or it.get("seller_custom_field")
    custo = rec.custo_de(sku)
    frete, forig = rec.frete_de(sku, ITEM_ID, access)

    modo = "⚠️ EXECUTAR" if CONFIRMA else "SIMULAÇÃO (dry-run)"
    print(f"===== DESCONTO INDIVIDUAL | {ITEM_ID} | conta {sid} | {modo} =====", flush=True)
    print(f"título: {it.get('title')}", flush=True)
    print(f"preço cheio: R${p0:.2f} | custo: {custo} | frete(list_cost): R${frete:.2f} ({forig})"
          f" | categoria {cat} | tipo {ltid}", flush=True)

    # --- elegibilidade (só avisa; não bloqueia a simulação) ---
    st, u = req("GET", f"/users/{sid}", access)
    nivel = ((u or {}).get("seller_reputation") or {}).get("level_id") if isinstance(u, dict) else None
    if nivel and "green" not in str(nivel):
        print(f"⚠️ reputação '{nivel}' (a doc pede VERDE p/ PRICE_DISCOUNT).", flush=True)
    if (it.get("status") or "") != "active":
        print(f"⚠️ item não está 'active' (status={it.get('status')}).", flush=True)
    if (it.get("condition") or "") != "new":
        print(f"⚠️ item não é 'new' (condition={it.get('condition')}).", flush=True)
    ativas = [o for o in promos_do_item(ITEM_ID, access) if isinstance(o, dict) and eh_ativa(o)]
    if any((o.get("type") or "") == "DEAL" for o in ativas):
        print("⚠️ item está num DEAL — o PRICE_DISCOUNT só aplica quando o DEAL terminar.", flush=True)

    if custo is None and not DEAL_PRICE:
        print("Sem custo cadastrado (tabela produtos) — não dá pra calcular margem. "
              "Preencha o custo ou passe DEAL_PRICE pra teste cru.", flush=True)
        return
    if not p0:
        print("Item sem preço — abortei.", flush=True); return

    # faixa permitida pela doc: desconto entre 5% (preço=95%) e 80% (preço=20%)
    pb_min = round(p0 * 0.20, 2)   # maior desconto (80% off)
    pb_max = round(p0 * 0.95, 2)   # menor desconto (5% off)

    # --- decide o deal_price ---
    if DEAL_PRICE:
        deal = round(float(DEAL_PRICE), 2)
        origem = "DEAL_PRICE (forçado)"
    elif MARGEM_ALVO:
        alvo = float(MARGEM_ALVO)
        deal, m_result = _preco_para_margem(rec, alvo, cat, ltid, frete, custo, access, pb_min, pb_max)
        if deal is None:
            # nem o menor desconto (5%) mantém a margem-alvo -> melhor NÃO entrar
            m_cheio, _, rec_cheio = _margem_no_preco(rec, p0, cat, ltid, frete, custo, access)
            print(f"\n✋ NÃO ENTRAR: com o menor desconto possível (5% -> R${pb_max:.2f}) a margem "
                  f"cai pra {m_result:.1f}%, abaixo do alvo {alvo:.0f}%.", flush=True)
            print(f"   No preço cheio a margem é {m_cheio:.1f}% (recebe R${rec_cheio:.2f}). "
                  f"Melhor deixar sem desconto.", flush=True)
            return
        origem = f"margem-alvo {alvo:.0f}%"
    else:
        print("Passe MARGEM_ALVO (ex 18) ou DEAL_PRICE.", flush=True); return

    # trava na faixa 5%–80%
    deal = min(max(deal, pb_min), pb_max)
    desc_pct = (1 - deal / p0) * 100
    margem, com, recebe = _margem_no_preco(rec, deal, cat, ltid, frete, custo, access)
    start, finish = _datas_desconto()

    print(f"\n----- PLANO ({origem}) -----", flush=True)
    print(f"  deal_price:  R${deal:.2f}   (desconto {desc_pct:.1f}% sobre R${p0:.2f})", flush=True)
    if custo is not None:
        print(f"  você recebe: R${recebe:.2f}  (comissão R${com:.2f} + frete R${frete:.2f})", flush=True)
        print(f"  MARGEM resultante: {margem:.1f}%", flush=True)
    print(f"  vigência: {start[:10]} a {finish[:10]}  ({DIAS} dia(s), some ao expirar)", flush=True)

    body = {"deal_price": deal, "start_date": start, "finish_date": finish,
            "promotion_type": "PRICE_DISCOUNT"}
    if TOP_DEAL_PRICE:
        body["top_deal_price"] = round(float(TOP_DEAL_PRICE), 2)
    path = f"/seller-promotions/items/{ITEM_ID}?app_version=v2"
    print(f"\n  POST {path}\n       body {json.dumps(body, ensure_ascii=False)}", flush=True)

    if desc_pct < 5:
        print("\n⚠️ desconto < 5%: o ML rejeita PRICE_DISCOUNT abaixo de 5%. "
              "Nesse caso o item já bate a margem sem promoção.", flush=True)

    if not CONFIRMA:
        print("\n=== SIMULAÇÃO: nada escrito. CONFIRMA=SIM pra criar o desconto. ===", flush=True)
        return

    print("\n----- EXECUTANDO -----", flush=True)
    st, resp = req("POST", path, access, body=body)
    print(f"  POST -> HTTP {st}: {json.dumps(resp, ensure_ascii=False)[:300]}", flush=True)
    if 200 <= st < 300:
        time.sleep(3)
        depois = promos_do_item(ITEM_ID, access)
        pd = next((o for o in depois if isinstance(o, dict) and (o.get("type") or "") == "PRICE_DISCOUNT"), None)
        if pd:
            print(f"  estado agora: status={pd.get('status')} price={pd.get('price')} "
                  f"original={pd.get('original_price')}", flush=True)
            if pd.get("boosted_offer"):
                print(f"  🎁 ML turbinou: +{pd.get('discount_meli_boosted_percentage')}% "
                      f"(R${pd.get('discount_meli_boost_amount')}), preço p/ boost "
                      f"R${pd.get('total_price_for_boosted_offer')} — você recebe mais.", flush=True)
        print("\n=== desconto criado. Confira no painel/anúncio. Pra tirar: DELETE "
              f"/seller-promotions/items/{ITEM_ID}?promotion_type=PRICE_DISCOUNT&app_version=v2 ===", flush=True)
    else:
        print("\n=== não criou. Veja a resposta acima. ===", flush=True)


def main():
    if ITEM_ID and (MARGEM_ALVO or DEAL_PRICE):
        modo_desconto()
    elif ITEM_ID:
        modo_item()
    elif SELLER_ID:
        modo_lote()
    else:
        modo_descoberta()


if __name__ == "__main__":
    main()
