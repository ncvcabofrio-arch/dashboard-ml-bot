"""
PILOTO — repricer completo em UMA conta (padrão Cabo Frio 471489691), com trilhos e reconciliador.
Lê a sonda + as vendas recentes + o controle por anúncio, e ACERTA o estado de cada item:
  perdendo + PARADO      -> cria desconto (Central de Promoções) no alvo competitivo (piso/PMA)
  perdendo + VENDENDO    -> não desconta; se já tiver desconto nosso, REMOVE (protege margem)
  ganhando / barato demais -> sem desconto; remove o nosso se houver
Nunca mexe no preço do anúncio. Padrão = SIMULAÇÃO. Só escreve com CONFIRMA=SIM e ATIVO=SIM.
Trilhos: conta travada · teto de CRIAÇÕES/rodada · anti-salto · piso do grupo (+PMA) · gate de
vendas (2 dias) · auto-exclusão das suas contas · botão de pânico · log de auditoria · Telegram.
As REMOÇÕES (que protegem margem) não contam no teto.
"""
import os
import time
from datetime import date, timedelta, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import requests
import repricer_sugestoes as rec
import repricer_competitivo as sonda
from ml_auth import obter_access
def _rs(v):
    """R$ no formato BR (1234.5 -> '1.234,50'). Sem o 'R$' na frente."""
    try:
        return format(float(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)
def _agora_iso():
    return datetime.now(timezone.utc).isoformat()
# --- API da Central de Promoções (autossuficiente: NÃO depende do repricer_aplicar) ---
STATUS_ATIVA = {"started", "active", "in_progress", "ongoing", "pending"}
_HTTP = getattr(rec, "_SESSION", None) or requests.Session()   # keep-alive: reaproveita conexão do sonda
def req(method, path, access, body=None, tent=2):
    h = {"Authorization": "Bearer " + access, "Content-Type": "application/json"}
    r = None
    for i in range(tent):
        r = _HTTP.request(method, rec.API + path, headers=h, json=body, timeout=25)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1)); continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)
def eh_ativa(o):
    if (o.get("status") or "").lower() in STATUS_ATIVA:
        return True
    return str(o.get("ref_id") or "").upper().startswith("OFFER-")
def promos_do_item(item_id, access):
    st, d = req("GET", f"/seller-promotions/items/{item_id}?app_version=v2", access)
    return d if isinstance(d, list) else []
SELLER_ID = (os.environ.get("SELLER_ID") or "471489691").strip()   # Cabo Frio por padrão
MAX_ITENS = int(os.environ.get("MAX_ITENS", "0"))                    # 0 = todos
WORKERS = int(os.environ.get("WORKERS", "16"))                       # análise em paralelo
CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
ATIVO = (os.environ.get("ATIVO") or "SIM").strip().upper() == "SIM"  # botão de pânico
# regras globais — resolvidas em resolver_config() (input do workflow > painel/repricer_config > default)
MAX_ALTERACOES = 0     # teto de CRIAÇÕES por rodada (0 = sem teto)
MAX_DROP_PCT = 35.0    # anti-salto (%)
DIAS = 14              # duração do desconto (rede de segurança, máx 14)
VENDAS_DIAS = 5        # janela do gate de vendas (dias)
VENDAS_MIN = 1         # vendeu >= isso no período -> não desconta
# --- subida gradual de margem (recupera preço nos "barato demais", sempre abaixo do concorrente) ---
SUBIR_ATIVO = True     # liga/desliga a subida gradual
SUBIR_PASSO_PCT = 8.0  # teto de % que o preço pode subir POR RODADA (gradual)
SUBIR_MIN_RS = 5.0     # só sobe se o ganho for pelo menos isso (R$), pra não mexer à toa
SUBIR_EXIGE_VENDA = True  # só sobe o próximo degrau se VENDEU desde a última subida (subiu e não vendeu -> segura)
# --- itens SEM concorrente: sobe por demanda (a cada N pedidos +X%), passo atrás se parar/cair ---
SEMC_ATIVO = True      # liga/desliga a regra dos itens sem concorrente
SEMC_PEDIDOS = 5       # a cada N pedidos desde a última subida, sobe
SEMC_PCT = 3.0         # quanto sobe (e desce no passo atrás), em %
SEMC_PAROU_DIAS = 2    # dias sem vender (desde a última subida) que contam como "parou" -> passo atrás
SEMC_CAIU_PCT = 50.0   # "caiu o ritmo": pedidos/dia depois da subida < esse % do que era antes -> passo atrás
SEMC_COOLDOWN_DIAS = 7  # anti-loop: depois de um passo atrás, espera isso antes de poder subir de novo
def _num(v, tipo, padrao):
    try:
        return tipo(v)
    except (TypeError, ValueError):
        return padrao
def resolver_config():
    """Regras globais: input do workflow (env, por rodada) > painel (repricer_config) > default."""
    global MAX_ALTERACOES, MAX_DROP_PCT, DIAS, VENDAS_DIAS, VENDAS_MIN
    global SUBIR_ATIVO, SUBIR_PASSO_PCT, SUBIR_MIN_RS, SUBIR_EXIGE_VENDA
    global SEMC_ATIVO, SEMC_PEDIDOS, SEMC_PCT, SEMC_PAROU_DIAS, SEMC_CAIU_PCT, SEMC_COOLDOWN_DIAS
    C = sonda.CONFIG
    def r(env_name, chave, padrao, tipo):
        v = os.environ.get(env_name)
        if v is None or v == "":
            v = C.get(chave)
        return _num(v, tipo, padrao) if v not in (None, "") else padrao
    # com limites sãos: um valor torto (ex.: conta no campo errado) é ignorado, não quebra
    MAX_ALTERACOES = max(0, r("MAX_ALTERACOES", "teto_alteracoes", 0, int))
    MAX_DROP_PCT = max(0.0, min(r("MAX_DROP_PCT", "anti_salto_pct", 35.0, float), 100.0))
    DIAS = max(1, min(r("DIAS", "dias", 14, int), 14))
    VENDAS_DIAS = max(1, min(r("VENDAS_DIAS", "vendas_dias", 5, int), 365))
    VENDAS_MIN = max(0, r("VENDAS_MIN", "vendas_min", 1, int))
    _sa = os.environ.get("SUBIR_ATIVO") or C.get("subir_ativo") or "1"
    SUBIR_ATIVO = str(_sa).strip().upper() not in ("0", "NAO", "NÃO", "FALSE", "OFF", "")
    SUBIR_PASSO_PCT = max(0.0, min(r("SUBIR_PASSO_PCT", "subir_passo_pct", 8.0, float), 100.0))
    SUBIR_MIN_RS = max(0.0, r("SUBIR_MIN_RS", "subir_min_rs", 5.0, float))
    _sev = os.environ.get("SUBIR_EXIGE_VENDA") or C.get("subir_exige_venda") or "1"
    SUBIR_EXIGE_VENDA = str(_sev).strip().upper() not in ("0", "NAO", "NÃO", "FALSE", "OFF", "")
    _sca = os.environ.get("SEMC_ATIVO") or C.get("semc_ativo") or "1"
    SEMC_ATIVO = str(_sca).strip().upper() not in ("0", "NAO", "NÃO", "FALSE", "OFF", "")
    SEMC_PEDIDOS = max(1, r("SEMC_PEDIDOS", "semc_pedidos", 5, int))
    SEMC_PCT = max(0.0, min(r("SEMC_PCT", "semc_pct", 3.0, float), 100.0))
    SEMC_PAROU_DIAS = max(1, r("SEMC_PAROU_DIAS", "semc_parou_dias", 2, int))
    SEMC_CAIU_PCT = max(0.0, min(r("SEMC_CAIU_PCT", "semc_caiu_pct", 50.0, float), 100.0))
    SEMC_COOLDOWN_DIAS = max(0, r("SEMC_COOLDOWN_DIAS", "semc_cooldown_dias", 7, int))
ACOES_DESCONTO = {"descontar", "descontar_ean", "descontar_piso"}
REMOVER_OK = {"subir_margem", "ja_competitivo", "manter_ganhando"}   # confiante que não precisa desconto
_CANCEL = ("cancel",)   # só cancelada NÃO conta como venda (paid e partially_refunded contam)
NOSSAS_PROMO = {"PRICE_DISCOUNT", "custom", "CUSTOM"}   # ofertas individuais (nossas); resto = campanha do ML
def promo_estado(a):
    """(tem_pd, tem_outra) a partir do sale_price JÁ lido pela sonda — sem chamada extra.
    tem_pd = desconto nosso ativo; tem_outra = DEAL/campanha cofinanciada do ML."""
    p = a.get("promo")
    if not p:
        return False, False
    return (p in NOSSAS_PROMO), (p not in NOSSAS_PROMO)
def melhor_cofin(a, access):
    """Melhor promoção COFINANCIADA (candidata) por MARGEM %, respeitando o piso.
    Reusa o avaliar() do repricer_sugestoes (que já trata o meli_percentage — a parte
    que o ML banca). Retorna o dict do avaliar (pb, margem, recebe, o) ou None."""
    custo, frete, piso = a.get("custo"), a.get("frete"), a.get("piso")
    cat, ltid = a.get("cat"), a.get("ltid")
    if custo is None or frete is None or piso is None:
        return None
    try:
        ofertas = rec.ofertas_do_item(a["item_id"], access)
    except Exception:
        return None
    seguras = []
    for o in (ofertas if isinstance(ofertas, list) else []):
        if not (isinstance(o, dict) and (o.get("status") or "").lower() == "candidate"
                and o.get("meli_percentage") and o.get("original_price")):
            continue
        ev = rec.avaliar(o, cat, ltid, access, frete, custo)
        if ev and ev["margem"] >= piso:
            seguras.append(ev)
    return max(seguras, key=lambda x: x["margem"]) if seguras else None   # BALIZADOR = margem %
def telegram(msg):
    tok = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=15)
    except Exception:
        pass
def logar(row):
    try:
        rec.sb.table("repricer_log").insert(row).execute()
    except Exception as e:
        print(f"   (log falhou: {e})", flush=True)
def _estoque_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
def base_log(sid, a):
    return {"seller_id": str(sid), "item_id": a.get("item_id"), "sku": a.get("sku"),
            "titulo": a.get("titulo"), "preco_cheio": a.get("preco_cheio"),
            "margem_alvo": a.get("margem_alvo"), "margem_cheio": a.get("margem_cheio"),
            "conc_min": a.get("conc_min") or a.get("segundo"), "price_to_win": a.get("price_to_win"),
            "piso": a.get("piso"), "grupo": a.get("grupo"), "motivo": a.get("detalhe"),
            "estoque": _estoque_int(a.get("aq")), "tipo_anuncio": a.get("tipo_anuncio"),
            "catalog": a.get("catalog"), "catalog_pid": a.get("catalog_pid"),
            "vinculado_mlb": a.get("vinculado_mlb"),
            "preco_venda": a.get("preco_venda"), "margem_venda": a.get("margem_venda"),
            "status_ml": a.get("status"), "winner_price": a.get("winner_price"),
            # O frete JÁ estava calculado e era jogado fora aqui. Sem ele no log, o
            # painel ficava sem custo de envio em todo anúncio que só existe nesta
            # tabela - e sem frete a margem sai maior do que é.
            "frete": a.get("frete")}
def _datas():
    hoje = date.today()
    fim = hoje + timedelta(days=DIAS - 1)
    return f"{hoje.isoformat()}T00:00:00", f"{fim.isoformat()}T00:00:00"
def carregar_vendas(sid, dias):
    """Unidades vendidas (NÃO canceladas) nos últimos `dias`, por item_id e por sku.
    FONTE: repricer_vendas, e não mais a tabela 'vendas'.
    A 'vendas' é do BI: ela junta Mercado Livre e Shopee e é enchida pelo
    puxador do financeiro. Enquanto o repricer lia de lá, ligar uma conta
    de CLIENTE aqui obrigava a puxar os pedidos dele para dentro do DRE
    da casa - dois sistemas presos pela mesma tabela.
    A repricer_vendas é do repricer: só Mercado Livre, só o que este robô
    consome, e para todas as orgs. Conferimos as duas lado a lado antes
    de trocar, na janela de 5 dias que este portão usa: itens iguais nas
    três contas da casa (7/7, 63/63, 5/5). A única diferença era um item
    da Shopee, que não é anúncio do ML e não deve entrar aqui mesmo.
    As colunas são as mesmas, então o resto desta função não mudou.
    """
    corte = (date.today() - timedelta(days=dias)).isoformat()
    por_item, por_sku, ini = {}, {}, 0
    while True:
        try:
            lote = (rec.sb.table("repricer_vendas")
                    .select("item_id,sku,quantidade,status,data_aprovacao")
                    .eq("seller_id", str(sid)).gte("data_aprovacao", corte)
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: não li vendas: {e})", flush=True)
            return por_item, por_sku
        for r in lote:
            if any(x in str(r.get("status") or "").lower() for x in _CANCEL):
                continue
            try:
                q = int(r.get("quantidade") or 0)
            except (TypeError, ValueError):
                q = 0
            if r.get("item_id"):
                por_item[r["item_id"]] = por_item.get(r["item_id"], 0) + q
            if r.get("sku"):
                por_sku[r["sku"]] = por_sku.get(r["sku"], 0) + q
        if len(lote) < 1000:
            break
        ini += 1000
    return por_item, por_sku
def unidades(a, por_item, por_sku):
    u = por_item.get(a.get("item_id"), 0)
    if a.get("sku"):
        u = max(u, por_sku.get(a["sku"], 0))
    return u
def _dia(s):
    return str(s)[:10] if s else ""   # 'YYYY-MM-DD' (compara por dia, sem dor de cabeça de fuso)
def ultima_venda_dia(sid, dias=90):
    """Dia da venda mais recente (não cancelada) por item_id, nos últimos `dias`. Pra saber
    se houve venda DEPOIS da última subida de preço (só aí vale subir mais um degrau)."""
    corte = (date.today() - timedelta(days=dias)).isoformat()
    it, ini = {}, 0
    while True:
        try:
            lote = (rec.sb.table("repricer_vendas").select("item_id,status,data_aprovacao")
                    .eq("seller_id", str(sid)).gte("data_aprovacao", corte)
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: não li datas de venda: {e})", flush=True); return it
        for r in lote:
            if any(x in str(r.get("status") or "").lower() for x in _CANCEL):
                continue
            iid, d = r.get("item_id"), _dia(r.get("data_aprovacao"))
            if iid and d and d > it.get(iid, ""):
                it[iid] = d
        if len(lote) < 1000:
            break
        ini += 1000
    return it
def ultimo_dia_acoes(sid, acoes, dias=90):
    """Dia da última vez (por item_id) que o robô APLICOU uma das `acoes` (repricer_log)."""
    corte = (date.today() - timedelta(days=dias)).isoformat()
    it, ini = {}, 0
    while True:
        try:
            lote = (rec.sb.table("repricer_log").select("item_id,ts")
                    .eq("seller_id", str(sid)).in_("acao", list(acoes)).eq("aplicado", True)
                    .gte("ts", corte).range(ini, ini + 999).execute().data) or []
        except Exception:
            return it
        for r in lote:
            iid, d = r.get("item_id"), _dia(r.get("ts"))
            if iid and d and d > it.get(iid, ""):
                it[iid] = d
        if len(lote) < 1000:
            break
        ini += 1000
    return it
def ultima_subida_dia(sid, dias=90):
    return ultimo_dia_acoes(sid, ("subir_preco",), dias)
def vendas_pedidos_por_item(sid, dias=120):
    """Por item_id: lista de dias (ISO) dos PEDIDOS distintos (order_id, não cancelados) nos
    últimos `dias`. Usada na regra dos itens sem concorrente (a cada N pedidos, sobe)."""
    corte = (date.today() - timedelta(days=dias)).isoformat()
    tmp, ini = {}, 0   # tmp[iid] = {order_id: dia}
    while True:
        try:
            lote = (rec.sb.table("repricer_vendas").select("item_id,order_id,status,data_aprovacao")
                    .eq("seller_id", str(sid)).gte("data_aprovacao", corte)
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: não li pedidos p/ sem-concorrente: {e})", flush=True); break
        for r in lote:
            if any(x in str(r.get("status") or "").lower() for x in _CANCEL):
                continue
            iid, oid, d = r.get("item_id"), r.get("order_id"), _dia(r.get("data_aprovacao"))
            if iid and d:
                tmp.setdefault(iid, {})[oid or d] = d
        if len(lote) < 1000:
            break
        ini += 1000
    return {iid: sorted(od.values()) for iid, od in tmp.items()}
def estado_promo(item_id, access):
    """(tem_pd, tem_outra): tem PRICE_DISCOUNT NOSSO ativo? tem OUTRA promoção ativa
    (DEAL ou campanha cofinanciada do ML)? Só mexemos no nosso PRICE_DISCOUNT."""
    tem_pd = tem_outra = False
    for o in promos_do_item(item_id, access):
        if isinstance(o, dict) and eh_ativa(o):
            if (o.get("type") or "") == "PRICE_DISCOUNT":
                tem_pd = True
            else:
                tem_outra = True
    return tem_pd, tem_outra
def criar_desconto(item_id, deal_price, access):
    start, finish = _datas()
    body = {"deal_price": round(float(deal_price), 2), "start_date": start,
            "finish_date": finish, "promotion_type": "PRICE_DISCOUNT"}
    return req("POST", f"/seller-promotions/items/{item_id}?app_version=v2", access, body=body)
def remover_desconto(item_id, access):
    return req("DELETE",
               f"/seller-promotions/items/{item_id}?promotion_type=PRICE_DISCOUNT&app_version=v2", access)
def entrar_campanha(item_id, o, access):
    """Entra numa campanha cofinanciada do ML (candidata). Corpo: tipo + promotion_id + offer_id."""
    body = {"promotion_type": o.get("type"), "promotion_id": o.get("id")}
    if o.get("ref_id"):
        body["offer_id"] = o["ref_id"]
    st, resp = req("POST", f"/seller-promotions/items/{item_id}?app_version=v2", access, body=body)
    if st == 400 and "START_DATE" in str(resp):        # algumas pedem a janela de datas
        for k in ("start_date", "finish_date"):
            if o.get(k):
                body[k] = o[k]
        st, resp = req("POST", f"/seller-promotions/items/{item_id}?app_version=v2", access, body=body)
    return st, resp
def mudar_preco_lista(item_id, preco, access):
    """Muda o PREÇO DE TABELA (cheio) do anúncio. Só usado nos itens SEM concorrente, e só
    depois de você APROVAR pelo botão do Telegram. Nunca é chamado automático sem aprovação."""
    return req("PUT", f"/items/{item_id}", access, body={"price": round(float(preco), 2)})
# ============ APROVAÇÃO POR BOTÃO NO TELEGRAM (subida de preço de lista) ============
def telegram_botao(msg, aprov_id):
    """Manda a mensagem com os botões ✅/❌ e devolve o message_id (pra Edge Function editar)."""
    tok = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return None
    teclado = {"inline_keyboard": [[
        {"text": "✅ Aprova", "callback_data": f"apv:{aprov_id}:ok"},
        {"text": "❌ Não",    "callback_data": f"apv:{aprov_id}:no"},
    ]]}
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                                "reply_markup": teclado}, timeout=15)
        d = r.json()
        return (d.get("result") or {}).get("message_id")
    except Exception:
        return None
def aprovacao_pendente(sid, item_id):
    """Já existe um pedido de aprovação em aberto (pendente/aprovada não aplicada) pra esse item?"""
    try:
        d = (rec.sb.table("repricer_aprovacoes").select("id")
             .eq("seller_id", str(sid)).eq("item_id", item_id)
             .in_("status", ["pendente", "aprovada"]).limit(1).execute().data) or []
        return bool(d)
    except Exception:
        return True   # na dúvida, não duplica
def criar_aprovacao(sid, a, pv, novo):
    """Cria a linha 'pendente' e manda o botão no Telegram. Não mexe em preço nenhum."""
    iid = a.get("item_id")
    linha = {"seller_id": str(sid), "item_id": iid, "sku": a.get("sku"),
             "titulo": a.get("titulo"), "preco_atual": round(float(pv), 2),
             "preco_novo": round(float(novo), 2), "status": "pendente"}
    try:
        ins = rec.sb.table("repricer_aprovacoes").insert(linha).execute().data
        aprov_id = ins[0]["id"] if ins else None
    except Exception as e:
        print(f"   (não criei aprovação {iid}: {e})", flush=True); return
    if not aprov_id:
        return
    tit = str(a.get("titulo"))[:60]
    msg = (f"📈 <b>Subir preço de lista</b>\n<b>{tit}</b>\n"
           f"Sem concorrente e vendendo — de R$ {_rs(pv)} para <b>R$ {_rs(novo)}</b> (+{SEMC_PCT:.0f}%).\n"
           f"Aprova a subida?")
    mid = telegram_botao(msg, aprov_id)
    if mid:
        try:
            chat = os.environ.get("TELEGRAM_CHAT_ID")
            rec.sb.table("repricer_aprovacoes").update({"message_id": str(mid), "chat_id": str(chat)}).eq("id", aprov_id).execute()
        except Exception:
            pass
def aplicar_aprovacoes(sid, access):
    """No começo da rodada: aplica as subidas de lista que você APROVOU (status='aprovada')."""
    try:
        pend = (rec.sb.table("repricer_aprovacoes").select("*")
                .eq("seller_id", str(sid)).eq("status", "aprovada").limit(50).execute().data) or []
    except Exception:
        return 0
    n = 0
    for row in pend:
        iid, novo = row.get("item_id"), row.get("preco_novo")
        if not iid or not novo:
            continue
        st, resp = mudar_preco_lista(iid, novo, access)
        ok = 200 <= st < 300
        print(f"{'✅' if ok else '⛔'} SOBE LISTA {iid} -> R${float(novo):.2f} HTTP {st}", flush=True)
        try:
            rec.sb.table("repricer_aprovacoes").update({
                "status": "aplicada" if ok else "erro", "http_status": st,
                "aplicado_em": _agora_iso()}).eq("id", row["id"]).execute()
        except Exception:
            pass
        if ok:
            n += 1
            logar({"seller_id": str(sid), "item_id": iid, "sku": row.get("sku"),
                   "titulo": row.get("titulo"), "acao": "subir_lista", "aplicado": True,
                   "modo": "live", "deal_price": novo, "http_status": st,
                   "motivo": f"lista aprovada: R${float(row.get('preco_atual') or 0):.2f}->R${float(novo):.2f}"})
        time.sleep(0.4)
    return n
def _ritmo_caiu(datas, us, hoje):
    """True se o ritmo de pedidos DEPOIS da subida caiu abaixo de SEMC_CAIU_PCT% do de ANTES.
    Só julga com >=3 dias desde a subida (senão é cedo demais e vira falso positivo)."""
    if not us:
        return False
    try:
        d_us = date.fromisoformat(us)
    except ValueError:
        return False
    dias_dep = (hoje - d_us).days
    if dias_dep < 3:
        return False
    ini_antes = (d_us - timedelta(days=30)).isoformat()
    n_antes = sum(1 for d in datas if ini_antes <= d < us)
    n_dep = sum(1 for d in datas if d > us)
    rate_antes = n_antes / 30.0
    rate_dep = n_dep / max(dias_dep, 1)
    if rate_antes <= 0:
        return False
    return rate_dep < rate_antes * (SEMC_CAIU_PCT / 100.0)
def passo_semc(a, sid, access, pedidos_map, sub_dia, passo_dia, cont):
    """Itens SEM concorrente (tipo confirmado no mapa): a cada SEMC_PEDIDOS pedidos desde a
    última subida, +SEMC_PCT%. Sobe por desconto se couber abaixo do cheio; se passaria do
    cheio, PEDE APROVAÇÃO no Telegram (botão). Passo atrás (-SEMC_PCT%) se parou ou caiu o ritmo."""
    sku = a.get("sku"); iid = a.get("item_id"); tit = str(a.get("titulo"))[:26]
    mrec = sonda.MATCH.get(sku) if sku else None
    if not mrec or mrec.get("tipo") != "sem_concorrente":
        return                                        # só nos confirmados sem concorrente
    pv = a.get("preco_venda"); p0 = a.get("preco_cheio")
    if not pv or not p0 or pv <= 0:
        return
    datas = pedidos_map.get(iid, [])                  # dias dos pedidos (um por pedido)
    us = sub_dia.get(iid, "")                          # dia da última subida
    up = passo_dia.get(iid, "")                        # dia do último passo atrás
    hoje = date.today()
    ult_venda = datas[-1] if datas else ""
    # ANTI-LOOP: conta pedidos desde o ÚLTIMO MOVIMENTO (subida OU passo atrás), não só da subida.
    # Assim, depois de descer, precisa de N vendas NOVAS pra subir de novo — não 1 venda solta.
    ref_mov = max(us or "", up or "")
    pedidos_desde = sum(1 for d in datas if (not ref_mov) or d > ref_mov)
    # cooldown pós passo-atrás: se o último movimento foi um passo atrás e faz pouco tempo, não sobe
    em_cooldown = bool(up and up >= (us or "") and (hoje - date.fromisoformat(up)).days < SEMC_COOLDOWN_DIAS)
    # ---- SUBIR: a cada SEMC_PEDIDOS pedidos desde o último movimento (fora do cooldown) ----
    if pedidos_desde >= SEMC_PEDIDOS and not em_cooldown:
        novo = round(pv * (1 + SEMC_PCT / 100.0), 2)
        if novo <= pv + 0.01:
            return
        if novo <= p0 + 0.01:                          # cabe abaixo do cheio -> sobe por desconto (auto)
            novo = min(novo, round(p0 - 0.01, 2))
            base = {**base_log(sid, a), "acao": "subir_semc", "deal_price": novo,
                    "motivo": f"sem conc: {pedidos_desde} pedidos -> R${pv:.2f}->R${novo:.2f} (+{SEMC_PCT:.0f}%)"}
            if not CONFIRMA:
                print(f"• SIMULA sobe(semc) {iid} {tit}: R${pv:.2f}->R${novo:.2f}", flush=True)
                logar({**base, "aplicado": False, "modo": "simulacao"}); cont["semc"] = cont.get("semc", 0) + 1; return
            tem_pd, tem_outra = promo_estado(a)
            if tem_outra:
                logar({**base, "acao": "pulado_campanha", "aplicado": False, "modo": "live"}); return
            if tem_pd:
                remover_desconto(iid, access); time.sleep(0.3)
            st, resp = criar_desconto(iid, novo, access)
            ok = 200 <= st < 300
            print(f"{'✅' if ok else '⛔'} SOBE(semc) {iid} {tit}: R${pv:.2f}->R${novo:.2f} HTTP {st}", flush=True)
            logar({**base, "aplicado": ok, "modo": "live", "http_status": st})
            if ok:
                cont["semc"] = cont.get("semc", 0) + 1
            return
        # passaria do cheio -> PEDE APROVAÇÃO (não mexe sozinho no preço de lista)
        if aprovacao_pendente(sid, iid):
            return                                     # já tem botão em aberto pra esse item
        print(f"📈 PEDE APROVAÇÃO {iid} {tit}: subir lista R${pv:.2f}->R${novo:.2f}", flush=True)
        logar({**base_log(sid, a), "acao": "pede_aprovacao", "aplicado": False, "modo": "insight",
               "deal_price": novo, "motivo": f"sem conc: {pedidos_desde} pedidos, subir lista"})
        if CONFIRMA:
            criar_aprovacao(sid, a, pv, novo)
            cont["pede"] = cont.get("pede", 0) + 1
        return
    # ---- PASSO ATRÁS: parou OU caiu (só se subiu depois do último passo atrás) ----
    if us and (not up or us > up):
        try:
            dias_sem_venda = (hoje - date.fromisoformat(ult_venda)).days if ult_venda else 999
        except ValueError:
            dias_sem_venda = 999
        parou = dias_sem_venda >= SEMC_PAROU_DIAS
        caiu = _ritmo_caiu(datas, us, hoje)
        if parou or caiu:
            novo = round(pv * (1 - SEMC_PCT / 100.0), 2)
            if novo >= pv - 0.01 or novo <= 0:
                return
            motivo = "parou" if parou else "caiu o ritmo"
            base = {**base_log(sid, a), "acao": "passo_atras", "deal_price": novo,
                    "motivo": f"sem conc ({motivo}): R${pv:.2f}->R${novo:.2f}"}
            if not CONFIRMA:
                print(f"• SIMULA passo-atrás {iid} {tit} ({motivo})", flush=True)
                logar({**base, "aplicado": False, "modo": "simulacao"}); cont["passo"] = cont.get("passo", 0) + 1; return
            tem_pd, tem_outra = promo_estado(a)
            if tem_outra:
                return
            if tem_pd:
                remover_desconto(iid, access); time.sleep(0.3)
            st, resp = criar_desconto(iid, novo, access)
            ok = 200 <= st < 300
            print(f"{'✅' if ok else '⛔'} PASSO-ATRÁS {iid} {tit} ({motivo}): R${pv:.2f}->R${novo:.2f} HTTP {st}", flush=True)
            logar({**base, "aplicado": ok, "modo": "live", "http_status": st})
            if ok:
                cont["passo"] = cont.get("passo", 0) + 1
            return
def _sku_do(b):
    """SKU de um anúncio, usando o extrator NOVO do repricer_sugestoes (que olha
    também o atributo SELLER_SKU e as variações).

    O getattr existe por causa da ORDEM DE SUBIDA: se este arquivo subir antes
    do repricer_sugestoes novo, ele cai no comportamento antigo em vez de
    quebrar com AttributeError no meio da rodada. Depois que os dois estiverem
    no ar, o getattr sempre acha a função nova.
    """
    fn = getattr(rec, "sku_do_item", None)
    if fn:
        return fn(b)
    return b.get("seller_sku") or b.get("seller_custom_field")
def _detalhe_itens(ids, access,
                   attrs=("id,sub_status,seller_sku,seller_custom_field,"
                          "attributes,variations,title")):
    """Detalhe de vários itens (lotes de 20 — limite do multiget do ML).

    'attributes' e 'variations' entraram na lista porque é ONDE O SKU MORA nos
    anúncios feitos pelo formulário atual do ML. Sem eles o multiget devolve
    seller_custom_field=None e o anúncio chega aqui como se não tivesse SKU.
    """
    out = {}
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        st, d = rec.get(f"/items?ids={','.join(chunk)}&attributes={attrs}", access)
        if isinstance(d, list):
            for r in d:
                b = r.get("body") if isinstance(r, dict) else None
                if isinstance(b, dict) and b.get("id"):
                    out[b["id"]] = b
    return out
def snapshot_pausados(sid, access):
    """Atualiza repricer_status_ml com os anúncios PAUSADOS DE VERDADE no ML (fila 'Pausados').
    Igual ao Pricebot: EXCLUI os 'out_of_stock' (sem estoque), que são a maioria e não são pausa real.
    Snapshot por conta: apaga os antigos desta conta e regrava os atuais. Só leitura no ML."""
    try:
        ids = rec.pausados_ids(sid, access)
    except Exception as e:
        print(f"   (pausados: falha ao listar no ML: {e})", flush=True)
        return 0
    det = {}
    try:
        det = _detalhe_itens(ids, access)   # pra saber o sub_status (out_of_stock) e o SKU
    except Exception as e:
        print(f"   (pausados: detalhe falhou — gravando sem filtrar sem-estoque: {e})", flush=True)
    reais = []
    for iid in ids:
        b = det.get(iid, {})
        if det and "out_of_stock" in (b.get("sub_status") or []):
            continue                        # sem estoque NÃO entra na fila Pausados
        reais.append((iid, b))
    try:
        rec.sb.table("repricer_status_ml").delete().eq("seller_id", str(sid)).execute()
        for i in range(0, len(reais), 300):
            lote = [{"seller_id": str(sid), "item_id": iid,
                     "sku": _sku_do(b),
                     "titulo": b.get("title")} for iid, b in reais[i:i + 300]]
            if lote:
                rec.sb.table("repricer_status_ml").insert(lote).execute()
        print(f"   ({len(reais)} pausado(s) de verdade / {len(ids)} paused no ML → fila Pausados)", flush=True)
        return len(reais)
    except Exception as e:
        print(f"   (pausados: tabela repricer_status_ml ausente/erro — rode o SQL: {e})", flush=True)
        return 0
def gravar_sugestoes(sid, analises):
    """Grava, na repricer_sku_sugerido, o SKU que a FICHA TÉCNICA do anúncio
    sugere — para os anúncios que não têm SKU nenhum.

    Por que aqui e não no painel: quem lê a ficha técnica do Mercado Livre é
    este robô, que tem o token. O painel fala só com o Supabase. Então o robô
    propõe e o painel mostra pra você aprovar.

    RASCUNHO, NÃO VERDADE. Nada aqui vira custo sozinho: o vínculo aprovado
    continua indo pra repricer_custo_item, e quem aprova é você na tela.

    GRAVA MESMO EM SIMULAÇÃO (CONFIRMA=NAO). Isto não toca no Mercado Livre;
    é o insumo da tela. As contas de cliente rodam sempre em simulação - se
    eu respeitasse o CONFIRMA aqui, a tela nasceria vazia justamente para
    quem mais precisa dela.

    SNAPSHOT por conta: apaga as desta conta e regrava. Assim sugestão de
    anúncio que ganhou SKU (ou que saiu do ar) desaparece sozinha, sem
    ninguém precisar limpar. Se o insert falhar no meio, a próxima rodada
    conserta - é rascunho, e rascunho se refaz.
    """
    linhas, sem_candidato = [], 0
    for a in analises:
        if (a.get("sku") or "").strip():
            continue                       # já tem SKU: não é caso desta tela
        s = (a.get("sku_sugerido") or "").strip()
        if not s:
            sem_candidato += 1
            continue
        linhas.append({"item_id": a.get("item_id"), "seller_id": str(sid),
                       "titulo": a.get("titulo"), "sku_sugerido": s,
                       "origem": a.get("sku_origem"), "atualizado_em": _agora_iso()})
    try:
        rec.sb.table("repricer_sku_sugerido").delete().eq("seller_id", str(sid)).execute()
        for i in range(0, len(linhas), 300):
            rec.sb.table("repricer_sku_sugerido").insert(linhas[i:i + 300]).execute()
    except Exception as e:
        # tabela ainda não criada (PROMO_B1) ou erro de rede: avisa e segue.
        # Isto NÃO pode derrubar a rodada - o trabalho principal do piloto é
        # outro, e uma sugestão que falta não estraga nada do que já funciona.
        print(f"   (sugestoes de SKU: nao gravei — {e})", flush=True)
        return 0
    por_origem = {}
    for l in linhas:
        o = (l.get("origem") or "?").split(":")[0]
        por_origem[o] = por_origem.get(o, 0) + 1
    if linhas or sem_candidato:
        print(f"   ({len(linhas)} sugestao(oes) de SKU gravada(s)"
              + (f" · {', '.join(f'{k}={v}' for k, v in sorted(por_origem.items()))}" if por_origem else "")
              + (f" · {sem_candidato} anuncio(s) sem candidato nenhum" if sem_candidato else "")
              + ")", flush=True)
    return len(linhas)
def main():
    if not ATIVO:
        print("⛔ ATIVO=NAO (botão de pânico) — nada será escrito. Saindo.", flush=True)
        telegram("⛔ Piloto: botão de pânico ligado. Nada foi feito.")
        return
    faltando = [fn for fn in ("carregar_match", "carregar_controle", "carregar_config",
                              "analisar", "sale_price", "UNDERCUT") if not hasattr(sonda, fn)]
    if faltando:
        print(f"⛔ repricer_competitivo.py DESATUALIZADO no repo (falta: {', '.join(faltando)}).", flush=True)
        print("   Suba a versão mais nova do repricer_competitivo.py JUNTO com este piloto.", flush=True)
        return
    # PASSA A CONTA. Sem o argumento, o preload assume CASA e carrega os custos
    # da Ponto Musical mesmo quando este robô roda numa conta de CLIENTE - que
    # é justamente o que o workflow 'repricer_clientes' faz. Um SKU do cliente
    # igual a um seu receberia o SEU custo. Em simulação seria um número torto
    # no painel; com CONFIRMA=SIM seria preço errado no anúncio de outra
    # empresa. O preload já sabe escolher a tabela certa - só precisava saber
    # de quem é a conta.
    rec.preload(SELLER_ID)
    sonda.carregar_match()
    sonda.carregar_controle()
    sonda.carregar_config()
    resolver_config()          # aplica as regras do painel (repricer_config)
    access = sid = None
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if str(s) == str(SELLER_ID):
            access, sid = a, s
            break
    if not access:
        print(f"não autentiquei a conta {SELLER_ID}.", flush=True)
        return
    modo = "AO VIVO" if CONFIRMA else "SIMULAÇÃO"
    teto_txt = "sem teto" if MAX_ALTERACOES <= 0 else f"teto {MAX_ALTERACOES}/rodada"
    uc_txt = sonda.CONFIG.get("undercut") or sonda.UNDERCUT
    print(f"===== PILOTO | conta {sid} | {modo} | {teto_txt} | anti-salto {MAX_DROP_PCT:.0f}% | "
          f"gate vendas {VENDAS_MIN}u/{VENDAS_DIAS}d | UNDERCUT R${float(uc_txt):.0f} =====", flush=True)
    if CONFIRMA and SEMC_ATIVO:                 # aplica as subidas de lista que você aprovou no botão
        n_apr = aplicar_aprovacoes(sid, access)
        if n_apr:
            print(f"   ({n_apr} subida(s) de lista aprovada(s) aplicada(s))", flush=True)
    todos, _ = rec.todos_ativos(sid, access)
    if MAX_ITENS:
        todos = todos[:MAX_ITENS]
    snapshot_pausados(sid, access)   # fila 'Pausados' do painel (status PAUSED no ML)
    por_item, por_sku = carregar_vendas(sid, VENDAS_DIAS)
    venda_dia = ultima_venda_dia(sid) if SUBIR_ATIVO and SUBIR_EXIGE_VENDA else {}
    subida_dia = ultima_subida_dia(sid) if SUBIR_ATIVO and SUBIR_EXIGE_VENDA else {}
    corte_recente = (date.today() - timedelta(days=VENDAS_DIAS)).isoformat()
    pedidos_map = vendas_pedidos_por_item(sid) if SEMC_ATIVO else {}                       # sem-concorrente
    sub_dia_semc = ultimo_dia_acoes(sid, ("subir_semc", "subir_lista")) if SEMC_ATIVO else {}
    passo_dia = ultimo_dia_acoes(sid, ("passo_atras",)) if SEMC_ATIVO else {}
    def _an(iid):
        try:
            a = sonda.analisar(iid, access, sid)
            if a and a.get("acao") in ACOES_DESCONTO:      # só nos que a gente descontaria
                a["cofin"] = melhor_cofin(a, access)        # melhor campanha do ML por margem
            return a
        except Exception as e:
            print(f"   (analisar {iid} falhou: {e})", flush=True)
            return None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:   # análise em paralelo (grande ganho)
        analises = [a for a in ex.map(_an, todos) if a]
    gravar_sugestoes(sid, analises)   # insumo da tela de vínculo (não toca no ML)
    cont = {"criado": 0, "removido": 0, "vende": 0, "barato": 0, "fila": 0, "campanha": 0,
            "subiu": 0, "semc": 0, "passo": 0, "pede": 0}
    criados = 0
    try:
        uc = float(sonda.CONFIG.get("undercut") or sonda.UNDERCUT or 5)
    except (TypeError, ValueError):
        uc = 5.0
    for a in analises:
        acao = a.get("acao")
        # desligado (por anúncio) ou conta somente-leitura: MOSTRA tudo no painel, mas NÃO aplica
        if a.get("desligado"):        # desligado (por anúncio ou conta): MOSTRA tudo, NÃO aplica
            logar({**base_log(sid, a), "acao": acao or "desligado", "aplicado": False, "modo": "insight"})
            continue
        # MOSTRA TODOS OS ATIVOS no painel: os anúncios sem ação de preço (sem concorrência,
        # sem catálogo, sem custo, sem estoque) entram como "insight" — o robô NÃO os precifica,
        # mas passam a aparecer no Catálogo (com "—" onde não houver dado). Os sem_match ainda
        # rodam a regra de demanda (passo_semc) para os confirmados "sem concorrente".
        if acao in ("sem_match", "sem_custo", "sem_dado", "sem_estoque", "sem_concorrencia"):
            logar({**base_log(sid, a), "acao": acao, "aplicado": False, "modo": "insight"})
            if acao == "sem_match" and SEMC_ATIVO:
                passo_semc(a, sid, access, pedidos_map, sub_dia_semc, passo_dia, cont)
            continue
        iid = a.get("item_id")
        tit = str(a.get("titulo"))[:26]
        u = unidades(a, por_item, por_sku)
        quer_desconto = acao in ACOES_DESCONTO
        remover_motivo = None
        # "perde no cheio": no preço CHEIO a gente perderia o buy box — concorrente (próprio)
        # ou price_to_win (catálogo) <= preço cheio. Aqui a venda vem DO desconto, então o gate
        # de vendas NÃO deve tirar o desconto (senão sobe o preço, perde a caixa e mata a venda).
        _p0 = a.get("preco_cheio")
        _conc = a.get("conc_min") or a.get("segundo") or a.get("price_to_win")
        perde_no_cheio = bool(_p0 and _conc and _conc <= _p0 + 0.01)
        if quer_desconto and u >= VENDAS_MIN and not perde_no_cheio:   # gate de vendas: gira -> não desconta (só se competitivo no cheio)
            quer_desconto = False
            remover_motivo = f"vende {u}u/{VENDAS_DIAS}d"
            cont["vende"] += 1
            print(f"🛒 VENDE {iid} {tit} ({u}u/{VENDAS_DIAS}d) -> não desconta", flush=True)
            logar({**base_log(sid, a), "acao": "vende_nao_mexe", "aplicado": False, "modo": "insight"})
        elif quer_desconto and u >= VENDAS_MIN and perde_no_cheio:     # vende, mas só por causa do desconto -> mantém
            print(f"🛒🔒 VENDE {iid} {tit} ({u}u/{VENDAS_DIAS}d) mas perde no cheio -> MANTÉM desconto", flush=True)
            logar({**base_log(sid, a), "acao": "vende_mantem_desconto", "aplicado": False, "modo": "insight",
                   "motivo": f"vende {u}u/{VENDAS_DIAS}d; perde no cheio"})
        elif acao in REMOVER_OK:
            remover_motivo = acao
            if acao in ("subir_margem", "ja_competitivo"):
                cont["barato"] += 1
                print(f"💰 BARATO DEMAIS {iid} {tit} -> {a.get('detalhe')}", flush=True)
                logar({**base_log(sid, a), "acao": acao, "aplicado": False, "modo": "insight",
                       "deal_price": a.get("alvo_subir")})
                # ===== SUBIDA GRADUAL DE MARGEM =====
                # Recupera preço nos "barato demais": REDUZ o desconto (não remove tudo), subindo
                # o preço em passos até logo abaixo do concorrente. Regras:
                #  - alvo = 2º lugar/concorrente - undercut (continua ganhando a caixa)
                #  - nunca acima do preço cheio (não mexe na tabela, é sempre camada de desconto)
                #  - no máximo SUBIR_PASSO_PCT% por rodada (gradual); só se o ganho >= SUBIR_MIN_RS
                if SUBIR_ATIVO:
                    # GATE DA VENDA: só sobe o próximo degrau se VENDEU no preço atual.
                    # Já subiu antes -> exige venda em dia POSTERIOR à última subida.
                    # Nunca subiu -> exige venda recente (últimos VENDAS_DIAS). Subiu e não vendeu = segura.
                    if SUBIR_EXIGE_VENDA:
                        uv = venda_dia.get(iid, ""); us = subida_dia.get(iid, "")
                        pode_subir = (uv > us) if us else (uv >= corte_recente)
                        if not pode_subir:
                            print(f"⏸️  SUBIR-ESPERA {iid} {tit}: sem venda nova desde a última subida "
                                  f"(venda {uv or '—'} / subida {us or '—'}) -> segura", flush=True)
                            logar({**base_log(sid, a), "acao": "subir_espera", "aplicado": False, "modo": "insight",
                                   "motivo": f"aguarda venda (última venda {uv or '—'}, última subida {us or '—'})"})
                            remover_motivo = None; continue   # segura no preço atual (não sobe, não remove)
                    tem_pd, tem_outra = promo_estado(a)   # do sale_price já lido (sem chamada extra)
                    pv = a.get("preco_venda"); p0 = a.get("preco_cheio")
                    if acao == "subir_margem":
                        topo = a.get("alvo_subir")                    # já vem (2º lugar - undercut)
                    else:                                             # ja_competitivo
                        cc = a.get("conc_min")
                        topo = round(cc - uc, 2) if cc else None      # logo abaixo do concorrente
                    if (not tem_outra) and topo and pv and p0 and topo > pv + SUBIR_MIN_RS and topo < p0:
                        teto = pv * (1 + SUBIR_PASSO_PCT / 100.0)     # passo gradual desta rodada
                        novo = round(min(topo, teto, p0 - 0.01), 2)   # nunca acima do alvo nem do cheio
                        if novo > pv + SUBIR_MIN_RS:
                            descn = (1 - novo / p0) * 100
                            rowu = {**base_log(sid, a), "acao": "subir_preco", "deal_price": novo,
                                    "desconto_pct": round(descn, 1),
                                    "motivo": f"barato demais: sobe R${pv:.2f}->R${novo:.2f} (alvo R${topo:.2f})"}
                            dentro = (MAX_ALTERACOES <= 0) or (criados < MAX_ALTERACOES)
                            if not CONFIRMA:
                                print(f"• SIMULA sobe {iid} {tit}: R${pv:.2f} -> R${novo:.2f} (alvo R${topo:.2f})", flush=True)
                                logar({**rowu, "acao": "subir_preco" if dentro else "fila_teto",
                                       "aplicado": False, "modo": "simulacao"})
                                if dentro:
                                    criados += 1; cont["subiu"] += 1
                                else:
                                    cont["fila"] += 1
                                remover_motivo = None; continue
                            if not dentro:
                                logar({**rowu, "acao": "fila_teto", "aplicado": False, "modo": "live"})
                                cont["fila"] += 1; remover_motivo = None; continue
                            if tem_pd:                                     # troca o desconto atual pelo novo (maior)
                                remover_desconto(iid, access); time.sleep(0.3)
                            st, resp = criar_desconto(iid, novo, access)   # reduz o desconto (preço maior)
                            ok = 200 <= st < 300
                            print(f"{'✅' if ok else '⛔'} SOBE {iid} {tit}: R${pv:.2f} -> R${novo:.2f} HTTP {st}", flush=True)
                            logar({**rowu, "aplicado": ok, "modo": "live", "http_status": st})
                            if ok:
                                criados += 1; cont["subiu"] += 1
                            time.sleep(0.4)
                            remover_motivo = None; continue
        if quer_desconto:
            alvo, p0 = a.get("alvo"), a.get("preco_cheio")
            if not alvo or not p0 or alvo <= 0:
                continue
            desc = (1 - alvo / p0) * 100
            row = {**base_log(sid, a), "acao": acao, "deal_price": alvo, "desconto_pct": round(desc, 1)}
            cofin = a.get("cofin")                        # melhor campanha do ML por margem, ou None
            ma = a["margem_alvo"] if a.get("margem_alvo") is not None else -999
            # REGRA "só se continuar competitivo": campanha entra se paga MAIS margem E preço <= alvo
            usar_campanha = bool(cofin and cofin["pb"] <= alvo + 0.01 and cofin["margem"] > ma + 0.01)
            camp_txt = ""
            if cofin:
                _nome = cofin["o"].get("name") or cofin["o"].get("type")
                _tag = ("USA CAMPANHA (competitiva e paga mais)" if usar_campanha
                        else ("paga mais margem MAS fica acima do alvo -> usa desconto"
                              if cofin["margem"] > ma + 0.01 else "desconto próprio vence"))
                camp_txt = f"  |  🎁 \"{_nome}\" R${cofin['pb']:.2f} margem {cofin['margem']:.1f}% -> {_tag}"
            tem_pd, tem_outra = promo_estado(a)           # do sale_price já lido (sem chamada extra)
            if tem_outra:                                 # já em campanha/DEAL do ML -> deixa quieto
                logar({**row, "acao": "pulado_campanha", "aplicado": False, "modo": modo.lower()}); continue
            if usar_campanha:                             # ===== caminho CAMPANHA =====
                o = cofin["o"]; _nome = o.get("name") or o.get("type")
                rowc = {**base_log(sid, a), "acao": "entrar_campanha", "deal_price": cofin["pb"],
                        "margem_alvo": cofin["margem"], "motivo": f"campanha {_nome}"}
                dentro = (MAX_ALTERACOES <= 0) or (criados < MAX_ALTERACOES)
                cont["campanha"] += 1
                if not CONFIRMA:
                    marca = "" if dentro else f" (FILA, além do teto {MAX_ALTERACOES})"
                    print(f"🎁 USA CAMPANHA{marca} {iid} {tit}: {_nome} R${cofin['pb']:.2f} "
                          f"margem {cofin['margem']:.1f}% (vs desconto {ma:.1f}%)", flush=True)
                    logar({**rowc, "acao": "entrar_campanha" if dentro else "fila_teto", "aplicado": False, "modo": "simulacao"})
                    if dentro:
                        criados += 1; cont["criado"] += 1
                    else:
                        cont["fila"] += 1
                    continue
                if not dentro:
                    logar({**rowc, "acao": "fila_teto", "aplicado": False, "modo": "live"}); cont["fila"] += 1; continue
                if tem_pd:                                # trocar: sai do nosso desconto antes de entrar
                    remover_desconto(iid, access); time.sleep(0.3)
                st, resp = entrar_campanha(iid, o, access)
                ok = 200 <= st < 300
                print(f"{'✅' if ok else '⛔'} CAMPANHA {iid} {tit}: {_nome} R${cofin['pb']:.2f} HTTP {st}", flush=True)
                logar({**rowc, "aplicado": ok, "modo": "live", "http_status": st})
                if ok:
                    criados += 1; cont["criado"] += 1
                time.sleep(0.4)
                continue
            # ===== caminho DESCONTO PRÓPRIO =====
            if tem_pd:
                continue                                  # já descontado por nós; ajuste fino fica pra depois
            if desc > MAX_DROP_PCT:
                print(f"⏭️  SALTO {iid} {tit} -> R${alvo:.2f} ({desc:.1f}% off > {MAX_DROP_PCT:.0f}%, "
                      f"margem {a.get('margem_alvo')}%) — segurado p/ revisão", flush=True)
                logar({**row, "acao": "pulado_salto", "aplicado": False, "modo": modo.lower()}); continue
            if desc < 5:
                logar({**row, "acao": "pulado_menor5", "aplicado": False, "modo": modo.lower()}); continue
            dentro = (MAX_ALTERACOES <= 0) or (criados < MAX_ALTERACOES)   # teto 0 = sem teto
            if not CONFIRMA:                        # simulação: mostra TUDO (marca a fila além do teto)
                marca = "" if dentro else f" (FILA, além do teto {MAX_ALTERACOES})"
                print(f"• SIMULA cria{marca} {iid} {tit} -> R${alvo:.2f} ({desc:.1f}% off, "
                      f"margem {a.get('margem_alvo')}%){camp_txt}", flush=True)
                logar({**row, "acao": acao if dentro else "fila_teto", "aplicado": False, "modo": "simulacao"})
                if dentro:
                    criados += 1; cont["criado"] += 1
                else:
                    cont["fila"] += 1
                continue
            if not dentro:                          # ao vivo: respeita o teto
                logar({**row, "acao": "fila_teto", "aplicado": False, "modo": "live"})
                cont["fila"] += 1; continue
            gp, go = estado_promo(iid, access)      # guarda final: confere no seller-promotions antes de escrever
            if gp or go:
                logar({**row, "acao": "pulado_ja_promo", "aplicado": False, "modo": "live"}); continue
            st, resp = criar_desconto(iid, alvo, access)
            ok = 200 <= st < 300
            print(f"{'✅' if ok else '⛔'} CRIA {iid} {tit} -> R${alvo:.2f} ({desc:.1f}% off) HTTP {st}", flush=True)
            logar({**row, "aplicado": ok, "modo": "live", "http_status": st})
            if ok:
                criados += 1; cont["criado"] += 1
            time.sleep(0.4)
        elif remover_motivo:
            tem_pd, _ = promo_estado(a)               # do sale_price já lido
            if not tem_pd:
                continue
            # ANTI-GANGORRA: só remove se a gente continuaria GANHANDO no preço CHEIO.
            # Se só ganha por causa do desconto (concorrente <= preço cheio), remover faria
            # o preço subir e perder -> mantém o desconto (senão vira cria/remove toda hora).
            p0 = a.get("preco_cheio")
            conc = a.get("conc_min") or a.get("segundo") or a.get("price_to_win")  # catálogo: price_to_win
            if p0 and conc and conc <= p0 + 0.01:
                pv = a.get("preco_venda")
                print(f"🔒 MANTÉM desconto {iid} {tit}: ganha a R${pv} mas no cheio R${p0:.2f} "
                      f"perderia p/ conc R${conc:.2f} (anti-gangorra)", flush=True)
                logar({**base_log(sid, a), "acao": "manter_desconto", "aplicado": False,
                       "modo": modo.lower(), "motivo": f"anti-gangorra ({remover_motivo})"})
                cont["mantido"] = cont.get("mantido", 0) + 1
                continue
            row = {**base_log(sid, a), "acao": "remover_desconto", "motivo": remover_motivo}
            if not CONFIRMA:
                print(f"• SIMULA remove desconto {iid} {tit} ({remover_motivo})", flush=True)
                logar({**row, "aplicado": False, "modo": "simulacao"}); cont["removido"] += 1; continue
            gp, _ = estado_promo(iid, access)         # guarda final: confirma que ainda temos o desconto
            if not gp:
                continue
            st, resp = remover_desconto(iid, access)
            ok = 200 <= st < 300
            print(f"{'✅' if ok else '⛔'} REMOVE desconto {iid} {tit} ({remover_motivo}) HTTP {st}", flush=True)
            logar({**row, "aplicado": ok, "modo": "live", "http_status": st})
            if ok:
                cont["removido"] += 1
            time.sleep(0.3)
    resumo = (f"Piloto {modo} · conta {sid}: {cont['criado']} desconto(s) "
              f"{'criados' if CONFIRMA else 'a criar'}"
              + (f" (+{cont['fila']} na fila além do teto)" if cont['fila'] else "")
              + f", {cont['removido']} remoção(ões), {cont.get('mantido', 0)} desconto(s) mantido(s) "
              f"(anti-gangorra), {cont['vende']} segurados por venda, {cont['barato']} barato-demais"
              + (f", ⬆️ {cont['subiu']} subida(s) de margem" if cont.get('subiu') else "")
              + (f", 📈 {cont['semc']} subida(s) sem-concorrente" if cont.get('semc') else "")
              + (f", 🔽 {cont['passo']} passo(s) atrás" if cont.get('passo') else "")
              + (f", 🔔 {cont['pede']} aprovação(ões) pedida(s)" if cont.get('pede') else "")
              + (f", 🎁 {cont['campanha']} onde campanha do ML paga mais margem" if cont['campanha'] else "")
              + ".")
    print(f"\n=== {resumo} ===", flush=True)
    telegram("🤖 " + resumo)
    if not CONFIRMA:
        print("SIMULAÇÃO: nada escrito. CONFIRMA=SIM (e ATIVO=SIM) pra valer.", flush=True)
if __name__ == "__main__":
    main()
