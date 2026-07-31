"""
Fase 1 — ROBÔ DE RECOMENDAÇÃO DE PROMOÇÕES COMPARTILHADAS.
Somente leitura no Mercado Livre (não aplica nada). Para cada anúncio ATIVO:
  - lê as promoções onde o ML banca parte (meli_percentage)
  - calcula a MARGEM resultante de cada uma (recebimento − comissão − frete − custo)
  - recomenda a melhor que MANTÉM o piso do grupo do produto (padrão 18%)
Grava as recomendações na tabela 'repricer_sugestoes' pra você aprovar no app.
"""
import os
import time
import threading
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from supabase import create_client
from ml_auth import obter_access
API = "https://api.mercadolibre.com"
# 0 = TODOS os ativos (pagina tudo). Ponha um número só pra testar rápido (ex.: 40).
MAX_ITENS = int(os.environ.get("MAX_ITENS", os.environ.get("AMOSTRA", "0")))
MARGEM_PADRAO = float(os.environ.get("MARGEM_MIN", "18"))
WORKERS = int(os.environ.get("WORKERS", "8"))   # itens processados em paralelo
SELLER_ID_FILTRO = (os.environ.get("SELLER_ID") or "").strip()   # se setado, roda SÓ essa conta (ex.: testar a CF)
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
# caches pré-carregados (evitam ida ao banco por item; seguros entre threads)
CUSTOS = {}          # sku -> custo
CUSTO_ITEM = {}      # item_id -> custo (pra anúncios SEM SKU, preenchido no painel)
PISOS = {}           # sku -> (margem_minima, nome_grupo)
_pct_lock = threading.Lock()
SEM_CUSTO = []       # anúncios com promoção disponível MAS sem custo cadastrado (pra cadastrar)
_sc_lock = threading.Lock()
# VITRINE: anúncios que TÊM campanha mas nenhuma a recomendar (todas abaixo do
# piso, ou programadas para o futuro). Hoje eles somem: processar_item devolve
# None e o anúncio não ganha linha nenhuma. O painel do repricer está certo em
# não mostrá-los — não há o que aprovar. O painel da Arcos precisa vê-los,
# porque ele existe para MOSTRAR campanha, não para recomendar.
# Vão para a tabela promo_vitrine, separada. A repricer_sugestoes continua
# recebendo exatamente as mesmas linhas de sempre.
VITRINE = []
_vt_lock = threading.Lock()
def _estrategia_promo():
    """Estratégia p/ ESCOLHER a promoção nos anúncios TRADICIONAIS (fora do catálogo):
      - 'equilibrado' -> mira a MARGEM PADRÃO cadastrada (o piso do grupo) — recomendado
      - 'agressivo'   -> maior desconto (menor margem, ainda >= piso)
      - 'conservador' -> menor desconto (maior margem)
    Vem do painel (repricer_config chave 'estrategia_promo') > env ESTRATEGIA_PROMO > default."""
    v = os.environ.get("ESTRATEGIA_PROMO")
    if not v:
        try:
            d = (sb.table("repricer_config").select("valor")
                 .eq("chave", "estrategia_promo").limit(1).execute().data) or []
            v = d[0]["valor"] if d else None
        except Exception:
            v = None
    v = str(v or "equilibrado").strip().lower()
    return v if v in ("equilibrado", "agressivo", "conservador") else "equilibrado"
ESTRATEGIA = _estrategia_promo()
def escolher_alvo(cands, piso, estrategia):
    """Escolhe a promoção recomendada entre as candidatas seguras (>= piso), conforme a
    estratégia. Só é usada nos anúncios TRADICIONAIS (sem concorrência de catálogo)."""
    if not cands:
        return None
    if estrategia == "agressivo":
        return min(cands, key=lambda a: a["margem"])       # maior desconto
    if estrategia == "conservador":
        return max(cands, key=lambda a: a["margem"])       # menor desconto
    # equilibrado (padrão): margem mais próxima da margem padrão cadastrada (o piso)
    return min(cands, key=lambda a: abs(a["margem"] - piso))
def _todas_linhas(tabela, cols, passo=1000):
    """Lê a tabela INTEIRA paginando (PostgREST devolve no máx 1000 linhas por vez)."""
    linhas, ini = [], 0
    while True:
        lote = (sb.table(tabela).select(cols).range(ini, ini + passo - 1).execute().data) or []
        linhas += lote
        if len(lote) < passo:
            break
        ini += passo
    return linhas
def preload():
    """Carrega custos e grupos de uma vez só, pra não bater no banco por item."""
    try:
        for r in _todas_linhas("produtos", "sku, custo"):
            if r.get("sku") and r.get("custo") is not None:
                CUSTOS[r["sku"]] = float(r["custo"])
    except Exception as e:
        print("Aviso: não consegui pré-carregar custos:", e, flush=True)
    try:
        for r in _todas_linhas("repricer_custo_item", "item_id, custo"):
            if r.get("item_id") and r.get("custo") is not None:
                CUSTO_ITEM[r["item_id"]] = float(r["custo"])
    except Exception as e:
        print("Aviso: não consegui pré-carregar custo por anúncio:", e, flush=True)
    try:
        grupos = {g["id"]: (float(g["margem_minima"]), g.get("nome") or "Grupo")
                  for g in _todas_linhas("repricer_grupos", "id, margem_minima, nome")}
        for et in _todas_linhas("repricer_etiquetas", "sku, grupo_id"):
            if et.get("sku") in (None, "") or et.get("grupo_id") not in grupos:
                continue
            PISOS[et["sku"]] = grupos[et["grupo_id"]]
    except Exception as e:
        print("Aviso: não consegui pré-carregar grupos:", e, flush=True)
    print(f"pré-carregado: {len(CUSTOS)} custos, {len(PISOS)} etiquetas de grupo | estratégia: {ESTRATEGIA}", flush=True)
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
        return r.status_code, None
def contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    cs = [(c["seller_id"], c.get("refresh_token")) for c in (res.data or []) if c.get("refresh_token")]
    seed = os.environ.get("ML_REFRESH_TOKEN", "")
    if not cs and seed:
        cs = [(None, seed)]
    return cs
def todos_ativos(sid, access):
    """Retorna TODOS os item_ids ativos da conta (pagina de verdade).
    Usa offset até 1000; se a conta tiver mais que isso, muda pro modo 'scan'.
    Se MAX_ITENS > 0, corta nesse total (útil só pra teste rápido)."""
    ids = []
    total = None
    offset = 0
    while True:
        st, d = get(f"/users/{sid}/items/search?status=active&limit=50&offset={offset}", access)
        if not isinstance(d, dict):
            break
        res = d.get("results") or []
        ids.extend(res)
        total = (d.get("paging") or {}).get("total")
        offset += 50
        if MAX_ITENS and len(ids) >= MAX_ITENS:
            break
        if not res or offset >= 1000 or (total is not None and offset >= total):
            break
    # conta grande (> 1000 ativos): refaz pelo 'scan', que não tem teto
    if not (MAX_ITENS and len(ids) >= MAX_ITENS) and total and total > 1000:
        ids = []
        scroll = None
        while True:
            path = f"/users/{sid}/items/search?status=active&search_type=scan&limit=100"
            if scroll:
                path += f"&scroll_id={scroll}"
            st, d = get(path, access)
            if not isinstance(d, dict):
                break
            res = d.get("results") or []
            if not res:
                break
            ids.extend(res)
            scroll = d.get("scroll_id")
            if MAX_ITENS and len(ids) >= MAX_ITENS:
                break
            if not scroll:
                break
    # dedup preservando ordem
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    if MAX_ITENS:
        out = out[:MAX_ITENS]
    return out, total
def pausados_ids(sid, access):
    """item_ids com status PAUSED no Mercado Livre — alimenta a fila 'Pausados' do painel.
    Só coleta ids (não precifica, não altera nada no ML)."""
    ids, offset = [], 0
    while True:
        st, d = get(f"/users/{sid}/items/search?status=paused&limit=50&offset={offset}", access)
        if not isinstance(d, dict):
            break
        res = d.get("results") or []
        ids.extend(res)
        total = (d.get("paging") or {}).get("total")
        offset += 50
        if not res or offset >= 2000 or (total is not None and offset >= total):
            break
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
def custo_de(sku):
    return CUSTOS.get(sku) if sku else None
def custo_efetivo(item_id, sku):
    """Custo do anúncio: pelo SKU (tabela produtos) ou, se não tiver SKU/custo,
    pelo custo POR ANÚNCIO preenchido no painel (repricer_custo_item)."""
    c = CUSTOS.get(sku) if sku else None
    if c is not None:
        return c
    return CUSTO_ITEM.get(item_id)
CEP = os.environ.get("CEP", "01310100")   # destino de referência (Av. Paulista, SP)
def frete_anuncio(item_id, access):
    """Custo de envio DO ANÚNCIO (o que VOCÊ paga no frete grátis), lido de
    /items/{id}/shipping_options com um CEP de referência.
    Retorna (custo, origem):
      - custo = list_cost da opção de frete grátis (o valor que o ML te cobra,
        já com o desconto do Mercado Envios) — é o que bate com o
        "Custo de envio" do painel de promoções.
      - fallback: base_cost (tabela cheia) se não houver list_cost.
    Retorna (None, None) se o anúncio não devolver opções.
    """
    st, so = get(f"/items/{item_id}/shipping_options?zip_code={CEP}", access)
    opts = so.get("options") if isinstance(so, dict) else None
    if not isinstance(opts, list) or not opts:
        return None, None
    # prioriza a opção "grátis para o comprador" (cost == 0); senão a mais barata
    gratis = [o for o in opts if float(o.get("cost") or 0) == 0]
    escolha = (gratis or opts)
    # entre as candidatas, pega a de menor list_cost (opção padrão de envio)
    def keyf(o):
        return float(o.get("list_cost") or o.get("base_cost") or 1e9)
    o = min(escolha, key=keyf)
    lc = o.get("list_cost")
    bc = o.get("base_cost")
    if lc is not None:
        return round(float(lc), 2), "list_cost"
    if bc is not None:
        return round(float(bc), 2), "base_cost"
    return None, None
def frete_de(sku, item_id, access):
    """Frete do anúncio (list_cost do shipping_options). Se a API não trouxer,
    usa 0.0 (não bate no banco aqui pra ser seguro entre threads)."""
    custo, origem = frete_anuncio(item_id, access)
    if custo is not None:
        return custo, origem
    return 0.0, "indisponivel"
def margem_minima_do(sku):
    """Piso do grupo do produto (etiqueta -> grupo, pré-carregado); senão, padrão."""
    return PISOS.get(sku, (MARGEM_PADRAO, "Padrão"))
# comissão = % fixa por (categoria, tipo de anúncio). Acima do "custo fixo"
# (itens > R$79) a tarifa é só preço × %. Cacheamos a % pra não chamar a API
# uma vez por oferta — isso derruba MUITO o tempo de execução.
LIMITE_CUSTO_FIXO = 79.0
_pct_cache = {}
def _consulta_listing(preco, cat, ltid, access):
    path = f"/sites/MLB/listing_prices?price={preco}"
    if ltid:
        path += f"&listing_type_id={ltid}"
    if cat:
        path += f"&category_id={cat}"
    st, d = get(path, access)
    if isinstance(d, list) and d:
        d = d[0]
    return d if isinstance(d, dict) else None
def _percentual(cat, ltid, access):
    key = (cat, ltid)
    if key in _pct_cache:
        return _pct_cache[key]
    pct = None
    d = _consulta_listing(1000, cat, ltid, access)   # 1000 está acima do custo fixo
    if d:
        det = d.get("sale_fee_details") or {}
        pct = det.get("percentage_fee")
        if pct is None and d.get("sale_fee_amount"):
            pct = float(d["sale_fee_amount"]) / 1000.0 * 100.0   # deriva a %
    with _pct_lock:
        _pct_cache[key] = pct
    return pct
def comissao(preco, cat, ltid, access):
    if not preco:
        return None
    # itens acima do custo fixo: tarifa = preço × % (cacheado, sem nova chamada)
    if preco >= LIMITE_CUSTO_FIXO:
        pct = _percentual(cat, ltid, access)
        if pct is not None:
            return round(preco * pct / 100.0, 2)
    # itens baratos (tem custo fixo por unidade): consulta exata na API
    d = _consulta_listing(round(preco, 2), cat, ltid, access)
    return d.get("sale_fee_amount") if d else None
def detalhes_itens(ids, access):
    """Busca detalhes de VÁRIOS itens de uma vez (multiget, 20 por chamada)."""
    out = {}
    attrs = "id,price,listing_type_id,category_id,seller_sku,seller_custom_field,title,status,available_quantity,catalog_listing"
    for i in range(0, len(ids), 20):
        lote = ids[i:i + 20]
        st, d = get(f"/items?ids={','.join(lote)}&attributes={attrs}", access)
        if isinstance(d, list):
            for row in d:
                b = row.get("body") if isinstance(row, dict) else None
                if isinstance(b, dict) and b.get("id"):
                    out[b["id"]] = b
    return out
def ofertas_do_item(item_id, access):
    """Lista as promoções do item. O RESUMO já traz preço, original e percentuais
    de cada oferta — o 'detalhe' devolvia a mesma lista, então uma chamada basta.
    ATENÇÃO: este endpoint NÃO traz as participações ATIVAS de campanha
    cofinanciada/marketplace (só candidatas). Pra saber em quais o item está
    REALMENTE ativo (e sair delas), use participacoes_ativas()."""
    st, resumo = get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
    return resumo if isinstance(resumo, list) else []
def promocoes_do_vendedor(seller_id, access, limit=50, teto_paginas=80):
    """TODAS as promoções do vendedor (id, type, status). O endpoint /users pagina por
    OFFSET (a resposta traz paging.total) — não é search_after. Doc:
    GET /seller-promotions/users/{USER_ID}?app_version=v2&limit=50&offset=N ."""
    out = []
    if not seller_id:
        return out
    offset = 0
    for _ in range(teto_paginas):
        p = f"/seller-promotions/users/{seller_id}?app_version=v2&limit={limit}&offset={offset}"
        st, d = get(p, access)
        if not isinstance(d, dict):
            break
        res = d.get("results") or []
        out.extend(res)
        total = (d.get("paging") or {}).get("total")
        offset += limit
        if not res or (total is not None and offset >= total) or offset >= 4000:
            break
    return out
def participacoes_ativas(item_id, seller_id, access):
    """Descobre em quais promoções ESTE item está ATIVO/programado — o caminho CONFIÁVEL
    da doc (o /seller-promotions/items/{id} não traz as ativas de campanha marketplace):
      1) users/{seller}                       -> todas as promoções do vendedor;
      2) promotions/{id}/items?item_id=...     -> se o item participa (status started/pending)
         e o offer_id (obrigatório pra sair de cofinanciada).
    Retorna lista de dicts: {promotion_id, type, offer_id, name, status}."""
    achadas, vistos = [], set()
    for pr in promocoes_do_vendedor(seller_id, access):
        stp = (pr.get("status") or "").lower()
        if stp not in ("started", "pending"):
            continue
        pid = pr.get("id")
        ptipo = (pr.get("type") or "")
        if not pid or not ptipo:
            continue
        st, d = get(f"/seller-promotions/promotions/{pid}/items"
                    f"?promotion_type={ptipo}&item_id={item_id}&app_version=v2", access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        for it in res:
            if str(it.get("id")) != str(item_id):
                continue
            sti = (it.get("status") or "").lower()
            if sti not in ("started", "pending"):
                continue
            chave = (pid, ptipo.upper())
            if chave in vistos:
                continue
            vistos.add(chave)
            achadas.append({
                "promotion_id": pid,
                "type": ptipo.upper(),
                "offer_id": it.get("offer_id") or it.get("ref_id"),
                "name": pr.get("name"),
                "status": sti,
            })
    return achadas
# Você JÁ está participando quando o status é "started" (confirmado na sonda)
# ou quando o ref_id começa com "OFFER-" (candidatas vêm como "CANDIDATE-").
STATUS_ATIVA = {"started", "active", "in_progress", "ongoing"}
def eh_ativa(o):
    if (o.get("status") or "").lower() in STATUS_ATIVA:
        return True
    return str(o.get("ref_id") or "").upper().startswith("OFFER-")
def preco_oferta(o):
    """Preço que o comprador paga nessa oferta (candidatas têm 'price';
    ativas podem trazer o preço aplicado em outro campo)."""
    for k in ("price", "applied_price", "discounted_price", "deal_price", "new_price"):
        v = o.get(k)
        try:
            v = float(v)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None
def preco_exibicao(o):
    """Preço para MOSTRAR — nunca para decidir.

    Existe um tipo de candidata em que o VENDEDOR escolhe o valor: a API
    manda price = 0 e, no lugar, uma faixa (min/max/suggested). É o caso da
    SELLER_CAMPAIGN FLEXIBLE_PERCENTAGE e de parte das PRICE_DISCOUNT.

    O preco_oferta() devolve None nesses casos, e faz certo: o robô não pode
    inventar um preço para entrar numa promoção. Só que a tela da Arcos
    precisa MOSTRAR a campanha — foi assim que a "ARCOS BASE - 08-26" ficou
    invisível no painel apesar de estar candidata no Mercado Livre.

    Então esta função existe em separado, e o nome é para não haver dúvida:
    ela alimenta a vitrine, não a decisão."""
    p = preco_oferta(o)
    if p:
        return p
    for k in ("suggested_discounted_price", "max_discounted_price"):
        try:
            v = float(o.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None


def avaliar(o, cat, ltid, access, frete, custo):
    """Calcula a margem de UMA oferta (candidata ou ativa).
    Retorna None se não der pra avaliar (sem preço ou sem preço original)."""
    pb = preco_oferta(o)
    p0 = o.get("original_price")
    if not pb or not p0:
        return None
    p0 = float(p0)
    sp = float(o.get("seller_percentage") or 0)
    mp = float(o.get("meli_percentage") or 0)
    # comissão cheia sobre o preço em promoção; o ML banca via REDUÇÃO da tarifa
    # (redução = meli% do preço original), exatamente como o painel mostra.
    com_cheia = comissao(round(pb, 2), cat, ltid, access) or 0
    reducao = mp / 100.0 * p0
    tarifa = max(com_cheia - reducao, 0)
    recebe = pb - tarifa - frete              # = "Você recebe" do painel
    margem = ((recebe - custo) / pb * 100) if pb else -999
    return {"o": o, "pb": round(pb, 2), "sp": sp, "mp": mp,
            "tarifa": round(tarifa, 2), "frete": round(frete, 2),
            "recebe": round(recebe, 2), "margem": round(margem, 2)}
def _data_promo(o, *chaves):
    """Retorna a 1ª data preenchida entre as chaves (start_date/finish_date/end_date)."""
    for k in chaves:
        v = o.get(k)
        if v:
            return str(v)
    return None
def _parse_dt(v):
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
def _vigente(o):
    """False se a promoção ainda NÃO começou (programada/futura) ou já terminou.
    Sem data de início = tratamos como vigente (a maioria das cofinanciadas não trava data)."""
    ini = _parse_dt(o.get("start_date"))
    fim = _parse_dt(_data_promo(o, "finish_date", "end_date"))
    agora = datetime.now(timezone.utc)
    if ini and ini > agora:
        return False
    if fim and fim < agora:
        return False
    return True
_promo_cache = {}
_promo_lock = threading.Lock()
def _promo_detalhe(promotion_id, tipo, access):
    """Detalhe da promoção (traz status: started/pending/finished e datas). Com cache
    porque a MESMA promoção vale pra muitos itens (ex.: 'DESTAQUE 8.8' em 200 anúncios)."""
    if not promotion_id:
        return None
    with _promo_lock:
        if promotion_id in _promo_cache:
            return _promo_cache[promotion_id]
    st, d = get(f"/seller-promotions/promotions/{promotion_id}?promotion_type={tipo}&app_version=v2", access)
    d = d if isinstance(d, dict) else None
    with _promo_lock:
        _promo_cache[promotion_id] = d
    return d
def cand_vigente(o, access):
    """Vigência de um candidato. Usa a data do próprio candidato; se ela não vier
    (comum nas cofinanciadas), consulta o DETALHE da promoção (status started=ativa,
    pending=programada). É o jeito confiável de não recomendar/entrar em promo futura."""
    if o.get("start_date") or o.get("finish_date"):
        return _vigente(o)
    d = _promo_detalhe(o.get("id"), o.get("type"), access)
    # resposta de erro do ML vem como dict com "status" inteiro (404, 400...) e "error";
    # nesse caso não dá pra determinar — não descarta (a trava do aplicador ainda pega).
    if not isinstance(d, dict) or d.get("error") or not d.get("id"):
        return True
    status = str(d.get("status") or "").lower()
    if status == "started":
        return True
    if status in ("pending", "programmed", "scheduled", "finished"):
        return False
    return _vigente(d)
def _rotulo(a):
    o = a["o"]
    return f"{o.get('name') or o.get('type') or '?'} R${a['pb']:.2f}->{a['margem']:.1f}%"
def _oferta_dict(a, ativa_flag, recomendada_flag, acao=None, access=None):
    """Uma linha da lista de campanhas mostrada na tela expandida do painel.
    As datas das cofinanciadas costumam vir no DETALHE da promoção (não no
    objeto da oferta), então buscamos lá quando não vêm direto (com cache)."""
    o = a["o"]
    ini = _data_promo(o, "start_date")
    fim = _data_promo(o, "finish_date", "end_date")
    if (ini is None or fim is None) and access is not None:
        det = _promo_detalhe(o.get("id"), o.get("type"), access)
        if isinstance(det, dict):
            ini = ini or _data_promo(det, "start_date")
            fim = fim or _data_promo(det, "finish_date", "end_date")
    d = {"nome": o.get("name"), "tipo": o.get("type"),
         "rebate": a.get("mp"), "desconto_vendedor": a.get("sp"),
         "preco": a.get("pb"), "inicio": ini, "fim": fim,
         "margem": a.get("margem"), "ativa": ativa_flag,
         "recomendada": recomendada_flag,
         "acao": (acao if recomendada_flag else None)}
    # campanha de preço aberto: o valor mostrado é o SUGERIDO, e a faixa
    # inteira vai junto para a tela poder simular qualquer desconto dentro dela
    if a.get("faixa"):
        d["preco_min"], d["preco_max"] = a["faixa"]
        d["preco_aberto"] = True
    return d
# NOTA: as campanhas de marketplace (SMART/PRICE_MATCHING etc.) já vêm no endpoint
# por item (/seller-promotions/items/{id}) como candidatas — não precisa varrer as
# campanhas do vendedor. Pro PAINEL mostramos TODAS as candidatas (inclusive as
# 'pending'/programadas); a trava de vigência vale só pra DECISÃO do robô (abaixo).
def processar_item(item_id, access, sid, detalhes):
    """Processa UM anúncio (só leitura) e devolve o dict da sugestão, ou None.
    Sem gravar no banco — é chamado em paralelo por várias threads."""
    try:
        ofertas = ofertas_do_item(item_id, access)
        if not isinstance(ofertas, list) or not ofertas:
            return None
        ativas_raw = [o for o in ofertas if isinstance(o, dict) and eh_ativa(o)]
        # AMPLIADO: antes só cofinanciadas (meli_percentage). Agora TODA candidata com preço
        # avaliável — inclui relâmpago (LIGHTNING), Pix, DOD etc. (mp=0 é tratado em avaliar()).
        cand_raw = [o for o in ofertas if isinstance(o, dict)
                    and (o.get("status") or "").lower() == "candidate"
                    and o.get("original_price") and preco_oferta(o)]
        # candidatas de preço aberto (price = 0 + faixa). Elas NÃO entram na
        # decisão do robô, mas não podem fazer o anúncio inteiro desaparecer.
        tem_aberta = any(isinstance(o, dict)
                         and (o.get("status") or "").lower() == "candidate"
                         and o.get("original_price")
                         and not preco_oferta(o) and preco_exibicao(o)
                         for o in ofertas)
        if not ativas_raw and not cand_raw and not tem_aberta:
            return None
        it = detalhes.get(item_id)
        if not isinstance(it, dict):
            st, it = get(f"/items/{item_id}", access)
        if not isinstance(it, dict):
            return None
        preco = it.get("price")
        ltid = it.get("listing_type_id")
        cat = it.get("category_id")
        sku = it.get("seller_sku") or it.get("seller_custom_field")
        titulo = it.get("title")
        tradicional = not bool(it.get("catalog_listing"))   # tradicional = fora do catálogo
        custo = custo_efetivo(item_id, sku)
        if custo is None:
            # tem promoção disponível, mas sem custo não dá pra avaliar margem —
            # registra pra você saber quais cadastrar (aparece no painel)
            with _sc_lock:
                SEM_CUSTO.append({"seller_id": str(sid), "item_id": item_id,
                                  "sku": sku, "titulo": titulo})
            return None
        frete, frete_origem = frete_de(sku, item_id, access)
        piso, grupo = margem_minima_do(sku)
        ativa = None
        for o in ativas_raw:
            ev = avaliar(o, cat, ltid, access, frete, custo)
            if ev:
                ativa = ev
                break
        cand_todas = [avaliar(o, cat, ltid, access, frete, custo) for o in cand_raw]
        cand_todas = [c for c in cand_todas if c]     # TODAS as candidatas (p/ EXIBIR no painel)
        # NÃO recomenda promoção que ainda não está vigente (programada/futura) nem já encerrada.
        # cand_vigente consulta o detalhe da promoção quando a data não vem no candidato.
        # Esta trava vale só pra DECISÃO do robô — no painel mostramos todas (inclusive pending).
        cand = [c for c in cand_todas if cand_vigente(c["o"], access)]
        seguras = [c for c in cand if c["margem"] >= piso]
        # ---- de preço aberto: avaliadas pelo valor SUGERIDO, só para a tela ----
        # Ficam fora de cand/cand_todas de propósito: se entrassem, virariam
        # candidatas a 'alvo' e o robô tentaria aplicar um preço que ninguém
        # escolheu. Nas contas que aplicam de verdade isso seria grave.
        cand_abertas = []
        for _o in ofertas:
            if not (isinstance(_o, dict) and (_o.get("status") or "").lower() == "candidate"
                    and _o.get("original_price")):
                continue
            if preco_oferta(_o):
                continue                      # essa já está na decisão
            _pe = preco_exibicao(_o)
            if not _pe:
                continue
            _o2 = dict(_o)
            _o2["price"] = _pe
            _ev = avaliar(_o2, cat, ltid, access, frete, custo)
            if _ev:
                _ev["faixa"] = (_o.get("min_discounted_price"), _o.get("max_discounted_price"))
                cand_abertas.append(_ev)
        # ---- decide a ação (sua regra) ----
        # NUNCA sai de uma promoção que já está — mesmo abaixo do piso. Só TROCA se houver
        # uma que respeite o piso E pague MAIS (recebe maior). Senão, MANTÉM (com alerta se abaixo).
        # ESCOLHA da promoção:
        #  - TRADICIONAL (fora do catálogo): usa a ESTRATÉGIA configurada (equilibrado = mira a
        #    margem padrão; agressivo = maior desconto; conservador = menor desconto).
        #  - CATÁLOGO: mantém a lógica competitiva (menor preço / quem paga mais).
        alerta = None
        alvo = None
        if ativa:
            if ativa["margem"] < piso:
                alerta = "ativa_abaixo_piso"
            melhores = [a for a in seguras if a["recebe"] > ativa["recebe"] + 0.01]
            if melhores:
                acao = "trocar"
                alvo = (escolher_alvo(melhores, piso, ESTRATEGIA) if tradicional
                        else max(melhores, key=lambda a: a["recebe"]))
            else:
                acao = "manter"
        else:
            if seguras:
                acao = "entrar"
                alvo = (escolher_alvo(seguras, piso, ESTRATEGIA) if tradicional
                        else min(seguras, key=lambda a: a["pb"]))
            else:
                # Nada a recomendar. Continua devolvendo None — a
                # repricer_sugestoes fica idêntica ao que é hoje. Antes de
                # sair, guarda o retrato do anúncio com TODAS as campanhas
                # (cand_todas inclui as programadas) para o painel da Arcos.
                with _vt_lock:
                    VITRINE.append({
                        "seller_id": str(sid), "item_id": item_id, "sku": sku,
                        "titulo": titulo, "preco_atual": preco,
                        "custo": custo, "custo_envio": frete,
                        "custo_envio_origem": frete_origem,
                        "estoque": it.get("available_quantity"),
                        "grupo": grupo, "margem_minima": piso,
                        "tem_ativa": False,
                        "ofertas": [_oferta_dict(_c, False, False, None, access)
                                    for _c in sorted(cand_todas + cand_abertas,
                                        key=lambda x: (x["margem"] if x.get("margem") is not None else -999),
                                        reverse=True)],
                        "motivo": f"nenhuma campanha vigente acima do piso de {piso:.0f}%",
                    })
                return None
        rejeitadas = " | ".join(
            _rotulo(a) + (" (ok)" if a["margem"] >= piso else f" (<{piso:.0f}%)")
            for a in sorted(cand, key=lambda x: x["pb"]) if a is not alvo
        )
        # ---- LISTA DO PAINEL: mostra TODAS as campanhas do item (igual ao Pricebot) ----
        # A DECISÃO do robô (acima) usa só 'cand' (vigentes). Aqui, pra EXIBIR, usamos
        # 'cand_todas' — inclui as candidatas 'pending'/programadas (ex.: SMART "TOP SELLERS"
        # que ainda não começou), que a trava de vigência tira da decisão mas o Pricebot mostra.
        ofertas_lst = []
        if ativa:
            ofertas_lst.append(_oferta_dict(ativa, True, acao == "manter", acao, access))
        for _c in sorted(cand_todas + cand_abertas,
                         key=lambda x: (x["margem"] if x.get("margem") is not None else -999),
                         reverse=True):
            ofertas_lst.append(_oferta_dict(_c, False, _c is alvo, acao, access))
        sug = {
            "seller_id": str(sid),
            "item_id": item_id,
            "sku": sku,
            "titulo": titulo,
            "preco_atual": preco,
            "acao": acao,
            "alerta": alerta,
            "tem_ativa": bool(ativa),
            "ativa_nome": (ativa["o"].get("name") if ativa else None),
            "ativa_preco": (ativa["pb"] if ativa else None),
            "ativa_margem": (ativa["margem"] if ativa else None),
            "custo": custo,
            "custo_envio": frete,
            "custo_envio_origem": frete_origem,
            "estoque": it.get("available_quantity"),
            "grupo": grupo,
            "margem_minima": piso,
            "alternativas": max(len(seguras) - (1 if alvo in seguras else 0), 0),
            "ofertas": ofertas_lst,
            "rejeitadas": rejeitadas,
            "status": "ok" if acao == "manter" else "pendente",
        }
        if alvo:
            o = alvo["o"]
            sug.update({
                "promocao_id": o.get("id"),
                "promocao_ref_id": o.get("ref_id"),
                "promocao_nome": o.get("name"),
                "promocao_tipo": o.get("type"),
                "promocao_inicio": _data_promo(o, "start_date"),
                "promocao_fim": _data_promo(o, "finish_date", "end_date"),
                "preco_comprador": alvo["pb"],
                "seller_percentage": alvo["sp"],
                "meli_percentage": alvo["mp"],
                "tarifa_venda": alvo["tarifa"],
                "recebe_liquido": alvo["recebe"],
                "margem_resultante": alvo["margem"],
            })
        elif acao == "manter" and ativa:
            ao = ativa["o"]
            sug.update({
                "promocao_nome": ao.get("name"),
                "promocao_tipo": ao.get("type"),
                "promocao_inicio": _data_promo(ao, "start_date"),
                "promocao_fim": _data_promo(ao, "finish_date", "end_date"),
                "preco_comprador": ativa["pb"],
                "recebe_liquido": ativa["recebe"],
                "margem_resultante": ativa["margem"],
            })
        return sug
    except Exception as e:
        print(f"  erro ao processar {item_id}: {e}", flush=True)
        return None
def _linha_log(s):
    at = (f" | ATIVA {s.get('ativa_nome')} R${s.get('ativa_preco')} margem {s.get('ativa_margem')}%"
          if s.get("tem_ativa") else "")
    alvo_txt = ""
    if s["acao"] in ("entrar", "trocar") and s.get("promocao_nome"):
        alvo_txt = (f" -> {s['promocao_nome']} R${s.get('preco_comprador')} "
                    f"recebe R${s.get('recebe_liquido')} ({s.get('margem_resultante')}%)")
    flag = " ⚠️ABAIXO DO PISO" if s.get("alerta") else ""
    return f"[{s['acao']}]{flag} {s['item_id']} {str(s.get('titulo'))[:26]}{at}{alvo_txt} (piso {s['margem_minima']})"
def gravar_em_lote(sugs, tam=200):
    """Devolve True se TODOS os lotes entraram. Isso passou a importar: a
    limpeza da rodada anterior agora depende de a nova ter entrado inteira."""
    ok = True
    for i in range(0, len(sugs), tam):
        try:
            sb.table("repricer_sugestoes").insert(sugs[i:i + tam]).execute()
        except Exception as e:
            ok = False
            print(f"  erro ao gravar lote {i}: {e}", flush=True)
    return ok
def main():
    """GRAVAR ANTES, APAGAR DEPOIS.

    Antes, esta função começava apagando as sugestões e só gravava as novas
    no fim — e a rodada leva minutos. Nessa janela a tabela ficava vazia, e
    QUALQUER leitura pegava a conta inteira sem campanha: o painel abrindo,
    um F5, a atualização automática. Não adianta ensinar cada leitor a
    esperar; enquanto existir um buraco, alguém cai nele.

    Agora não existe buraco. As linhas novas entram PRIMEIRO, convivendo com
    as velhas; só depois de tudo gravado é que as da rodada anterior saem. A
    tela sempre pega a linha mais nova de cada anúncio, então durante a
    troca ela vê uma foto completa — parte nova, parte velha, nenhuma
    faltando.

    E se a gravação falhar no meio, a limpeza NÃO acontece: fica valendo a
    rodada anterior inteira, que é melhor que meia rodada nova.
    """
    preload()
    total_sug = 0
    contadores = {"entrar": 0, "trocar": 0, "sair": 0, "manter": 0}
    SEM_CUSTO.clear()
    VITRINE.clear()
    tudo_ok = True
    # marco da rodada: o que for mais velho que isto é da rodada anterior
    T0 = datetime.now(timezone.utc).isoformat()
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        if SELLER_ID_FILTRO and str(sid) != SELLER_ID_FILTRO:
            continue                              # roda SÓ a conta escolhida (ex.: testar a CF)
        print(f"\n===== CONTA {sid} =====", flush=True)
        ids, total = todos_ativos(sid, access)
        cap = f" (varrendo {len(ids)}, limite de teste MAX_ITENS={MAX_ITENS})" if MAX_ITENS else ""
        print(f"itens ativos: {len(ids)} de {total if total is not None else '?'}{cap}", flush=True)
        detalhes = detalhes_itens(ids, access)
        # processa os itens em paralelo (só leitura); grava em lote no fim
        sugs = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for sug in ex.map(lambda i: processar_item(i, access, sid, detalhes), ids):
                if sug:
                    sugs.append(sug)
        for s in sugs:
            contadores[s["acao"]] = contadores.get(s["acao"], 0) + 1
            print(_linha_log(s), flush=True)
        tudo_ok = gravar_em_lote(sugs) and tudo_ok
        total_sug += len(sugs)
        print(f"--- conta {sid}: {len(sugs)} registros gravados ---", flush=True)
    # a vitrine tem chave (seller_id, item_id), então é upsert: a linha do
    # anúncio é reescrita no lugar, sem nunca deixar de existir. O criado_em
    # vai explícito para marcar a rodada — no upsert o valor padrão da coluna
    # não é reaplicado, e sem isso a linha nova pareceria velha na limpeza.
    if VITRINE:
        try:
            for v in VITRINE:
                v["criado_em"] = T0
            for i in range(0, len(VITRINE), 200):
                sb.table("promo_vitrine").upsert(VITRINE[i:i + 200],
                                                 on_conflict="seller_id,item_id").execute()
            print(f"vitrine: {len(VITRINE)} anúncios gravados (só para o painel da Arcos)", flush=True)
        except Exception as e:
            tudo_ok = False
            print("Aviso: não consegui gravar a vitrine:", e, flush=True)

    # ---- só agora sai a rodada anterior ----
    if tudo_ok:
        try:
            q = sb.table("repricer_sugestoes").delete().neq("status", "aplicada").lt("criado_em", T0)
            if SELLER_ID_FILTRO:
                q = q.eq("seller_id", SELLER_ID_FILTRO)
            q.execute()
        except Exception as e:
            print("Aviso: não consegui limpar as sugestões antigas:", e, flush=True)
        try:
            q = sb.table("promo_vitrine").delete().lt("criado_em", T0)
            if SELLER_ID_FILTRO:
                q = q.eq("seller_id", SELLER_ID_FILTRO)
            q.execute()
        except Exception as e:
            print("Aviso: não consegui limpar a vitrine antiga:", e, flush=True)
    else:
        print("ATENÇÃO: houve falha ao gravar — NÃO apaguei a rodada anterior. "
              "Fica valendo o dado de antes, inteiro.", flush=True)

    # O sem_custo continua sendo apagado e regravado: ele não alimenta o
    # painel da Arcos, e eu não conheço as colunas dessa tabela bem o
    # bastante para trocar o método sem olhar.
    try:
        q = sb.table("repricer_sem_custo").delete().neq("item_id", "")
        if SELLER_ID_FILTRO:
            q = q.eq("seller_id", SELLER_ID_FILTRO)
        q.execute()
    except Exception as e:
        print("Aviso: não consegui limpar sem_custo:", e, flush=True)
    if SEM_CUSTO:
        try:
            for i in range(0, len(SEM_CUSTO), 200):
                sb.table("repricer_sem_custo").insert(SEM_CUSTO[i:i + 200]).execute()
        except Exception as e:
            print("Aviso: não consegui gravar sem_custo:", e, flush=True)
    resumo = ", ".join(f"{k}: {v}" for k, v in contadores.items())
    print(f"\n=== {total_sug} registros gravados ({resumo}) | {len(SEM_CUSTO)} sem custo — nada foi aplicado no ML ===", flush=True)
if __name__ == "__main__":
    main()
