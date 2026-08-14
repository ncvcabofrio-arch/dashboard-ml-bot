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
# nome da org da casa: o que separa "catálogo da casa" de "catálogo de
# cliente". Mesmo valor do ORG_BI do puxador e do ORG_CASA da ml_oauth.
ORG_CASA = os.environ.get("ORG_CASA", "pontomusical")
CUSTOS = {}          # sku -> custo
CUSTO_ITEM = {}      # item_id -> custo (pra anúncios SEM SKU, preenchido no painel)
SKU_ITEM = {}        # item_id -> sku ESCRITO NA MÃO (pra anúncio que não tem SKU no ML)
PISOS = {}           # sku -> (margem_minima, nome_grupo)
_pct_lock = threading.Lock()
SEM_CUSTO = []       # anúncios com promoção disponível MAS sem custo cadastrado (pra cadastrar)
_sc_lock = threading.Lock()
# ---------------------------------------------------------------------------
# DE ONDE VEM O SKU DO ANÚNCIO
#
# O Mercado Livre guarda o SKU do vendedor em TRÊS lugares, e por muito tempo
# este robô olhou só dois:
#
#     item.seller_custom_field       campo antigo (ERP, formulário velho)
#     item.attributes[SELLER_SKU]    formulário ATUAL do ML
#     variations[].(os dois acima)   quando o anúncio tem variação
#
# O preço disso apareceu na IRMAOS_BROTHERS: 227 anúncios, vários com SKU no
# Mercado Livre, e ZERO SKU chegando aqui. Zero exato nunca é "não preencheu"
# — é campo que ninguém leu. Confirmado item a item pelo diagnóstico:
# seller_custom_field=None, atributo SELLER_SKU='40P5'.
#
# O contador SKU_ORIGEM existe pra isso não voltar a passar despercebido:
# toda rodada imprime de onde os SKUs vieram.
# ---------------------------------------------------------------------------
SKU_ORIGEM = {}      # origem -> quantos anúncios (só pro log)
_sku_lock = threading.Lock()
def _txt(v):
    """Texto limpo de um campo que pode vir nulo, número ou string."""
    return "" if v is None else str(v).strip()
def _attr_sku(bloco):
    """SKU guardado como ATRIBUTO 'SELLER_SKU'. Serve pro item e pra variação.
    Devolve None pra atributo em branco: SKU vazio é pior que SKU nenhum,
    porque casaria com qualquer produto sem SKU do catálogo."""
    if not isinstance(bloco, dict):
        return None
    for a in (bloco.get("attributes") or []):
        if isinstance(a, dict) and a.get("id") == "SELLER_SKU":
            return _txt(a.get("value_name")) or None
    return None
def sku_do_item(it):
    """O SKU do anúncio, procurado nos três lugares, nesta ordem:
        1. seller_custom_field / seller_sku    (campos de nível de anúncio)
        2. atributo SELLER_SKU                  (formulário atual do ML)
        3. dentro das variações                 SÓ SE TODAS CONCORDAREM
    O item 3 tem essa condição por um motivo prático: um anúncio cujas
    variações têm SKUs DIFERENTES é mais de um produto no mesmo lugar.
    Escolher um deles daria um custo único pra coisas de custo diferente —
    margem errada com cara de certa. Nesse caso devolvo None de propósito e
    conto como 'variacoes_divergentes', pra virar decisão sua no painel em
    vez de chute meu aqui.
    """
    if not isinstance(it, dict):
        return None
    sku = _txt(it.get("seller_custom_field")) or _txt(it.get("seller_sku")) or None
    origem = "campo_antigo" if sku else None
    if not sku:
        sku = _attr_sku(it)
        origem = "atributo_SELLER_SKU" if sku else None
    if not sku:
        vs = []
        for v in (it.get("variations") or []):
            if not isinstance(v, dict):
                continue
            s = _txt(v.get("seller_custom_field")) or _txt(v.get("seller_sku")) or _attr_sku(v)
            if s:
                vs.append(s)
        distintos = set(vs)
        if len(distintos) == 1:
            sku, origem = vs[0], "variacao"
        elif len(distintos) > 1:
            origem = "variacoes_divergentes"        # fica SEM sku, de propósito
    with _sku_lock:
        k = origem or "sem_sku_nenhum"
        SKU_ORIGEM[k] = SKU_ORIGEM.get(k, 0) + 1
    return sku
def _resumo_sku():
    """Devolve e ZERA o contador de origens (uma linha por conta no log)."""
    with _sku_lock:
        if not SKU_ORIGEM:
            return "nenhum anuncio avaliado"
        txt = " · ".join(f"{k}={v}" for k, v in sorted(SKU_ORIGEM.items()))
        SKU_ORIGEM.clear()
    return txt
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
def _todas_linhas(tabela, cols, passo=1000, org=None):
    """Lê a tabela INTEIRA paginando (PostgREST devolve no máx 1000 linhas por vez).
    org: se vier, filtra por essa coluna. Existe por causa da
    produtos_repricer, que guarda o catálogo de vários donos na mesma
    tabela - ler ela inteira e confiar no sku seria juntar catálogo de
    clientes diferentes num mapa só.
    """
    linhas, ini = [], 0
    while True:
        q = sb.table(tabela).select(cols)
        if org is not None:
            q = q.eq("org", org)
        lote = (q.range(ini, ini + passo - 1).execute().data) or []
        linhas += lote
        if len(lote) < passo:
            break
        ini += passo
    return linhas
def org_da_conta(seller_id):
    """A org (o dono) de uma conta. Devolve None se não souber.
    None é tratado como CASA em todo lugar que usa isto - é o
    comportamento de antes, e não faz o robô ir procurar catálogo numa
    tabela de cliente por causa de um dado faltando.
    """
    if not seller_id:
        return None
    try:
        d = (sb.table("contas").select("org")
             .eq("seller_id", str(seller_id)).limit(1).execute().data) or []
        return (d[0].get("org") if d else None) or None
    except Exception:
        return None
def preload(seller_id=None):
    """Carrega custos e grupos de uma vez só, pra não bater no banco por item.
    DE QUAL CATÁLOGO: depende de quem é a conta.
        casa    -> tabela 'produtos'          (a de sempre)
        cliente -> tabela 'produtos_repricer' (chave (org, sku))
    Sem este desvio, um SKU do cliente igual a um SKU da casa faria ele
    receber o CUSTO DA CASA - e margem errada não parece errada. Este
    robô roda como service_role, então o RLS não protege aqui: a
    separação tem que ser explícita no código.
    Chamar sem seller_id mantém o comportamento antigo (só a casa), pra
    não quebrar quem chama assim.
    """
    org = org_da_conta(seller_id)
    da_casa = (org is None) or (org == ORG_CASA)
    tabela = "produtos" if da_casa else "produtos_repricer"
    # LIMPA antes de encher. Sem isto, rodar duas contas de donos
    # diferentes na mesma execução deixaria os custos da primeira no mapa
    # da segunda - exatamente o vazamento que este desvio existe para
    # evitar, só que por dentro do processo em vez de por dentro da tabela.
    CUSTOS.clear()
    try:
        if da_casa:
            linhas = _todas_linhas("produtos", "sku, custo")
        else:
            # o filtro por org é cinto: a chave já é (org, sku), mas ler a
            # tabela inteira e confiar no sku seria voltar ao problema
            linhas = _todas_linhas("produtos_repricer", "sku, custo", org=org)
        for r in linhas:
            if r.get("sku") and r.get("custo") is not None:
                CUSTOS[r["sku"]] = float(r["custo"])
        print(f"catálogo: {tabela}" + (f" (org {org})" if not da_casa else ""), flush=True)
    except Exception as e:
        print("Aviso: não consegui pré-carregar custos:", e, flush=True)
    # Da mesma tabela saem DUAS coisas, e por muito tempo a segunda ficou
    # parada: o custo por anúncio (pra quem não tem SKU) e o SKU ESCRITO
    # NA MÃO. A coluna 'sku' já existia aqui e ninguém lia - o custo_efetivo
    # só usava o 'custo'.
    #
    # Ler o sku muda o que dá pra fazer: um anúncio sem SKU no Mercado
    # Livre passa a poder apontar pra um produto do catálogo, e aí UM custo
    # serve o Premium e o Clássico do mesmo item. Sem isso, cada anúncio
    # precisaria do próprio custo digitado - 414 deles, nas contas novas.
    CUSTO_ITEM.clear()
    SKU_ITEM.clear()
    try:
        for r in _todas_linhas("repricer_custo_item", "item_id, sku, custo"):
            iid = r.get("item_id")
            if not iid:
                continue
            if r.get("custo") is not None:
                CUSTO_ITEM[iid] = float(r["custo"])
            if (r.get("sku") or "").strip():
                SKU_ITEM[iid] = r["sku"].strip()
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
    """Custo do anúncio, em três tentativas, nesta ordem:
      1. SKU do próprio anúncio (o que o Mercado Livre devolve) -> catálogo
      2. SKU ESCRITO NA MÃO para este anúncio -> catálogo
      3. custo digitado só para este anúncio
    A ordem importa. O SKU do ML vem primeiro porque é o que o vendedor
    mantém; o manual é remendo para quem ainda não preencheu lá. E o custo
    por anúncio fica por último porque é o menos reaproveitável: vale para
    um anúncio só, enquanto um SKU serve o Premium e o Clássico do mesmo
    produto de uma vez.
    """
    c = CUSTOS.get(sku) if sku else None
    if c is not None:
        return c
    sku_manual = SKU_ITEM.get(item_id)
    if sku_manual:
        c = CUSTOS.get(sku_manual)
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
    """Busca detalhes de VÁRIOS itens de uma vez (multiget, 20 por chamada).

    'attributes' e 'variations' entraram na lista porque é ONDE O SKU MORA
    nos anúncios feitos pelo formulário atual do ML. Sem eles, o multiget
    devolve seller_custom_field=None e o anúncio chega aqui como se não
    tivesse SKU — foi o que aconteceu com os 227 anúncios da IRMAOS.

    Custo disso: a resposta fica maior, principalmente em anúncio com
    muitas variações. Se a rodada ficar lenta, o primeiro corte é tirar
    'variations' e ficar só com 'attributes', que já resolve a maioria.
    """
    out = {}
    attrs = ("id,price,listing_type_id,category_id,seller_sku,seller_custom_field,"
             "attributes,variations,title,status,available_quantity,catalog_listing")
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
    return {"nome": o.get("name"), "tipo": o.get("type"), "promocao_id": o.get("id"), "promocao_ref_id": o.get("ref_id"),
            "rebate": a.get("mp"), "desconto_vendedor": a.get("sp"),
            "preco": a.get("pb"), "inicio": ini, "fim": fim,
            "margem": a.get("margem"), "ativa": ativa_flag,
            "recomendada": recomendada_flag,
            "acao": (acao if recomendada_flag else None)}
# ---------------------------------------------------------------------------
# DESCONTO INDIVIDUAL (PRICE_DISCOUNT) — informar, não decidir.
#
# Este tipo chega do ML com price=0 e promotion_id nulo. Não é dado faltando: é o
# ML dizendo "o preço é do vendedor". O que ele manda é a FAIXA CRÍVEL, em
# min/max_discounted_price (conferido em itens reais: min = 20% do preço cheio, ou
# seja 80% de desconto; max = 5% a 10% de desconto, calculado pelo ML item a item).
#
# Como avaliar() exige preço, o cand_raw de processar_item descartava essa oferta e
# ela NUNCA chegava ao painel — quem montava uma promoção individual escolhia a
# margem no escuro e só descobria se era possível depois de tentar aplicar.
#
# O que entra aqui é SÓ INFORMAÇÃO: a margem nos dois extremos da faixa. O desconto
# individual continua FORA da decisão automática (não entra em 'cand' nem 'seguras')
# — escolher desconto próprio é decisão do vendedor na tela, não do robô.
# ---------------------------------------------------------------------------
def faixa_preco_livre(o, cat, ltid, access, frete, custo):
    """Margem nos pontos que interessam de uma campanha de PREÇO PRÓPRIO.

    Vale para PRICE_DISCOUNT, DEAL, SELLER_CAMPAIGN, DOD — todas as que chegam com
    price=0 e faixa. A doc do ML é explícita: "Valor 0 quando o item é candidato", e
    min/max/suggested_discounted_price existem justamente para DEAL, PRICE_DISCOUNT e
    SELLER_CAMPAIGN. Aqui não filtramos por tipo: quem manda é o formato da resposta.

    Calcula em três pontos:
      max_discounted_price  -> menor desconto permitido -> MAIOR margem possível
      min_discounted_price  -> maior desconto permitido -> MENOR margem
      suggested_discounted_price (quando vem) -> o preço que o ML sugere

    O sugerido importa muito na DEAL: a doc avisa que um deal_price fora dos descontos
    sugeridos volta 400 ERROR_CREDIBILITY_DISCOUNTED_PRICE.

    Custa até 3 avaliações por campanha; a comissão é cacheada por (categoria, tipo de
    anúncio) em _pct_cache, então quase não gera chamada nova. Devolve None se a faixa
    não vier — nunca inventa preço."""
    mn, mx = o.get("min_discounted_price"), o.get("max_discounted_price")
    sug, p0 = o.get("suggested_discounted_price"), o.get("original_price")
    if not p0:
        return None
    if mn is None and mx is None:
        # O ML listou a campanha como CANDIDATA mas não informou a faixa — visto de
        # verdade na SELLER_CAMPAIGN "ARCOS BASE - 08-26", que vem com price=0 e nada
        # mais. Sem faixa não há margem a calcular, mas ESCONDER a campanha é pior:
        # ela existe, você pode entrar nela, e sumir da tela é como se não existisse.
        # Devolvemos a linha marcada como sem_faixa para a tela dizer isso na cara.
        return {"o": o, "sem_faixa": True,
                "preco_min": None, "margem_min": None,
                "preco_max": None, "margem_max": None,
                "preco_sug": None, "margem_sug": None,
                "desconto_min_pct": None, "desconto_max_pct": None, "desconto_sug_pct": None}
    try:
        p0 = float(p0)
        mx = float(mx) if mx is not None else p0
        mn = float(mn) if mn is not None else mx
        sug = float(sug) if sug is not None else None
    except (TypeError, ValueError):
        return None
    if mn <= 0 or mx <= 0 or mx < mn:
        return None
    base = dict(o)
    def _av(pb):
        if pb is None:
            return None
        base["price"] = round(float(pb), 2)
        return avaliar(base, cat, ltid, access, frete, custo)
    topo, fundo, meio = _av(mx), _av(mn), _av(sug)
    if not topo:
        return None
    _d = lambda p: (round((1 - float(p) / p0) * 100, 1) if p else None)
    return {"o": o,
            "preco_max": round(mx, 2), "margem_max": topo["margem"],
            "preco_min": round(mn, 2), "margem_min": (fundo["margem"] if fundo else None),
            "preco_sug": (round(sug, 2) if sug is not None else None),
            "margem_sug": (meio["margem"] if meio else None),
            "desconto_min_pct": _d(mx), "desconto_max_pct": _d(mn),
            "desconto_sug_pct": _d(sug)}
def _oferta_preco_livre_dict(fx, access=None):
    """Linha do painel para uma campanha de preço próprio. Diferente das cofinanciadas:
    não tem preço nem margem única — tem uma FAIXA, porque quem escolhe é o vendedor.

    'preco'/'margem' levam o SUGERIDO pelo ML quando ele existe, porque é o valor que a
    plataforma aceita sem discutir (na DEAL, fugir dele dá 400). Sem sugestão — caso do
    desconto individual — levam o extremo de maior margem. A faixa inteira vai nos
    campos próprios, pra tela poder mostrar o espaço de decisão."""
    o = fx["o"]
    tipo = (o.get("type") or "").upper()
    pref = fx["preco_sug"] if fx["preco_sug"] is not None else fx["preco_max"]
    mref = fx["margem_sug"] if fx["margem_sug"] is not None else fx["margem_max"]
    nome = o.get("name") or ("Desconto individual" if tipo == "PRICE_DISCOUNT" else tipo)
    ini, fim = _data_promo(o, "start_date"), _data_promo(o, "finish_date", "end_date")
    if (ini is None or fim is None) and access is not None and o.get("id"):
        det = _promo_detalhe(o.get("id"), tipo, access)
        if isinstance(det, dict):
            ini = ini or _data_promo(det, "start_date")
            fim = fim or _data_promo(det, "finish_date", "end_date")
    return {"nome": nome, "tipo": tipo,
            "promocao_id": o.get("id"), "promocao_ref_id": o.get("ref_id"),
            "rebate": o.get("meli_percentage"), "desconto_vendedor": o.get("seller_percentage"),
            "preco": pref, "inicio": ini, "fim": fim,
            "margem": mref, "ativa": False, "recomendada": False, "acao": None,
            "preco_livre": True, "individual": (tipo == "PRICE_DISCOUNT"),
            "sem_faixa": bool(fx.get("sem_faixa")),
            "preco_min": fx["preco_min"], "preco_max": fx["preco_max"], "preco_sug": fx["preco_sug"],
            "margem_min": fx["margem_min"], "margem_max": fx["margem_max"], "margem_sug": fx["margem_sug"],
            "desconto_min_pct": fx["desconto_min_pct"], "desconto_max_pct": fx["desconto_max_pct"],
            "desconto_sug_pct": fx["desconto_sug_pct"]}
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
        # CAMPANHAS DE PREÇO PRÓPRIO (não cofinanciadas): candidato SEM preço mas COM
        # faixa. São as DEAL (organizadas pelo ML, com a vitrine da plataforma), as
        # SELLER_CAMPAIGN, a DOD e o desconto individual. Todas caíam fora do cand_raw
        # pela mesma linha — o filtro preco_oferta(o) — e por isso NUNCA apareciam no
        # painel: você só via as cofinanciadas, que são as únicas que vêm com preço.
        # Não filtramos por tipo aqui: quem decide é o formato (price vazio + faixa).
        # Ficam separadas de cand_raw de propósito — são informação pro painel, não
        # candidatas à decisão automática do robô.
        livres_raw = [o for o in ofertas if isinstance(o, dict)
                      and (o.get("status") or "").lower() == "candidate"
                      and not preco_oferta(o)
                      ]   # SEM exigir faixa: quando o ML não a informa, a campanha
                          # aparece assim mesmo, marcada como "faixa não informada".
        if not ativas_raw and not cand_raw:
            return None
        it = detalhes.get(item_id)
        if not isinstance(it, dict):
            st, it = get(f"/items/{item_id}", access)
        if not isinstance(it, dict):
            return None
        preco = it.get("price")
        ltid = it.get("listing_type_id")
        cat = it.get("category_id")
        sku = sku_do_item(it)      # <- os TRÊS lugares, não só os dois campos antigos
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
        for _c in sorted(cand_todas, key=lambda x: (x["margem"] if x.get("margem") is not None else -999), reverse=True):
            ofertas_lst.append(_oferta_dict(_c, False, _c is alvo, acao, access))
        # preço próprio por último: são opções do vendedor, não recomendação do robô.
        # UMA linha por campanha (antes era uma só por anúncio, o que bastava quando só
        # existia o desconto individual — com DEAL/SELLER_CAMPAIGN cada campanha é uma
        # oportunidade distinta, com nome, vitrine e faixa próprios).
        for _i in livres_raw:
            _fx = faixa_preco_livre(_i, cat, ltid, access, frete, custo)
            if _fx:
                ofertas_lst.append(_oferta_preco_livre_dict(_fx, access))
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
    for i in range(0, len(sugs), tam):
        try:
            sb.table("repricer_sugestoes").insert(sugs[i:i + tam]).execute()
        except Exception as e:
            print(f"  erro ao gravar lote {i}: {e}", flush=True)
def main():
    # o preload de custos agora acontece DENTRO do laço das contas, porque
    # cada conta pode ter um dono - e um dono, um catálogo. Aqui fora fica
    # só o que é comum a todas (grupos, custo por anúncio).
    preload()
    total_sug = 0
    contadores = {"entrar": 0, "trocar": 0, "sair": 0, "manter": 0}
    SEM_CUSTO.clear()
    try:
        q = sb.table("repricer_sugestoes").delete().neq("status", "aplicada")  # limpa tudo menos aplicadas
        if SELLER_ID_FILTRO:
            q = q.eq("seller_id", SELLER_ID_FILTRO)                            # só a conta em teste
        q.execute()
    except Exception as e:
        print("Aviso: não consegui limpar pendentes:", e, flush=True)
    try:
        q = sb.table("repricer_sem_custo").delete().neq("item_id", "")         # limpa a lista anterior
        if SELLER_ID_FILTRO:
            q = q.eq("seller_id", SELLER_ID_FILTRO)
        q.execute()
    except Exception as e:
        print("Aviso: não consegui limpar sem_custo:", e, flush=True)
    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        if SELLER_ID_FILTRO and str(sid) != SELLER_ID_FILTRO:
            continue                              # roda SÓ a conta escolhida (ex.: testar a CF)
        print(f"\n===== CONTA {sid} =====", flush=True)
        preload(sid)          # catálogo do DONO desta conta (limpa o da anterior)
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
        # De onde vieram os SKUs desta conta. Existe pra um zero suspeito nunca
        # mais passar despercebido: se 'atributo_SELLER_SKU' aparecer alto, era
        # exatamente o que o robô estava perdendo antes.
        print(f"    SKU dos anuncios avaliados: {_resumo_sku()}", flush=True)
        for s in sugs:
            contadores[s["acao"]] = contadores.get(s["acao"], 0) + 1
            print(_linha_log(s), flush=True)
        gravar_em_lote(sugs)
        total_sug += len(sugs)
        print(f"--- conta {sid}: {len(sugs)} registros gravados ---", flush=True)
    # grava os "sem custo" (promoção disponível mas sem custo cadastrado)
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
