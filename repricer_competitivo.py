"""
FASE 1 — SONDA COMPETITIVA (somente leitura, NÃO escreve nada no ML nem no banco).
Cruza cada anúncio de CATÁLOGO com a concorrência (price_to_win + concorrentes na PDP)
e a sua margem, e imprime a DECISÃO que o robô tomaria — pra validarmos a régua antes
de ligar no painel/aplicador.

Régua (definida com o vendedor):
  - Perdendo por PREÇO: alvo = price_to_win, TRAVADO no piso da etiqueta (18% ou grupo).
      * price_to_win >= preço-piso  -> DESCONTAR até o price_to_win (via PRICE_DISCOUNT/co-financiada).
      * price_to_win <  preço-piso  -> NÃO PERSEGUIR (ganhar daria prejuízo). Alerta.
  - Perdendo por motivo NÃO-preço (reputação, envio, Full, manufacturing): não desconta, reporta motivo.
  - GANHANDO: se há folga até o 2º lugar, pode SUBIR o preço (reduzir desconto) até logo
      abaixo do concorrente pra maximizar margem. Se já está no cheio, nada a fazer.
  - Sem concorrente / não-catálogo: sem visão competitiva (fica pra política por etiqueta).

Uso: SELLER_ID=<conta>  [MAX_ITENS=30]  [MARGEM_MIN=18]
Só leitura: usa GET /items/{id}, /items/{id}/price_to_win, /products/{pid}/items.
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import repricer_sugestoes as rec
from ml_auth import obter_access

API = rec.API
WORKERS = int(os.environ.get("WORKERS", "8"))   # itens processados em paralelo
# TRAVA DE SEGURANÇA (gatilho): piso absoluto que nenhuma etiqueta pode furar.
# Vazio = desligada (respeita as etiquetas como estão, mesmo negativas).
_pmin_abs = (os.environ.get("PISO_MIN_ABS") or "").strip()
PISO_MIN_ABS = float(_pmin_abs) if _pmin_abs else None
DEBUG = (os.environ.get("DEBUG") or "") == "1"
DEBUG_ITEM = (os.environ.get("DEBUG_ITEM") or "").strip()   # analisa só 1 item, verboso
NORM_CUSTO = {}   # sku normalizado -> sku original na tabela produtos (só p/ DIAGNÓSTICO)
MEUS_SELLERS = set()   # TODOS os seus seller_ids (as 3 contas) — nunca competir consigo mesmo
MATCH = {}             # item_id -> {product_ids, confianca, tipo} (mapa confirmado com IA)
CONTROLE = {}          # item_id -> {ativo, piso_override, undercut_override, pma, preco_manual} (painel)
CONFIG = {}            # chave -> valor (regras globais editáveis no painel: undercut, piso etc.)


def _norm(s):
    return re.sub(r"\s+", "", str(s)).upper() if s else ""


def _parece_kit(titulo):
    """Título com 'kit' ou 'set' => conjunto (não casa com página de produto avulso)."""
    return bool(re.search(r"\bkit\b|\bset\b", (titulo or "").lower()))


# bundle = item + extras (não é concorrente do item pelado). Sinais que raramente
# dão falso-positivo (um 'Pedal' de bateria NÃO é flagado; um 'X + pedal' é).
_BUNDLE_RE = re.compile(
    r"\+|\bkit\b|\bcombo\b|\bpacote\b|\bbrinde\b|bon[eé]|\be microfone\b"
    r"|com (suporte|pedal|afinador|acess)", re.I)   # 'com capa/bag' NÃO é bundle (é padrão)


def _parece_bundle(nome):
    return bool(_BUNDLE_RE.search(nome or ""))


def carregar_match():
    """Carrega o mapa (chave->produto(s)) confirmado, paginando. A chave é o SKU
    ou, quando o anúncio não tem SKU, o próprio item_id. Silencioso se não existir."""
    ini = 0
    while True:
        try:
            lote = (rec.sb.table("repricer_match").select("*")
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: sem tabela repricer_match ainda: {e})", flush=True)
            return
        for r in lote:
            if r.get("sku"):
                MATCH[r["sku"]] = r
        if len(lote) < 1000:
            break
        ini += 1000


def carregar_controle():
    """Carrega o controle por anúncio (painel): liga/desliga, regra individual, PMA.
    Silencioso se a tabela não existir. Linha ausente = usa as regras globais."""
    ini = 0
    while True:
        try:
            lote = (rec.sb.table("repricer_controle").select("*")
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: sem tabela repricer_controle ainda: {e})", flush=True)
            return
        for r in lote:
            if r.get("item_id"):
                CONTROLE[r["item_id"]] = r
        if len(lote) < 1000:
            break
        ini += 1000


def carregar_config():
    """Carrega as regras globais editáveis no painel (repricer_config: chave->valor)."""
    try:
        for r in (rec.sb.table("repricer_config").select("chave,valor").execute().data or []):
            if r.get("chave"):
                CONFIG[r["chave"]] = r.get("valor")
    except Exception as e:
        print(f"(aviso: sem tabela repricer_config ainda: {e})", flush=True)


SELLER_ID = (os.environ.get("SELLER_ID") or "").strip()
MAX_ITENS = int(os.environ.get("MAX_ITENS", "30"))
EPS = 0.01
# Quanto ficar ABAIXO do alvo (mais barato que o menor concorrente / abaixo do 2º lugar).
# Antes era 1 centavo; agora R$5 por padrão pra ficar claramente na frente. Editável por env.
UNDERCUT = float(os.environ.get("UNDERCUT", "5"))


def _pct_cat(cat, ltid, access):
    """% de comissão fixa da categoria (cacheada por (cat,ltid)). None se a API não trouxer."""
    p = rec._percentual(cat, ltid, access)
    return float(p) if p is not None else None


def margem_no_preco(pb, cat, ltid, frete, custo, access):
    """'Você recebe' = preço - comissão - frete (meli%=0). Comissão = % fixa da categoria.
    Determinístico e sem chamada por preço (usa a % cacheada)."""
    pct = _pct_cat(cat, ltid, access)
    com = (pb * pct / 100.0) if pct is not None else (rec.comissao(round(pb, 2), cat, ltid, access) or 0)
    recebe = pb - com - frete
    margem = ((recebe - custo) / pb * 100) if pb else -999
    return margem, round(com, 2), round(recebe, 2)


def preco_piso(piso_pct, cat, ltid, frete, custo, access, teto):
    """Menor preço que ainda rende a margem mínima — FÓRMULA FECHADA (sem busca binária):
       margem = (1 - pct/100) - (frete+custo)/pb ; resolvendo margem = piso.
    Retorna None se nem no preço cheio a margem alcança o piso (item já abaixo do piso)."""
    pct = _pct_cat(cat, ltid, access)
    if pct is None:
        return None
    denom = (1 - pct / 100.0) - piso_pct / 100.0
    if denom <= 0:
        return None
    pb = (frete + custo) / denom
    if pb > teto + 0.01:     # precisaria de preço acima do cheio -> já abaixo do piso
        return None
    return round(pb, 2)


def price_to_win(item_id, access):
    st, d = rec.get(f"/items/{item_id}/price_to_win?version=v2", access)
    return d if isinstance(d, dict) else None


def concorrentes(product_id, sid, access):
    """Preços dos concorrentes na página de produto. Exclui TODAS as suas contas
    (MEUS_SELLERS) — nunca conta você mesmo como concorrente. Retorna lista de floats."""
    st, d = rec.get(f"/products/{product_id}/items?limit=50", access)
    res = d.get("results") if isinstance(d, dict) else None
    precos = []
    for r in (res or []):
        try:
            s = str(r.get("seller_id"))
            if s not in MEUS_SELLERS and s != str(sid) and r.get("price"):
                precos.append(float(r["price"]))
        except (TypeError, ValueError):
            pass
    return sorted(precos)


def gtin_do_item(it):
    """Extrai o primeiro EAN/GTIN do anúncio (atributos ou variações)."""
    def _busca(attrs):
        for a in (attrs or []):
            if a.get("id") == "GTIN" and a.get("value_name"):
                return str(a["value_name"]).split(",")[0].strip()
        return None
    g = _busca(it.get("attributes"))
    if g:
        return g
    for v in (it.get("variations") or []):
        g = _busca(v.get("attributes"))
        if g:
            return g
    return None


# palavras genéricas que não ajudam a busca (categoria, ligações, cores, qualificadores)
_GENERICO = {
    "de", "da", "do", "com", "sem", "para", "por", "e", "ou", "a", "o", "em", "no", "na",
    "c/", "p/", "kit", "conjunto", "set",
    "sintetizador", "teclado", "teclados", "teclas", "tecla", "piano", "controlador",
    "violao", "violão", "guitarra", "contrabaixo", "baixo", "cavaco", "cavaquinho", "ukulele",
    "microfone", "mic", "fone", "fones", "ouvido", "headset", "headphone", "auricular",
    "interface", "placa", "audio", "áudio", "caixa", "som", "ativa", "passiva", "amplificador",
    "cabecote", "cabeçote", "pedal", "pedaleira", "prato", "pratos", "bateria", "mesa", "mixer",
    "estante", "suporte", "capa", "bag", "case", "cabo", "sistema", "fio", "arranjador",
    "profissional", "digital", "eletronico", "eletrônico", "eletroacustico", "eletroacústico",
    "acustico", "acústico", "eletrico", "elétrico", "portatil", "portátil", "compacto", "compacta",
    "novo", "nova", "original", "premium", "dinamico", "dinâmico", "condensador", "duplo", "mao", "mão",
    "preto", "preta", "branco", "branca", "azul", "vermelho", "vermelha", "rosa", "cinza", "prata",
    "polegadas", "pol", "w", "watts",
}


def consulta_do_item(it):
    """Busca curta (marca+modelo) extraída do TÍTULO: tira genéricos e números soltos,
    mantém as ~5 palavras que importam. Ex.: 'Teclado Casio Ct-x800 61 Teclas' -> 'Casio Ct-x800'."""
    titulo = it.get("title") or ""
    sig = []
    for t in re.findall(r"[0-9A-Za-zÀ-ÿ/\-]+", titulo):
        tl = t.lower().strip("/-")
        if not tl or len(tl) <= 1 or tl in _GENERICO or tl.isdigit():
            continue
        sig.append(t)
        if len(sig) >= 5:
            break
    return " ".join(sig) if sig else (titulo[:40] or None)


FAIXA_MIN, FAIXA_MAX = 0.40, 3.0   # concorrente crível: entre 40% e 300% do seu preço


def _busca_catalogo(q, sid, access, tipo, p0, nome_chave=None):
    """Acha produto(s) de catálogo (por EAN ou por texto) e lista os concorrentes deles.
    Substitui /sites/search (bloqueado, 403). Filtra por nome do produto (modelo) e por
    faixa de preço sã, pra não pegar produto/acessório errado. Exclui você."""
    from urllib.parse import quote
    if tipo == "ean":
        url = f"/products/search?status=active&site_id=MLB&product_identifier={q}"
    else:
        url = f"/products/search?status=active&site_id=MLB&q={quote(str(q))}"
    st, d = rec.get(url, access)
    prods = (d.get("results") if isinstance(d, dict) else None) or []
    lo, hi = FAIXA_MIN * p0, FAIXA_MAX * p0
    precos, usado, pulados = [], None, 0
    for prod in prods[:6]:
        nome = str(prod.get("name") or "")
        if nome_chave and nome_chave.lower() not in nome.lower():
            pulados += 1
            continue   # produto não bate com o modelo -> ignora
        if _parece_bundle(nome):
            pulados += 1
            continue   # bundle (item+extras) -> não é concorrente do item pelado
        if not prod.get("id"):
            continue
        c = [x for x in concorrentes(prod["id"], sid, access) if lo <= x <= hi]
        if c:                      # usa só o produto MAIS RELEVANTE que casou (não mistura)
            precos = c
            usado = (prod["id"], nome)
            break
    if DEBUG:
        alvo = f"{usado[0]} '{usado[1][:45]}'" if usado else "nenhum"
        print(f"    [products/search {tipo}='{str(q)[:40]}'] HTTP {st} | produtos={len(prods)} "
              f"| fora-do-modelo: {pulados} | usado: {alvo} | faixa R${lo:.0f}-{hi:.0f} "
              f"| conc={len(precos)} menor R${min(precos):.2f}" if precos else
              f"    [products/search {tipo}='{str(q)[:40]}'] HTTP {st} | produtos={len(prods)} "
              f"| fora-do-modelo: {pulados} | usado: nenhum | conc=0", flush=True)
    return sorted(precos)


def concorrencia_mercado(ean, consulta, titulo, sid, access, p0):
    """Concorrentes via CATÁLOGO (a busca de listagens /sites/search é bloqueada pelo ML):
       1) EAN -> produto de catálogo -> concorrentes
       2) marca+modelo (texto) -> produto cujo NOME contém o modelo -> concorrentes
       Retorna (precos_ordenados, origem). Exclui você."""
    if ean:
        precos = _busca_catalogo(ean, sid, access, "ean", p0)
        if precos:
            return precos, f"EAN {ean}"
    if consulta:
        chave = consulta.split()[0] if consulta else None   # 1º token = modelo (ex.: 'Mininova')
        precos = _busca_catalogo(consulta, sid, access, "q", p0, nome_chave=chave)
        if precos:
            return precos, f"busca '{consulta}'"
    return [], None


def sale_price(item_id, access):
    """Preço de venda ATUAL (com promoção, se houver) + tipo de promoção ativa.
    A promoção NÃO muda item.price; é isto que revela o que o cliente paga de fato
    e se há oferta rolando. Retorna (amount, promotion_type)."""
    st, d = rec.get(f"/items/{item_id}/sale_price?context=channel_marketplace", access)
    if not isinstance(d, dict):
        return None, None
    amt = d.get("amount")
    ptipo = (d.get("metadata") or {}).get("promotion_type")
    try:
        return (float(amt) if amt is not None else None), ptipo
    except (TypeError, ValueError):
        return None, ptipo


def analisar(item_id, access, sid):
    st, it = rec.get(f"/items/{item_id}?include_attributes=all", access)
    if not isinstance(it, dict):
        return None
    raw_aq = it.get("available_quantity")
    try:
        aq = int(raw_aq)
    except (TypeError, ValueError):
        aq = None
    if aq is not None and aq <= 0:
        return {"item_id": item_id, "titulo": it.get("title"), "aq": raw_aq, "acao": "sem_estoque",
                "detalhe": "estoque zero"}
    p0 = float(it.get("price") or 0)               # preço CHEIO (de tabela) — base do desconto
    pv, promo_ativa = sale_price(item_id, access)  # preço REAL que o cliente paga + promoção ativa
    pv = pv if pv else p0                           # sem sale_price -> usa o cheio
    cat, ltid = it.get("category_id"), it.get("listing_type_id")
    sku = it.get("seller_sku") or it.get("seller_custom_field")
    pid = it.get("catalog_product_id")
    catalog_listing = bool(it.get("catalog_listing"))   # SÓ True = compete de fato no catálogo
    ctl = CONTROLE.get(item_id) or {}
    if ctl.get("ativo") is False:                        # liga/desliga por anúncio (painel)
        return {"item_id": item_id, "titulo": it.get("title"), "sku": sku, "aq": raw_aq,
                "acao": "desligado", "detalhe": "robô desligado neste anúncio (controle)"}
    uc = (float(ctl["undercut_override"]) if ctl.get("undercut_override") is not None
          else float(CONFIG.get("undercut") or UNDERCUT))
    pma = float(ctl["pma"]) if ctl.get("pma") is not None else None
    custo = rec.custo_de(sku)
    if custo is None or not p0:
        nk = _norm(sku)
        if nk and nk in NORM_CUSTO:
            extra = f" [ESTÁ na tabela como '{NORM_CUSTO[nk]}' — só diferença de formato!]"
        else:
            extra = " [não está na tabela produtos]"
        return {"item_id": item_id, "titulo": it.get("title"), "sku": sku, "aq": raw_aq,
                "acao": "sem_custo", "detalhe": f"sem custo (SKU={sku or '—'}){extra}"}
    frete, _ = rec.frete_de(sku, item_id, access)
    piso, grupo = rec.margem_minima_do(sku)
    if ctl.get("piso_override") is not None:     # regra individual do item (painel)
        piso = float(ctl["piso_override"])
    piso_orig = piso
    if PISO_MIN_ABS is not None:                 # trava de segurança ligada
        piso = max(piso, PISO_MIN_ABS)
    pmin = preco_piso(piso, cat, ltid, frete, custo, access, p0)
    if pma is not None:                          # PMA (MAP): nunca anuncia abaixo disso
        pmin = pma if pmin is None else max(pmin, pma)
    m_cheio, _, _ = margem_no_preco(p0, cat, ltid, frete, custo, access)

    base = {"item_id": item_id, "titulo": it.get("title"), "sku": sku, "aq": raw_aq,
            "grupo": grupo, "piso": piso, "piso_orig": piso_orig, "preco_cheio": round(p0, 2),
            "preco_venda": round(pv, 2), "promo": promo_ativa,
            "margem_cheio": round(m_cheio, 1), "preco_piso": pmin, "pma": pma, "catalog": catalog_listing}

    if not catalog_listing:
        m = MATCH.get(sku) or MATCH.get(item_id)   # sem SKU -> busca pelo item_id
        if m:
            # ITEM MAPEADO (match confirmado) -> exato, agrega páginas duplicadas
            if (m.get("confianca") == "nenhum") or (m.get("tipo") in ("kit", "sem_concorrente")):
                base.update({"acao": "sem_match",
                             "detalhe": f"mapeado ({m.get('tipo')}) — não precifica por catálogo"})
                return base
            pids = m.get("product_ids") or []
            lo, hi = FAIXA_MIN * p0, FAIXA_MAX * p0
            precos = sorted([x for pid in pids for x in concorrentes(pid, sid, access) if lo <= x <= hi])
            origem = f"mapa ({len(pids)}p)"
        else:
            # NÃO mapeado -> trava anti-kit + busca fuzzy (estimativa)
            if _parece_kit(it.get("title")):
                base.update({"acao": "sem_match",
                             "detalhe": "parece kit/conjunto — não precifica por catálogo (não mapeado)"})
                return base
            ean = gtin_do_item(it)
            consulta = consulta_do_item(it)
            precos, origem = concorrencia_mercado(ean, consulta, it.get("title"), sid, access, p0)
            if origem:
                origem += " (estimativa)"
        ctx = "elegível ao catálogo, sem opt-in" if pid else "fora do catálogo"
        if not precos:
            base.update({"acao": "sem_match",
                         "detalhe": f"{ctx}; sem concorrente encontrado"})
            return base
        menor = precos[0]
        base.update({"origem": origem, "n_conc": len(precos), "conc_min": round(menor, 2)})
        if menor > pv + EPS:
            base.update({"acao": "ja_competitivo",
                         "detalhe": f"[{origem}] já é o mais barato: seu R${pv:.2f} < menor conc. "
                                    f"R${menor:.2f} ({len(precos)} conc)"})
        elif pmin is None:
            base.update({"acao": "nao_perseguir_ean",
                         "detalhe": f"[{origem}] já abaixo do piso no preço cheio -> não dá pra descontar"})
        elif (menor - uc) >= pmin:
            alvo = round(menor - uc, 2)
            m_alvo, _, _ = margem_no_preco(alvo, cat, ltid, frete, custo, access)
            desc = (1 - alvo / p0) * 100
            base.update({"acao": "descontar_ean", "alvo": alvo, "margem_alvo": round(m_alvo, 1),
                         "detalhe": f"[{origem}] {len(precos)} conc, menor R${menor:.2f} -> "
                                    f"descontar p/ R${alvo:.2f} (R${uc:.0f} abaixo; {desc:.1f}% off, "
                                    f"margem {m_alvo:.1f}%) [piso={piso}% pmin=R${pmin}]"})
        else:
            # mercado abaixo do piso: desconta até o PISO (mantém 18%, o mais competitivo possível)
            alvo = round(pmin, 2)
            m_alvo, _, _ = margem_no_preco(alvo, cat, ltid, frete, custo, access)
            desc = (1 - alvo / p0) * 100
            base.update({"acao": "descontar_piso", "alvo": alvo, "margem_alvo": round(m_alvo, 1),
                         "detalhe": f"[{origem}] menor conc R${menor:.2f} < piso -> descontar até o PISO "
                                    f"R${alvo:.2f} ({desc:.1f}% off, margem {m_alvo:.1f}%; não bate o concorrente)"})
        return base

    ptw = price_to_win(item_id, access)
    if not ptw:
        base.update({"acao": "sem_dado", "detalhe": "price_to_win indisponível"})
        return base
    status = ptw.get("status")
    alvo = ptw.get("price_to_win")
    winner = (ptw.get("winner") or {})
    reason = ptw.get("reason") or []
    base.update({"status": status, "price_to_win": alvo,
                 "winner_price": winner.get("price"), "reason": ", ".join(reason)})

    if status == "winning":
        precos = concorrentes(pid, sid, access)
        segundo = precos[0] if precos else None
        base["segundo"] = segundo
        if segundo and (segundo - uc) > pv + EPS:
            alvo_subir = round(segundo - uc, 2)
            m_seg, _, _ = margem_no_preco(alvo_subir, cat, ltid, frete, custo, access)
            base.update({"acao": "subir_margem", "alvo_subir": alvo_subir,
                         "detalhe": f"ganhando; 2º lugar R${segundo:.2f} > seu R${pv:.2f} "
                                    f"-> pode subir até R${alvo_subir:.2f} (R${uc:.0f} abaixo do 2º, "
                                    f"margem {m_seg:.1f}%)"})
        else:
            base.update({"acao": "manter_ganhando", "detalhe": "ganhando, sem folga p/ subir"})
        return base

    # perdendo (competing / listed / sharing_first_place)
    reason_preco = (alvo is not None)
    if not reason_preco:
        base.update({"acao": "perde_nao_preco", "detalhe": f"perde por: {base['reason'] or 'motivo não-preço'}"})
        return base
    alvo = float(alvo)
    if pmin is None:
        base.update({"acao": "nao_perseguir",
                     "detalhe": f"price_to_win R${alvo:.2f}; já abaixo do piso no cheio -> não dá pra descontar"})
    elif alvo >= pmin:
        m_alvo, _, _ = margem_no_preco(alvo, cat, ltid, frete, custo, access)
        desc = (1 - alvo / p0) * 100
        base.update({"acao": "descontar", "alvo": round(alvo, 2), "margem_alvo": round(m_alvo, 1),
                     "detalhe": f"descontar até R${alvo:.2f} ({desc:.1f}% off) -> margem {m_alvo:.1f}%"})
    else:
        alvo2 = round(pmin, 2)
        m_alvo, _, _ = margem_no_preco(alvo2, cat, ltid, frete, custo, access)
        desc = (1 - alvo2 / p0) * 100
        base.update({"acao": "descontar_piso", "alvo": alvo2, "margem_alvo": round(m_alvo, 1),
                     "detalhe": f"price_to_win R${alvo:.2f} < piso -> descontar até o PISO R${alvo2:.2f} "
                                f"({desc:.1f}% off, margem {m_alvo:.1f}%)"})
    return base


def main():
    if not SELLER_ID:
        print("Defina SELLER_ID (ex 177795203).", flush=True); return
    print(">>> SONDA v7 (mapa por SKU + anti-bundle) <<<", flush=True)
    rec.preload()
    carregar_match()
    carregar_controle()
    carregar_config()
    print(f"mapa de match carregado: {len(MATCH)} itens", flush=True)
    # autentica TODAS as contas: guarda os seller_ids (p/ não competir consigo) e pega o access da escolhida
    access = sid = None
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if s:
            MEUS_SELLERS.add(str(s))
        if str(s) == str(SELLER_ID):
            access, sid = a, s
    if not access:
        print(f"não autentiquei a conta {SELLER_ID}.", flush=True); return
    print(f"suas contas (não contam como concorrente): {', '.join(sorted(MEUS_SELLERS))}", flush=True)

    for k in rec.CUSTOS:
        NORM_CUSTO[_norm(k)] = k
    amostra = ", ".join(list(rec.CUSTOS.keys())[:15])
    print(f"amostra de SKUs na tabela 'produtos': {amostra}\n", flush=True)

    if DEBUG_ITEM:
        ids, total = [DEBUG_ITEM], "?"
    else:
        ids, total = rec.todos_ativos(sid, access)
        ids = ids[:MAX_ITENS]
    print(f"===== SONDA COMPETITIVA | conta {sid} | amostra {len(ids)} de {total} (só leitura, pula estoque 0) =====\n", flush=True)

    # processa os itens EM PARALELO (só leitura); mantém a ordem original
    if DEBUG_ITEM:
        resultados = [analisar(i, access, sid) for i in ids]
    else:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            resultados = list(ex.map(lambda i: analisar(i, access, sid), ids))

    TAGS = {"descontar": "🎯", "descontar_ean": "🎯", "descontar_piso": "🔻",
            "nao_perseguir": "🛑", "nao_perseguir_ean": "🛑", "subir_margem": "⬆️",
            "manter_ganhando": "🏆", "ja_competitivo": "✅", "perde_nao_preco": "⚠️",
            "sem_concorrencia": "·", "sem_match": "🔍", "sem_dado": "?", "sem_custo": "∅"}
    cont = {}
    linhas = []
    for r in resultados:
        if not r:
            continue
        cont[r["acao"]] = cont.get(r["acao"], 0) + 1
        linhas.append(r)
        if r["acao"] == "sem_estoque":
            continue   # não polui a saída; conta no resumo
        print(f"{TAGS.get(r['acao'], '·')} [{r['acao']}] {r['item_id']} est={r.get('aq')} "
              f"{str(r.get('titulo'))[:30]} | cheio R${r.get('preco_cheio')} "
              f"(margem {r.get('margem_cheio')}%) | {r.get('detalhe')}", flush=True)

    print("\n=== RESUMO: " + ", ".join(f"{k}: {v}" for k, v in cont.items()) + " ===", flush=True)
    cat = sum(1 for l in linhas if l.get("catalog"))
    print(f"competindo no catálogo (opt-in feito): {cat}/{len(linhas)} — só esses têm price_to_win.", flush=True)

    faltando = [l for l in linhas if l["acao"] == "sem_custo"]
    if faltando:
        print(f"\n--- {len(faltando)} SEM CUSTO (preencher na tabela 'produtos') ---", flush=True)
        for l in faltando:
            print(f"   {l['item_id']} | SKU={l.get('sku') or '—'} | {str(l.get('titulo'))[:45]}", flush=True)

    baixos = [l for l in linhas if (l.get("piso_orig") is not None and l["piso_orig"] < 0)]
    if PISO_MIN_ABS is not None:
        print(f"\n🔒 trava de segurança LIGADA: piso nunca abaixo de {PISO_MIN_ABS:.0f}%.", flush=True)
    elif baixos:
        grupos = sorted({f"{l.get('grupo')} ({l['piso_orig']:.0f}%)" for l in baixos})
        print(f"\n⚠️ LEMBRETE: trava de segurança DESLIGADA e {len(baixos)} item(ns) com piso NEGATIVO "
              f"— grupo(s): {', '.join(grupos)}. Esses podem descontar até dar prejuízo.\n"
              f"   Pra proteger: rode com piso_minimo (ex. 18) OU corrija o grupo em 'repricer_grupos'.", flush=True)

    print("\nNada foi escrito. É só a leitura da régua competitiva.", flush=True)


if __name__ == "__main__":
    main()
