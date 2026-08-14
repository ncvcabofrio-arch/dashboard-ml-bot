"""
Fase 2 — APLICADOR DE PROMOÇÕES (a partir da fila aprovada no painel).
Fluxo seguro, em duas travas:
  1) Lê repricer_promo_fila com status='aprovada' (você aprovou no painel).
  2) Pra cada item RE-BUSCA as promoções agora (preço/candidato podem ter mudado),
     RE-CALCULA a margem e SÓ aplica se continuar >= piso do grupo.
  3) Entra na promoção via API do ML e grava o resultado de volta na fila.
DRY_RUN=1 (padrão): NÃO posta nada — só mostra o corpo exato que enviaria e a margem
recalculada. Rode assim primeiro. Depois DRY_RUN=0 pra aplicar de verdade.
Escopo desta fase: ACAO='entrar' (item que HOJE não tem promoção ativa). 'trocar'
fica marcado como pendente de fase 3 (envolve sair da atual — sua regra é cautelosa).
NÃO altera nenhuma função compartilhada: só importa repricer_sugestoes como leitura (rec).
"""
import os
import time
import json
import requests
import repricer_sugestoes as rec
from datetime import datetime, timezone, timedelta
from ml_auth import obter_access
API = rec.API
sb = rec.sb
DRY = (os.environ.get("DRY_RUN", "1").strip() != "0")          # padrão: simula
SELLER_FILTRO = (os.environ.get("SELLER_ID") or "").strip()    # vazio = todas as contas
ITEM_FILTRO = (os.environ.get("ITEM_ID") or "").strip()        # 1 anúncio só
ITENS_FILTRO = [x.strip() for x in (os.environ.get("ITEM_IDS") or "").split(",") if x.strip()]  # lista (o painel manda os selecionados)
MAX_APLICAR = int(os.environ.get("MAX_APLICAR", "0"))          # 0 = sem limite
MARGEM_MIN = (os.environ.get("MARGEM_MIN") or "").strip()      # piso dos itens "Padrão" (mesmo da simulação)
# IGNORAR_PISO=1 -> aplica MESMO abaixo do piso do grupo. Só a tela Acelerar manda isso:
# lá quem aperta o botão é humano, olhando anúncio por anúncio, e a tela existe pra
# liquidar (aceitar margem magra de propósito). O piloto e o reator rodam em execuções
# próprias, sem esta variável, e continuam barrando normalmente.
# Sem piso NÃO é sem registro: a margem resultante vai gravada em margem_aplicada e o
# aviso "ABAIXO DO PISO" fica no campo resultado, visível no painel.
IGNORAR_PISO = (os.environ.get("IGNORAR_PISO", "0").strip() == "1")
RETRY_ONLY = (os.environ.get("RETRY_ONLY", "0").strip() == "1")  # 1 = SÓ reprocessa a fila de retry (cron horário)
WORKERS = int(os.environ.get("WORKERS", "1"))                  # >1 = processa itens em paralelo (pool controlado)
PROPAG_SLEEP = float(os.environ.get("PROPAG_SLEEP", "4"))      # respiro entre 1ª e 2ª passada (era 8s fixo)
TIPOS_OK = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "LIGHTNING", "DEAL",
            "DOD", "MARKETPLACE_CAMPAIGN", "VOLUME", "SELLER_CAMPAIGN",
            "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "BANK", "PRICE_DISCOUNT"}
# ===================== FILA DE REPROCESSAMENTO (tabela separada) =====================
# Só entram aqui itens que ERRARAM ou DIVERGIRAM na entrada. Fica numa tabela PRÓPRIA
# (repricer_promo_retry), separada da fila de aprovação, pra o painel mostrar num bloco
# à parte SEM dar refresh a cada sugestão nova.
#
# categoria      -> o que aconteceu e como a fila trata:
#   ok                   resolveu (entrou / já estava ativa) -> sai da fila
#   transitorio          erro recuperável (candidato sumiu, POST falhou) -> retenta de hora em hora
#   aguardando_ml        ML aceitou, ativação assíncrona -> reconfere de hora em hora
#   aguardando_vigencia  promoção/participação ainda não começou -> espera a data (retenta espaçado)
#   terminal             precisa de humano (sem custo, tipo não suportado, relâmpago já ativa,
#                        FAÇA NA MÃO por CREDIBILITY/START_DATE) -> NÃO retenta
#   divergencia          a sugestão MUDOU de promoção -> NÃO troca sozinho, te avisa
#
# RETRY_PLAN[categoria] = (intervalo_horas, teto_de_tentativas)
RETRY_PLAN = {
    "transitorio":         (1, 8),    # ~8 h
    "aguardando_ml":       (1, 6),    # ~6 h confirmando ativação
    "aguardando_vigencia": (6, 12),   # ~3 dias esperando a vigência começar
}
# código de resultado (1ª/2ª passada) -> categoria da fila de retry
CODIGO_CATEGORIA = {
    # resolvido
    "ja_ativa": "ok", "simulado": "ok", "saiu": "ok", "confirmado": "ok",
    # postou / assíncrono
    "aplicado": "aguardando_ml", "ativando": "aguardando_ml", "aguardando_ml": "aguardando_ml",
    # recuperável -> retenta
    "sem_candidato": "transitorio", "sem_oferta": "transitorio",
    "erro_post": "transitorio", "erro_sair": "transitorio",
    # espera a vigência
    "programada": "aguardando_vigencia", "vigencia_futura": "aguardando_vigencia",
    # precisa de humano -> não retenta
    "acao_invalida": "terminal", "tipo_nao_suportado": "terminal", "sem_item": "terminal",
    "sem_custo": "terminal", "sem_avaliar": "terminal", "abaixo_piso": "terminal",
    # desconto individual sem preço NEM margem-alvo: só você pode resolver -> não retenta
    "sem_preco_alvo": "terminal",
    # nem no menor desconto possível a margem-alvo é alcançada -> decisão sua
    "margem_inalcancavel": "terminal",
    "sem_corpo": "terminal", "bloqueada_luz_dod": "terminal",
    # FAÇA NA MÃO: o ML recusou por credibilidade do desconto / data da campanha.
    # Esperar NÃO resolve (precisa de você) -> terminal, NÃO retenta.
    "faca_na_mao": "terminal",
    # a sugestão mudou de promoção
    "divergencia": "divergencia",
}
RETRY_TAB = "repricer_promo_retry"
def _agora():
    return datetime.now(timezone.utc)
def _retry_atual(seller_id, item_id):
    """Lê a linha da fila de retry deste item (ou None)."""
    try:
        r = (sb.table(RETRY_TAB).select("*")
             .eq("seller_id", str(seller_id)).eq("item_id", item_id)
             .limit(1).execute().data) or []
        return r[0] if r else None
    except Exception as e:
        print(f"  aviso: não li retry de {item_id}: {e}", flush=True)
        return None
def registrar_retry(fila, codigo, resultado_txt=None):
    """Depois de processar um item, atualiza a tabela de retry conforme o desfecho.
    NÃO roda em simulação (DRY). Mantém 1 linha por (seller_id,item_id)."""
    if DRY:
        return
    seller_id = str(fila.get("seller_id") or "")
    item_id = fila.get("item_id")
    if not item_id:
        return
    cat = CODIGO_CATEGORIA.get(codigo, "transitorio")
    prev = _retry_atual(seller_id, item_id)
    agora = _agora()
    base = {
        "seller_id": seller_id, "item_id": item_id,
        "sku": fila.get("sku"), "titulo": fila.get("titulo"),
        "acao": fila.get("acao"),
        "promocao_id": fila.get("promocao_id"), "promocao_tipo": fila.get("promocao_tipo"),
        "promocao_nome": fila.get("promocao_nome"), "preco_alvo": fila.get("preco_alvo"),
        "categoria": cat, "ultimo_resultado": (resultado_txt or codigo)[:500],
        "atualizado_em": agora.isoformat(),
    }
    if cat == "ok":
        # resolveu: se havia linha, marca resolvida (some do bloco ativo do painel); senão nem cria.
        if prev:
            base.update({"status": "resolvido", "proxima_tentativa": None})
            _upsert_retry(base)
        return
    if cat in RETRY_PLAN:
        tent = int((prev.get("tentativas") if prev else 0) or 0) + 1
        horas, teto = RETRY_PLAN[cat]
        if tent >= teto:
            base.update({"status": "esgotado", "tentativas": tent, "max_tentativas": teto,
                         "proxima_tentativa": None,
                         "ultimo_resultado": f"esgotou {tent} tentativas — reveja no painel ({base['ultimo_resultado']})"})
        else:
            base.update({"status": "ativo", "tentativas": tent, "max_tentativas": teto,
                         "proxima_tentativa": (agora + timedelta(hours=horas)).isoformat()})
        _upsert_retry(base)
        return
    # terminal / divergencia: fica visível pra revisão manual, sem auto-retry.
    # max_tentativas é NOT NULL na tabela; terminal não retenta, então 0 (não None).
    base.update({"status": "revisar", "proxima_tentativa": None,
                 "tentativas": int((prev.get("tentativas") if prev else 0) or 0),
                 "max_tentativas": int((prev.get("max_tentativas") if prev else 0) or 0)})
    _upsert_retry(base)
def _upsert_retry(row):
    try:
        if "criado_em" not in row:
            row["criado_em"] = _agora().isoformat()
        sb.table(RETRY_TAB).upsert(row, on_conflict="seller_id,item_id").execute()
    except Exception as e:
        print(f"  aviso: não gravei retry de {row.get('item_id')}: {e}", flush=True)
def _sugestoes_rodando():
    """True se o robô de SUGESTÕES está rodando agora (pra não reprocessar em cima de
    sugestão meio-escrita). Best-effort: se não houver o registro, assume que não."""
    try:
        r = (sb.table("repricer_status").select("estado")
             .eq("workflow", "sugestoes").limit(1).execute().data) or []
        return bool(r) and (r[0].get("estado") == "rodando")
    except Exception:
        return False
def carregar_retry_devidos():
    """Lê os itens da fila de retry que já estão na hora (proxima_tentativa <= agora),
    ainda ativos e abaixo do teto. Devolve dicts no MESMO formato da fila de aprovação."""
    if DRY:
        return []
    try:
        q = (sb.table(RETRY_TAB).select("*")
             .eq("status", "ativo")
             .lte("proxima_tentativa", _agora().isoformat()))
        if SELLER_FILTRO:
            q = q.eq("seller_id", SELLER_FILTRO)
        if ITENS_FILTRO:
            q = q.in_("item_id", ITENS_FILTRO)
        elif ITEM_FILTRO:
            q = q.eq("item_id", ITEM_FILTRO)
        rows = (q.execute().data) or []
    except Exception as e:
        print(f"aviso: não li a fila de retry: {e}", flush=True)
        return []
    devidos = []
    for r in rows:
        teto = int(r.get("max_tentativas") or 8)
        if int(r.get("tentativas") or 0) >= teto:
            continue
        # id-sentinela: o gravar() NÃO deve tocar em repricer_promo_fila com o id da tabela de retry.
        r = {**r, "id": f"retry:{r.get('seller_id')}:{r.get('item_id')}", "_retry": True}
        devidos.append(r)   # já tem seller_id/item_id/acao/promocao_id/tentativas -> serve na processar()
    return devidos
def post(path, access, body, tent=3):
    r = None
    for i in range(tent):
        r = requests.post(API + path,
                          headers={"Authorization": "Bearer " + access,
                                   "Content-Type": "application/json"},
                          json=body, timeout=25)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)
def achar_candidato(ofertas, fila):
    """Localiza, na resposta ATUAL do ML, a promoção candidata que foi aprovada.

    MESMA RÉGUA DO SUGERIDOR — foi daqui que saíram os 'sem_avaliar'.
    O repricer_sugestoes monta as candidatas assim (processar_item):
        cand_raw = [... and o.get("original_price") and preco_oferta(o)]
    ou seja, ele DESCARTA candidata sem preço ou sem original_price, porque o
    rec.avaliar() devolve None nesses dois casos e não há margem pra calcular.

    Este aplicador não descartava: filtrava só por status e tipo e, se o id e o
    nome não casassem, aceitava cands[0]. Podia então escolher uma candidata que
    o sugeridor nunca teria olhado -> avaliar() = None -> 'sem_avaliar' sem motivo.

    Agora procuramos primeiro entre as AVALIÁVEIS. Só se não houver nenhuma é que
    caímos nas demais — e aí o diagnóstico do 'sem_avaliar' diz qual campo faltou.
    """
    tipo = (fila.get("promocao_tipo") or "").upper()
    pid = fila.get("promocao_id")
    nome = fila.get("promocao_nome")
    todas = [o for o in ofertas if isinstance(o, dict)
             and (o.get("status") or "").lower() == "candidate"
             and (o.get("type") or "").upper() == tipo]
    avaliaveis = [o for o in todas if o.get("original_price") and rec.preco_oferta(o)]
    for grupo in (avaliaveis, todas):     # 1º as avaliáveis; só depois as demais
        # 1) casa pelo id da promoção; 2) pelo nome; 3) se só sobrou uma do tipo, usa ela
        for o in grupo:
            if pid and o.get("id") == pid:
                return o
        for o in grupo:
            if nome and (o.get("name") or "") == nome:
                return o
        if len(grupo) == 1:
            return grupo[0]
    return None
def _com_datas(corpo, cand):
    """Algumas campanhas exigem start_date/finish_date no POST (erro START_DATE).
    Inclui quando o candidato informa essas datas."""
    ini = cand.get("start_date")
    fim = cand.get("finish_date") or cand.get("end_date")
    if ini:
        corpo["start_date"] = ini
    if fim:
        corpo["finish_date"] = fim
    return corpo
def _clamp_preco(preco, cand):
    """Mantém o deal_price dentro da faixa crível [min,max] do candidato (evita 400 CREDIBILITY)."""
    preco = round(float(preco or 0), 2)
    try:
        mx = cand.get("max_discounted_price")
        mn = cand.get("min_discounted_price")
        if mx is not None:
            preco = min(preco, float(mx))
        if mn is not None:
            preco = max(preco, float(mn))
    except (TypeError, ValueError):
        pass
    return round(preco, 2)
def corpo_post(tipo, cand, preco_alvo):
    """Monta o corpo do POST de ENTRADA conforme o tipo (docs seller-promotions v2 — auditado
    contra TODAS as páginas). Cada família tem um formato próprio."""
    tipo = (tipo or "").upper()
    oid = cand.get("ref_id") or cand.get("offer_id") or cand.get("candidate_id") or cand.get("id")
    # (1) cofinanciadas automatizadas / preços competitivos: id+type+offer_id
    #     (mantém as datas p/ SMART/PM como já vinha funcionando)
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL"):
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": tipo, "offer_id": oid}, cand)
    # (2) Pix (BANK) e pré-acordadas: id+type+offer_id — SEM datas (doc)
    if tipo in ("BANK", "PRE_NEGOTIATED", "UNHEALTHY_STOCK"):
        return {"promotion_id": cand.get("id"), "promotion_type": tipo, "offer_id": oid}
    # (3) cofinanciada tradicional / desconto por quantidade: SÓ id+type
    #     (o ML define o preço; sem offer_id, sem deal_price)
    if tipo in ("MARKETPLACE_CAMPAIGN", "VOLUME"):
        return {"promotion_id": cand.get("id"), "promotion_type": tipo}
    # (4) relâmpago: deal_price + stock
    if tipo == "LIGHTNING":
        st = cand.get("stock") or {}
        estoque = st.get("min") or st.get("remaining_stock") or 1
        return {"deal_price": _clamp_preco(preco_alvo, cand), "stock": int(estoque), "promotion_type": "LIGHTNING"}
    # (5) oferta do dia: deal_price (SEM stock, diferente da relâmpago)
    if tipo == "DOD":
        return {"deal_price": _clamp_preco(preco_alvo, cand), "promotion_type": "DOD"}
    # (6) tradicional e do vendedor: deal_price + id (vendedor define o preço, dentro da faixa crível)
    if tipo in ("DEAL", "SELLER_CAMPAIGN"):
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": tipo,
                           "deal_price": _clamp_preco(preco_alvo, cand)}, cand)
    # (7) desconto individual do vendedor: deal_price + datas próprias (formato local, máx 14 dias)
    if tipo == "PRICE_DISCOUNT":
        # A doc diz "prazo máximo de 14 dias" e que as datas ignoram o horário: começa
        # 00:00 do dia inicial e termina 23:59 do final. Logo hoje+14 seriam QUINZE dias
        # contados, um a mais que o permitido. +13 fecha exatamente em 14 dias.
        agora = datetime.now(timezone.utc) - timedelta(hours=3)   # BRT
        ini = agora.strftime("%Y-%m-%d")
        fim = (agora + timedelta(days=13)).strftime("%Y-%m-%d")
        return {"deal_price": _clamp_preco(preco_alvo, cand), "promotion_type": "PRICE_DISCOUNT",
                "start_date": ini + "T00:00:00", "finish_date": fim + "T23:59:59"}
    return None
# ===================== PROVA REAL POR PREÇO (sale_price) =====================
# A promoção NÃO altera item.price; o que revela o que o cliente PAGA de fato é o
# /items/{id}/sale_price. Por isso ele é a prova de que a promoção está no ar — e o
# critério que decide se o item SAI da fila (confirmado) ou VOLTA pra tentar (retry).
def preco_venda_real(iid, access):
    """(amount, promotion_type) que o cliente paga AGORA, direto do ML. (None,None) se indisponível."""
    try:
        st, d = rec.get(f"/items/{iid}/sale_price?context=channel_marketplace", access)
        if not isinstance(d, dict):
            return None, None
        amt = d.get("amount")
        ptipo = (d.get("metadata") or {}).get("promotion_type")
        return (float(amt) if amt is not None else None), ptipo
    except Exception:
        return None, None
def confirmar_entrada(iid, pid, tipo, access):
    """PROVA DE ERRO do entrar: confere DIRETO na promoção alvo se o anúncio ficou ATIVO
    (status started). Retorna True (confirmado), False (não confirmou) ou None (sem como
    conferir — ex.: relâmpago sem promotion_id)."""
    tipo = (tipo or "").upper()
    if not pid or not tipo:
        return None
    st, d = rec.get(f"/seller-promotions/promotions/{pid}/items"
                    f"?promotion_type={tipo}&item_id={iid}&app_version=v2", access)
    res = (d.get("results") if isinstance(d, dict) else None) or []
    for it in res:
        if str(it.get("id")) == str(iid) and (it.get("status") or "").lower() == "started":
            return True
    return False
def detalhe_relampago(iid, promotion_id, access):
    """Preço SUGERIDO e faixa crível da relâmpago só vêm na consulta POR PROMOÇÃO.
    IMPORTANTE: sem filtro de status o ML devolve só os ATIVOS — como o item é CANDIDATO,
    precisa de status=candidate. Retorna {price(sugerido), min/max_discounted_price, stock, datas}."""
    if not promotion_id:
        return None
    base = (f"/seller-promotions/promotions/{promotion_id}/items"
            f"?promotion_type=LIGHTNING&item_id={iid}&app_version=v2")
    for extra in ("&status=candidate", "&status=pending", "", "&status=started"):
        st, d = rec.get(base + extra, access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        for r in res:
            if r.get("id") == iid:
                return r
        if res:
            return res[0]
    return None
_RETRY_PATCH = {}   # guarda o último patch de itens vindos da fila de retry (id "retry:...")
def gravar(fila_id, patch):
    patch["aplicado_em"] = datetime.now(timezone.utc).isoformat()
    # Itens que vieram da FILA DE RETRY têm id "retry:<seller>:<item>" (não são linha de
    # repricer_promo_fila). NÃO escrevo em repricer_promo_fila (id colidiria com outro item):
    # só guardo o texto pro registrar_retry usar; a tabela de retry é a fonte da verdade deles.
    if isinstance(fila_id, str) and fila_id.startswith("retry:"):
        _RETRY_PATCH[fila_id] = patch
        return
    try:
        sb.table("repricer_promo_fila").update(patch).eq("id", fila_id).execute()
    except Exception as e:
        print("  !! falha ao gravar resultado:", e, flush=True)
def _gravar_melhor(fila_id, margem, preco):
    """Guarda a MELHOR margem possível (e o preço dela) em colunas próprias, pro painel
    conseguir LISTAR em massa os anúncios que não couberam na margem pedida — em vez de
    você abrir trinta textos de 'resultado' um por um.

    É um UPDATE SEPARADO e best-effort de propósito: o 'resultado' já foi gravado antes
    desta chamada, então se as colunas ainda não existirem no banco (fase_melhor_margem.sql
    não rodado) só esta gravação falha, e a explicação principal continua salva."""
    if isinstance(fila_id, str) and str(fila_id).startswith("retry:"):
        return
    try:
        sb.table("repricer_promo_fila").update(
            {"melhor_margem_possivel": round(float(margem), 2),
             "melhor_preco_possivel": round(float(preco), 2)}).eq("id", fila_id).execute()
    except Exception as e:
        print(f"  aviso: não gravei a melhor margem de {fila_id} "
              f"(rode fase_melhor_margem.sql no Supabase): {e}", flush=True)
def req_delete(path, access, tent=3):
    r = None
    for i in range(tent):
        r = requests.delete(API + path, headers={"Authorization": "Bearer " + access}, timeout=25)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)
# Forma do DELETE por tipo (confirmado na doc de CADA campanha):
#  (A) só promotion_type (nível de item, SEM promotion_id/offer_id): PRICE_DISCOUNT, LIGHTNING, DOD.
#      OBS: relâmpago/DOD, uma vez ATIVAS, não saem por API (só pausando o anúncio).
#  (B) promotion_type + promotion_id + offer_id (obrigatório):
#      SMART, PRICE_MATCHING, PRICE_MATCHING_MELI_ALL, MARKETPLACE_CAMPAIGN, PRE_NEGOTIATED,
#      UNHEALTHY_STOCK, VOLUME.  (mandar offer_id errado/faltando => 200 sem remover)
#  (C) promotion_type + promotion_id (SEM offer_id): DEAL, SELLER_CAMPAIGN, SELLER_COUPON_CAMPAIGN.
TIPOS_SO_TIPO = {"PRICE_DISCOUNT", "LIGHTNING", "DOD"}
TIPOS_COM_OFFER = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "MARKETPLACE_CAMPAIGN",
                   "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "VOLUME", "BANK"}
# Relâmpago e Oferta do dia, uma vez ATIVAS (started), a doc oficial diz que NÃO saem por API
# ("Uma vez ativadas, as ofertas não podem ser removidas... pode pausar o item"). Estado terminal:
# só pausando o anúncio ou esperando acabar estoque/horário. Não adianta reconferir/re-tentar.
LIGHTNING_DOD = {"LIGHTNING", "DOD"}
def remover_participacao(iid, p, access):
    """SAI de UMA promoção pelo TIPO, na forma EXATA que a doc de cada campanha manda (A/B/C acima).
    'p' vem de rec.participacoes_ativas: {promotion_id, type, offer_id, name}. Retorna (status, corpo)."""
    ptipo = (p.get("type") or "").upper()
    if ptipo in TIPOS_SO_TIPO:                         # (A) nível de item
        qs = f"?promotion_type={ptipo}&app_version=v2"
    else:
        qs = f"?promotion_type={ptipo}&promotion_id={p.get('promotion_id')}&app_version=v2"
        if ptipo in TIPOS_COM_OFFER and p.get("offer_id"):   # (B) precisa do offer_id
            qs += f"&offer_id={p['offer_id']}"
        # (C) DEAL/SELLER_CAMPAIGN/SELLER_COUPON_CAMPAIGN: fica só type+id
    return req_delete(f"/seller-promotions/items/{iid}{qs}", access)
def remover_todas(iid, access):
    """Remove em MASSA todas as ofertas do item (endpoint bulk do ML).
    Não remove relâmpago(LIGHTNING)/DOD — esses só pausando o anúncio.
    Retorna (status, resumo_legível, corpo_bruto)."""
    sc, body = req_delete(f"/seller-promotions/items/{iid}?app_version=v2", access)
    ok_ids, errs = [], []
    if isinstance(body, dict):
        ok_ids = [x.get("offer_id") for x in (body.get("successful_ids") or [])]
        errs = body.get("errors") or []
    resumo = f"bulk {sc}: {len(ok_ids)} removida(s)" + (f", {len(errs)} erro(s)" if errs else "")
    return sc, resumo, body
# ---------------------------------------------------------------------------
# DUAS FONTES, UMA LISTA.
#
# rec.participacoes_ativas() varre as CAMPANHAS DO VENDEDOR
# (/seller-promotions/users/{seller} cruzado com promotions/{id}/items). É o caminho
# confiável para cofinanciadas — mas só enxerga o que TEM promotion_id.
#
# O desconto individual (PRICE_DISCOUNT) não é campanha: é oferta de NÍVEL DE ITEM,
# sem promotion_id (confirmado no diagnóstico: promo_id null). Relâmpago e oferta do
# dia moram no mesmo lugar. Nenhuma das três aparece naquela varredura — elas só
# existem no endpoint por item.
#
# A consequência era silenciosa e cara: numa TROCA de desconto individual para uma
# campanha cofinanciada, o individual não era encontrado e portanto não era removido.
# O anúncio ficaria nas DUAS promoções e venderia pela mais barata — que costuma ser
# a de margem pior. Exatamente o que a troca existe para evitar.
#
# Esta função junta as duas fontes. Todos os caminhos que removem promoção passam a
# usar ela, então o conserto vale para trocar, para sair e para a 2ª passada de uma vez.
# ---------------------------------------------------------------------------
TIPOS_NIVEL_ITEM = {"PRICE_DISCOUNT", "LIGHTNING", "DOD"}
def participacoes_completas(iid, seller_id, access, ofertas=None):
    """Todas as promoções ATIVAS do item, das duas fontes, deduplicadas.
    As de nível de item entram com nivel_item=True e promotion_id possivelmente None —
    o remover_participacao já trata esses três tipos pelo promotion_type, que é a forma
    que a doc do ML manda para eles."""
    achadas = list(rec.participacoes_ativas(iid, seller_id, access) or [])
    vistos = {(str(p.get("promotion_id") or ""), (p.get("type") or "").upper()) for p in achadas}
    tipos_vistos = {(p.get("type") or "").upper() for p in achadas}
    if ofertas is None:
        ofertas = rec.ofertas_do_item(iid, access)
    for o in (ofertas or []):
        if not isinstance(o, dict) or not rec.eh_ativa(o):
            continue
        t = (o.get("type") or "").upper()
        if t not in TIPOS_NIVEL_ITEM:
            continue                       # campanha de verdade já veio da fonte 1
        if t in tipos_vistos:
            continue                       # não duplica o mesmo tipo
        chave = (str(o.get("id") or ""), t)
        if chave in vistos:
            continue
        vistos.add(chave); tipos_vistos.add(t)
        achadas.append({"promotion_id": o.get("id"),
                        "type": t,
                        "offer_id": o.get("offer_id") or o.get("ref_id"),
                        "name": o.get("name") or ("Desconto individual" if t == "PRICE_DISCOUNT" else t),
                        "status": (o.get("status") or "started"),
                        "nivel_item": True})
    return achadas
def sair_das_outras(iid, seller_id, access, manter_pid=None, manter_tipo=None):
    """SAI de TODAS as promoções em que o item está ATIVO, exceto a que queremos manter.
    Usa o caminho CONFIÁVEL da doc (rec.participacoes_ativas): users/{seller} cruzado com
    promotions/{id}/items?item_id=... — porque /seller-promotions/items/{id} NÃO traz as
    ativas de campanha cofinanciada/marketplace (só candidatas). Sai por TIPO, uma a uma.
    Retorna (saiu[], falhou[], restantes[])."""
    ativas = participacoes_completas(iid, seller_id, access)
    saiu, falhou = [], []
    for p in ativas:
        if manter_pid and p.get("promotion_id") == manter_pid:
            continue
        if manter_pid is None and manter_tipo and (p.get("type") or "").upper() == manter_tipo:
            continue  # sem id da nova (ex.: relâmpago): não sai de outra do mesmo tipo
        scd, body = remover_participacao(iid, p, access)
        rot = f"{p.get('name') or p.get('type')}({p.get('type')}:{scd})"
        (saiu if scd in (200, 201) else falhou).append(rot)
    # confere de novo: o que sobrou ativo além da que mantivemos
    restantes = [p for p in participacoes_completas(iid, seller_id, access)
                 if not (manter_pid and p.get("promotion_id") == manter_pid)
                 and not (manter_pid is None and manter_tipo and (p.get("type") or "").upper() == manter_tipo)]
    return saiu, falhou, restantes
def executar_sair(fila, iid, ofertas, access):
    """SAIR — 1ª PASSADA: sai de cada promoção em que o item participa (status started).
    NÃO confere agora (o ML leva segundos pra propagar): o sucesso é o 200 do DELETE.
    A conferência de verdade acontece na 2ª passada (revarrer_sair), no fim do main()."""
    seller_id = str(fila.get("seller_id") or "")
    ativas = participacoes_completas(iid, seller_id, access)
    achou = " ;; ".join(f"{(p.get('name') or '?')[:16]}[{p.get('type')}]" for p in ativas) or "nenhuma"
    if DRY:
        gravar(fila["id"], {"status": "aprovada", "resultado": f"[SIMULADO] SAIR de: {achou}"})
        print(f"  [DRY] sair {iid} -> {achou}", flush=True)
        return "simulado"
    dels, todos_ok = [], True
    for p in ativas:
        scd, body = remover_participacao(iid, p, access)   # DELETE por tipo (offer_id só onde a doc exige)
        if scd not in (200, 201):
            todos_ok = False
        dels.append(f"{(p.get('name') or '?')[:16]}[{p.get('type')}]:{scd}")
    remover_todas(iid, access)   # catch-all (menos LIGHTNING/DOD)
    gravar(fila["id"], {
        "status": "aplicada" if todos_ok else "erro",
        "resultado": f"1ª passada — ACHOU: {achou} || DELETE: {' ;; '.join(dels) or 'nada'} (confere na 2ª passada)",
    })
    print(f"  [{'OK' if todos_ok else 'ERRO'}] sair {iid} (1ª passada) | {len(ativas)} promo(s)", flush=True)
    return "saiu" if todos_ok else "erro_sair"
def revarrer_sair(fila, access):
    """SAIR — 2ª PASSADA (no fim do main, já com tempo de propagação): varre o item de novo,
    remove qualquer teimoso e CLASSIFICA o que sobrou (aprendizado da doc — remoção é ASSÍNCRONA):
      • sobrou NADA                       -> 'aplicada' ✓ (saiu de tudo)
      • sobrou só RELÂMPAGO/DOD ativa     -> 'erro' terminal honesto: NÃO sai por API (só pausando)
      • sobrou outro tipo mas DELETE=200  -> 'aprovada': é propagação assíncrona (restore_requested);
                                             o main só processa 'aprovada', então RECONFERE na próxima rodada
      • sobrou e algum DELETE != 200      -> 'erro' real (ex.: offer_id errado)."""
    iid = fila["item_id"]
    seller_id = str(fila.get("seller_id") or "")
    rest = participacoes_completas(iid, seller_id, access)
    dels2 = []                          # (rótulo, status_http)
    for p in rest:
        scd, body = remover_participacao(iid, p, access)
        dels2.append((f"{(p.get('name') or '?')[:16]}[{p.get('type')}]:{scd}", scd))
    if dels2:
        remover_todas(iid, access)
    final = participacoes_completas(iid, seller_id, access)   # conferência final
    sobrou = " ;; ".join(f"{(p.get('name') or '?')[:16]}[{p.get('type')}]" for p in final) or "nada ✓"
    dels_txt = " ;; ".join(d[0] for d in dels2) or "nenhum"
    base = (fila.get("resultado") or "").split(" || 2ª passada")[0]
    if not final:
        novo_status, resfim, ret = "aplicada", f"SOBROU: {sobrou}", "saiu"
    else:
        tipos_final = {(p.get("type") or "").upper() for p in final}
        so_luz_dod = bool(tipos_final) and tipos_final <= LIGHTNING_DOD
        del_falhou = any(sc not in (200, 201) for _, sc in dels2)
        if so_luz_dod:
            novo_status, ret = "erro", "bloqueada_luz_dod"
            resfim = (f"⚠️ SOBROU relâmpago/oferta-do-dia ATIVA ({sobrou}) — pela regra da ML NÃO sai "
                      f"por API; só PAUSANDO o anúncio ou esperando acabar o estoque/horário")
        elif del_falhou:
            novo_status, resfim, ret = "erro", f"SOBROU: {sobrou} (algum DELETE não deu 200)", "erro_sair"
        else:
            # DELETE respondeu 200 mas ainda consta ativo => propagação assíncrona da ML.
            # Mantém 'aprovada' pra RECONFERIR na próxima rodada (não é erro, nem está resolvido).
            novo_status, ret = "aprovada", "aguardando_ml"
            resfim = (f"⏳ aguardando processamento da ML (assíncrono): pedi p/ sair, ainda consta ativo "
                      f"({sobrou}). Reconfiro na próxima rodada")
    gravar(fila["id"], {
        "status": novo_status,
        "resultado": f"{base} || 2ª passada — teimosos: {dels_txt} || {resfim}",
    })
    print(f"  [{ret}] sair {iid} (2ª passada) | sobrou: {sobrou}", flush=True)
    return ret
def confirmar_pos_entrada(fila, access):
    """ENTRAR/TROCAR — 2ª PASSADA (no fim do main, com tempo de propagação).
    PROVA REAL = sale_price: se o cliente JÁ paga o preço com desconto (ou a promoção ativa no
    sale_price é a do tipo que entramos), está CONFIRMADO e o item SAI da fila. Se o preço ainda
    está cheio / sem promoção, NÃO confirmou -> volta pra fila (aguardando_ml) e reconfere de hora
    em hora. Fallback: o status 'started' na promoção alvo. No TROCAR, ainda sai das outras.
    Só mexe se a 1ª passada marcou 'aplicada' (se o POST falhou, mantém o erro)."""
    iid = fila["item_id"]
    seller_id = str(fila.get("seller_id") or "")
    pid = fila.get("promocao_id")
    tipo = (fila.get("promocao_tipo") or "").upper()
    alvo = fila.get("preco_alvo")
    base = (fila.get("resultado") or "").split(" || 2ª passada")[0]
    # 1) PROVA POR PREÇO: o que o cliente paga agora
    pv, ptipo = preco_venda_real(iid, access)
    preco_confirma = (pv is not None and alvo not in (None, "") and float(pv) <= float(alvo) * 1.03)
    # O /items/{id}/sale_price chama o desconto individual de "custom" no
    # metadata.promotion_type — não de PRICE_DISCOUNT. Sem esse apelido, uma entrada
    # BEM-SUCEDIDA ficava eternamente em "ativando": o cliente já pagava o preço novo,
    # mas o nome não batia e o item voltava pra fila de reconferência pra sempre.
    _APELIDOS_SALE_PRICE = {"PRICE_DISCOUNT": {"PRICE_DISCOUNT", "CUSTOM"}}
    _tipos_ok = _APELIDOS_SALE_PRICE.get(tipo, {tipo})
    tipo_confirma = (ptipo is not None and str(ptipo).upper() in _tipos_ok)
    # 2) FALLBACK: status 'started' na promoção alvo
    conf_started = confirmar_entrada(iid, pid, tipo, access)
    if preco_confirma or tipo_confirma or conf_started is True:
        confirmado, ret = True, "confirmado"
        prova = (f"cliente paga R${pv:.2f} agora" + (f" via {ptipo}" if ptipo else "")
                 + (f" (alvo R${float(alvo):.2f})" if alvo not in (None, "") else "")) if (preco_confirma or tipo_confirma) \
                else "promoção ATIVA (started) ✓"
        nota = f"✓ CONFIRMADO — {prova}"
    elif (pv is not None) or (conf_started is False):
        # sinal NEGATIVO real: preço ainda cheio / promo não 'started' -> volta pra fila
        confirmado, ret = False, "ativando"
        cliente = (f"cliente ainda paga R${pv:.2f}" + (f" ({ptipo})" if ptipo else " sem desconto")) if pv is not None \
                  else "ainda não 'started'"
        nota = (f"⏳ ainda NÃO confirmou pelo preço ({cliente}) — volta pra fila e reconfiro de hora em hora")
    else:
        # não dá pra conferir (ex.: relâmpago sem sale_price e sem 'started'): não fica em loop
        confirmado, ret = True, "confirmado"
        nota = "entrada não conferível por API (relâmpago) — considero aplicada"
    extra = ""
    if (fila.get("acao") or "") == "trocar":
        if confirmado:
            # nova PROVADA pelo preço -> AGORA sai das antigas. O item nunca ficou descoberto:
            # a antiga segurou o desconto até esta confirmação. Mantém a nova por promotion_id.
            # APRENDIZADO DO TESTE REAL: o DELETE de SMART volta 200 com corpo VAZIO e o ML tira a
            # participação de forma ASSÍNCRONA (leva segundos). Relistar AGORA ainda mostra as que
            # JÁ estão saindo -> dava falso "ainda ativas". Então NÃO relisto pra conferir na hora:
            # confio no status do DELETE (200 = pedido aceito). Só marco problema se o ML RECUSAR.
            rest = [p for p in participacoes_completas(iid, seller_id, access) if p.get("promotion_id") != pid]
            oks, ruins = 0, []
            for p in rest:
                scd, _b = remover_participacao(iid, p, access)
                if scd in (200, 201):
                    oks += 1
                else:
                    ruins.append(f"{p.get('name') or p.get('type')}:{scd}")
            if not rest:
                extra = " | nenhuma antiga a remover ✓"
            elif not ruins:
                extra = f" | pedi saída de {oks} antiga(s) ✓ (o ML remove em segundos — assíncrono)"
            else:
                extra = " | ⚠️ DELETE recusado em: " + ", ".join(ruins)
        else:
            # nova ainda NÃO confirmou pelo preço -> MANTÉM as antigas (rede intacta). A saída fica
            # pra reconferência da fila (aguardando_ml): quando a nova pegar, aí sim sai das antigas.
            extra = " | mantive as antigas (nova ainda não confirmou pelo preço — saio quando ela pegar)"
    gravar(fila["id"], {"status": "aplicada",
                        "resultado": f"{base} || 2ª passada — {nota}{extra}"})
    print(f"  [{ret}] {fila.get('acao')} {iid} (2ª passada) | {nota}", flush=True)
    return ret
def _sugestao_atual(iid, seller_id):
    """Lê a sugestão MAIS NOVA e VIVA do robô pra este item (status != 'aplicada', que é leftover
    congelado). Serve pra o aplicador agir na recomendação ATUAL, não numa ordem antiga da fila.
    Retorna o dict da sugestão fresca ou None."""
    try:
        rows = (sb.table("repricer_sugestoes")
                .select("acao,promocao_id,promocao_tipo,promocao_nome,status,criado_em")
                .eq("seller_id", str(seller_id)).eq("item_id", iid)
                .neq("status", "aplicada")
                .order("criado_em", desc=True).limit(1).execute().data) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"  aviso: não li a sugestão atual de {iid}: {e}", flush=True)
        return None
# ===================== CACHE POR RODADA (evita repetir a MESMA pergunta ao ML) =====================
# Quando muitos itens são da MESMA campanha, o detalhe/vigência da promoção é IDÊNTICO pra todos.
# Guardar a 1ª resposta e reusar corta dezenas de chamadas iguais, sem mudar nenhuma decisão.
_VIG_CACHE = {}     # promotion_id -> bool (vigente?)
_DET_CACHE = {}     # (promotion_id, tipo) -> dict detalhe
def _cand_vigente_cached(cand, access):
    k = cand.get("id")
    if k in _VIG_CACHE:
        return _VIG_CACHE[k]
    v = rec.cand_vigente(cand, access)
    if k:
        _VIG_CACHE[k] = v
    return v
def _promo_detalhe_cached(pid, tipo, access):
    k = (pid, tipo)
    if k in _DET_CACHE:
        return _DET_CACHE[k]
    d = rec._promo_detalhe(pid, tipo, access)
    _DET_CACHE[k] = d
    return d
def processar(fila, access):
    iid = fila["item_id"]
    tipo = (fila.get("promocao_tipo") or "").upper()
    acao = fila.get("acao")
    if acao not in ("entrar", "trocar", "sair"):
        gravar(fila["id"], {"status": "erro", "resultado": f"ação '{acao}' não aplicável aqui"})
        return "acao_invalida"
    ofertas = rec.ofertas_do_item(iid, access)
    if not isinstance(ofertas, list):
        ofertas = []
    if acao == "sair":
        return executar_sair(fila, iid, ofertas, access)
    # ---- RE-SINCRONIZA com a sugestão MAIS NOVA do robô (não age em ordem antiga da fila) ----
    # A fila guarda a decisão do momento em que você aprovou; se a sugestão foi rerodada depois,
    # a recomendação pode ter mudado (ex.: o item entrou na promoção e virou 'manter'). Usa sempre
    # a sugestão fresca do robô. 'aplicada' (leftover congelado) é ignorada por _sugestao_atual.
    nova = None if fila.get("escolha_manual") else _sugestao_atual(iid, fila.get("seller_id"))
    if nova is not None:
        nacao = (nova.get("acao") or "").lower()
        if nacao == "manter":
            gravar(fila["id"], {"status": "aplicada",
                "resultado": "já estava ATIVA no item ✓ (sugestão atual do robô: manter — não precisou entrar)"})
            return "ja_ativa"
        if nacao in ("entrar", "trocar"):
            # REGRA DE OURO: auto-retenta só a MESMA promoção aprovada. Se estamos num RETRY
            # (tentativas>0) e a sugestão fresca aponta pra OUTRA promoção, NÃO troco sozinho —
            # oferta nova é decisão sua. Marca divergência e sai do auto-retry.
            orig_pid = str(fila.get("promocao_id") or "")
            nova_pid = str(nova.get("promocao_id") or "")
            eh_retry = int(fila.get("tentativas") or 0) > 0
            if eh_retry and orig_pid and nova_pid and nova_pid != orig_pid:
                msg = (f"a sugestão MUDOU de promoção (aprovado {orig_pid} '{fila.get('promocao_nome') or ''}' "
                       f"→ agora {nova_pid} '{nova.get('promocao_nome') or ''}'). NÃO troquei sozinho: "
                       f"oferta nova é decisão sua — reveja no painel.")
                gravar(fila["id"], {"status": "revisar", "resultado": msg})
                return "divergencia"
            acao = nacao
            fila = {**fila, "acao": nacao,
                    "promocao_id": nova.get("promocao_id") or fila.get("promocao_id"),
                    "promocao_tipo": nova.get("promocao_tipo") or fila.get("promocao_tipo"),
                    "promocao_nome": nova.get("promocao_nome") or fila.get("promocao_nome")}
            tipo = (fila.get("promocao_tipo") or "").upper()
        # outras ações (raro): segue com o que veio na fila
    # (se nova is None: não há sugestão VIVA; o estado ao vivo abaixo decide já-ativa × candidato sumiu)
    if tipo not in TIPOS_OK:
        gravar(fila["id"], {"status": "erro", "resultado": f"tipo {tipo} ainda não suportado pelo aplicador"})
        return "tipo_nao_suportado"
    if not ofertas:
        gravar(fila["id"], {"status": "erro", "resultado": "sem promoções no item agora (candidato expirou?)"})
        return "sem_oferta"
    # snapshot das ATIVAS de agora. 'trocar' vai sair de todas elas e ficar só na sugerida.
    # 'entrar' não mexe nas outras. Só entra em quem está 'candidate' agora (a sugerida nunca é ativa).
    antigas = [o for o in ofertas if rec.eh_ativa(o)]
    cand = achar_candidato(ofertas, fila)
    if not cand:
        # NÃO achou candidato do tipo alvo. Se o item JÁ ESTÁ ATIVO nesse tipo, não é erro:
        # é um resultado BOM (o anúncio já está na promoção) — marca 'já ativa ✓', não vermelho.
        ja = any((o.get("type") or "").upper() == tipo and rec.eh_ativa(o) for o in ofertas)
        if ja:
            # TROCA adiada que finalmente pegou: a nova está ATIVA agora. É AQUI que a troca se
            # COMPLETA — saímos das antigas mantendo a nova (por promotion_id). Sem essa etapa, o
            # item ficaria pra sempre nas DUAS promoções (podendo vender pela antiga, de margem pior).
            # Sem promotion_id (ex.: relâmpago) não dá pra isolar a nova com segurança -> não removo nada.
            extra_troca = ""
            if acao == "trocar" and fila.get("promocao_id"):
                saiu, falhou, _rest = sair_das_outras(iid, str(fila.get("seller_id") or ""), access,
                                                      manter_pid=fila.get("promocao_id"))
                # NÃO uso a relista (_rest): é assíncrona e dá falso "ainda ativas". Reporto pelo
                # status do DELETE (saiu = 200; falhou = recusado de verdade).
                if falhou:
                    extra_troca = " | ⚠️ DELETE recusado em: " + ", ".join(falhou)
                elif saiu:
                    extra_troca = f" | pedi saída de {len(saiu)} antiga(s) ✓ (assíncrono)"
            gravar(fila["id"], {"status": "aplicada",
                "resultado": f"já estava ATIVA no item ✓ (não precisou entrar){extra_troca}"})
            return "ja_ativa"
        gravar(fila["id"], {"status": "erro",
            "resultado": "candidato aprovado não está mais disponível — rode a sugestão de novo"})
        return "sem_candidato"
    # trava de vigência (consulta o detalhe da promoção): nunca entrar em promo programada/futura.
    # Fica ANTES da remoção das outras, pra nunca deixar o item sem promoção por causa disso.
    if not _cand_vigente_cached(cand, access):
        gravar(fila["id"], {"status": "erro",
                            "resultado": "promoção ainda não está vigente (programada) — não apliquei (item mantido como estava)"})
        return "programada"
    # VIGÊNCIA A NÍVEL DE ITEM: a campanha pode estar 'started' mas a participação DESTE anúncio
    # começar no futuro (ex.: amanhã). Consulta a data do item na promoção e bloqueia se for futura.
    try:
        stx, dix = rec.get(f"/seller-promotions/promotions/{cand.get('id')}/items"
                           f"?promotion_type={tipo}&item_id={iid}&app_version=v2", access)
        for it in ((dix.get("results") if isinstance(dix, dict) else None) or []):
            if str(it.get("id")) == str(iid):
                ini = it.get("start_date")
                if ini and not rec._vigente({"start_date": ini}):
                    gravar(fila["id"], {"status": "erro",
                        "resultado": f"vigência do ITEM é futura (começa {str(ini)[:10]}) — não apliquei (mantido como estava)"})
                    return "vigencia_futura"
                break
    except Exception:
        pass
    # ---- RE-CHECAGEM de margem (a trava de segurança) ----
    st, it = rec.get(f"/items/{iid}", access)
    if not isinstance(it, dict):
        gravar(fila["id"], {"status": "erro", "resultado": "não consegui ler o item pra recalcular margem"})
        return "sem_item"
    ltid = it.get("listing_type_id")
    cat = it.get("category_id")
    # O SKU mora em TRÊS lugares no ML: seller_custom_field (campo antigo), o atributo
    # SELLER_SKU (formulário atual) e dentro das variações. Aqui liamos só os dois
    # primeiros — e o rec.sku_do_item() já resolve os três, com a regra de não escolher
    # nada quando as variações discordam entre si.
    # Sem isso, um anúncio com SKU só no atributo (ex.: MLB3967924417, SKU MLPA003)
    # chegava como "sem custo" e o aplicador desistia — de um item que TEM custo
    # cadastrado. Só não quebrou até agora porque o painel manda o sku na fila; pelo
    # retry ou por qualquer caminho sem esse campo, quebraria.
    sku = rec.sku_do_item(it) or fila.get("sku")
    custo = rec.custo_efetivo(iid, sku)
    if custo is None:
        gravar(fila["id"], {"status": "erro", "resultado": "sem custo pra recalcular margem (preencha o custo do anúncio no painel)"})
        return "sem_custo"
    frete, _ = rec.frete_de(sku, iid, access)
    piso, grupo = rec.margem_minima_do(sku)
    # DIAGNÓSTICO geral (qualquer tipo): despeja os campos do candidato que o ML traz,
    # pra sabermos exatamente o que o POST precisa (ex.: start_date nas cofinanciadas).
    _diag = {k: cand.get(k) for k in (
        "id", "ref_id", "type", "status", "price", "original_price", "min_discounted_price",
        "max_discounted_price", "suggested_discounted_price", "stock", "start_date", "finish_date",
        "meli_percentage", "seller_percentage", "name")}
    _diag["ITEM_price"] = it.get("price")                 # preço ATUAL do anúncio (referência da credibilidade?)
    _diag["ITEM_base_price"] = it.get("base_price")
    # despeja TODAS as ofertas que o robô vê do item (tipo/status/ativa?) — pra saber se as
    # promoções "ativas" aparecem aqui e se eh_ativa as reconhece.
    _diag["OFERTAS"] = [{"t": o.get("type"), "st": o.get("status"), "id": o.get("id"),
                         "ref": o.get("ref_id"), "ativa": rec.eh_ativa(o)}
                        for o in ofertas if isinstance(o, dict)]
    _diag["N_ANTIGAS"] = len(antigas)
    light_diag = " || DIAG " + json.dumps(_diag, ensure_ascii=False)
    # RELÂMPAGO: o ML exige um desconto CRÍVEL (o suggested_discounted_price é só um teto e é
    # recusado). A credibilidade quer desconto de verdade. Estratégia: oferecer o MAIOR desconto
    # possível que ainda mantenha a margem >= piso (foi o que funcionou na mão: raspa o piso).
    # Busca binária pelo menor preço com margem >= piso, dentro da faixa [min, max] do ML.
    if tipo == "LIGHTNING":
        try:
            mn = float(cand.get("min_discounted_price") or 0)
            mx = float(cand.get("max_discounted_price") or cand.get("price") or 0)
            base = dict(cand)
            def _marg(pb):
                base["price"] = round(pb, 2)
                e = rec.avaliar(base, cat, ltid, access, frete, custo)
                return (e["margem"] if e else -999)
            if mx > 0 and _marg(mx) >= piso:
                lo, hi = max(mn, 0.5), mx      # queremos o MENOR preço (maior desconto) com margem >= piso
                for _ in range(26):
                    mid = (lo + hi) / 2.0
                    if _marg(mid) >= piso:
                        hi = mid
                    else:
                        lo = mid
                cand = dict(cand)
                cand["price"] = round(hi, 2)
        except (TypeError, ValueError):
            pass
    # ---- DESCONTO INDIVIDUAL (PRICE_DISCOUNT): o preço é SEU, não do ML ----
    # A doc do desconto individual é explícita: o vendedor informa o deal_price. Por isso
    # o candidato chega com price=0 — não é dado faltando, é o ML dizendo "escolhe você".
    # O que ele manda é a FAIXA CRÍVEL, em min/max_discounted_price (conferido nos itens
    # reais: min = 20% do original = teto de 80% de desconto; max = 5% a 10% de desconto,
    # calculado pelo ML item a item).
    # Sem preencher cand["price"], o rec.avaliar() devolvia None e o item morria em
    # 'sem_avaliar' sem explicação.
    # DE ONDE VEM O PREÇO, nesta ordem:
    #   1. fila.preco_alvo         -> você fixou um preço na tela
    #   2. fila.margem_alvo_manual -> você escolheu uma MARGEM-ALVO (é o que a tela guarda
    #                                 hoje ao montar a promoção individual; o campo de preço
    #                                 dela depende de comissao_pct, que o log não tem)
    # NÃO existe caminho 3. Se você não escolheu preço nem margem, o aplicador PARA.
    # Cair no piso do grupo seria aplicar uma margem que você não pediu — chute com
    # aparência de regra. Melhor um item parado com o motivo escrito do que um desconto
    # no ar com um número que ninguém decidiu.
    # Preço fixado ganha da margem: é decisão explícita, não conta derivada.
    if not rec.preco_oferta(cand) and (cand.get("min_discounted_price") is not None
                                       or cand.get("max_discounted_price") is not None):
        _mn = float(cand.get("min_discounted_price") or 0.5)
        _mx = float(cand.get("max_discounted_price") or cand.get("original_price") or 0)
        _orig = float(cand.get("original_price") or 0)
        # Calculadora de margem por preço, usando a COMISSÃO REAL da API do ML.
        # Fica aqui em cima porque serve tanto pra escolher o preço quanto pra dizer,
        # nos avisos, qual margem este anúncio realmente permite.
        _base_calc = dict(cand)
        def _marg_em(pb):
            _base_calc["price"] = round(pb, 2)
            _e = rec.avaliar(_base_calc, cat, ltid, access, frete, custo)
            return _e["margem"] if _e else -999
        def _desc_de(pb):
            return ((1 - float(pb) / _orig) * 100) if _orig else 0
        def _faixa_txt():
            """A MELHOR e a PIOR margem que este anúncio permite dentro da faixa do ML.
            Margem cresce com o preço, então o teto está no menor desconto (_mx) e o
            piso no maior desconto (_mn)."""
            _melhor, _pior = _marg_em(_mx), _marg_em(_mn)
            return (f"neste anúncio a margem vai de {_pior:.1f}% (desconto máximo, preço R${_mn:.2f}) "
                    f"até {_melhor:.1f}% (desconto mínimo de {_desc_de(_mx):.0f}%, preço R${_mx:.2f})")
        _preco_tela = fila.get("preco_alvo")
        _marg_tela = fila.get("margem_alvo_manual")
        # Nenhuma escolha sua = nada feito. Sem fallback.
        if _preco_tela in (None, "") and _marg_tela in (None, ""):
            gravar(fila["id"], {"status": "erro",
                "resultado": (f"{tipo}: neste desconto quem define o preço é você, e a fila não trouxe "
                              f"nem preço nem margem-alvo. NÃO apliquei nada — não vou escolher a margem "
                              f"no seu lugar. Para você decidir: {_faixa_txt()}. Preço cheio "
                              f"R${_orig:.2f}. Monte a promoção individual no painel escolhendo a margem.")})
            print(f"  ! sem_preco_alvo {iid}: sem escolha sua — {_faixa_txt()}", flush=True)
            return "sem_preco_alvo"
        if _preco_tela not in (None, ""):
            # caminho 1: você fixou um PREÇO. Ele manda; só encaixamos na faixa do ML.
            _p = _clamp_preco(_preco_tela, cand)
            _origem = f"preço escolhido na tela R${float(_preco_tela):.2f}"
            if abs(float(_p) - float(_preco_tela)) > 0.005:
                print(f"  ~ {iid}: preço R${float(_preco_tela):.2f} ajustado para R${_p:.2f} "
                      f"(faixa do ML: {_mn}–{_mx})", flush=True)
        else:
            # caminho 2: você escolheu uma MARGEM-ALVO (é o que a tela guarda em
            # margem_alvo_manual quando você monta a promoção individual). Procuramos o
            # MENOR preço da faixa crível que ainda entrega essa margem — ou seja, o maior
            # desconto compatível com o que você pediu.
            # Aqui o aplicador é mais confiável que a estimativa do painel: rec.avaliar
            # consulta a COMISSÃO REAL na API do ML, em vez de estimar.
            _alvo_m = float(_marg_tela)      # só chega aqui se você escolheu; sem fallback
            _fonte_m = "margem-alvo escolhida na tela"
            _m_topo = _marg_em(_mx) if _mx > 0 else -999
            if _m_topo < _alvo_m:
                # A margem que você pediu não cabe na faixa do ML nem no menor desconto.
                # PARA — inclusive com IGNORAR_PISO ligado. Ignorar o PISO é uma decisão
                # sua sobre o piso; aplicar uma margem DIFERENTE da que você pediu é outra
                # coisa, e essa eu não tomo. O log traz o número exato pra você reescolher.
                gravar(fila["id"], {"status": "erro",
                    "resultado": (f"{tipo}: a margem-alvo de {_alvo_m:.1f}% não cabe neste anúncio. "
                                  f"A MELHOR MARGEM POSSÍVEL aqui é {_m_topo:.1f}%, no preço R${_mx:.2f} "
                                  f"(desconto de {_desc_de(_mx):.0f}%, o menor que o ML aceita neste item; "
                                  f"preço cheio R${_orig:.2f}). Descontar menos que isso o ML não permite, "
                                  f"e descontar mais só derruba a margem. NÃO apliquei — se {_m_topo:.1f}% "
                                  f"servir, escolha essa margem no painel.")})
                print(f"  ! margem_inalcancavel {iid}: pediu {_alvo_m:.1f}% | MELHOR POSSÍVEL "
                      f"{_m_topo:.1f}% a R${_mx:.2f} (desconto {_desc_de(_mx):.0f}%) — não apliquei",
                      flush=True)
                _gravar_melhor(fila["id"], _m_topo, _mx)   # pro painel listar em massa
                return "margem_inalcancavel"
            else:
                lo, hi = max(_mn, 0.5), _mx
                for _ in range(26):
                    mid = (lo + hi) / 2.0
                    if _marg_em(mid) >= _alvo_m:
                        hi = mid          # ainda segura o alvo -> pode descontar mais
                    else:
                        lo = mid
                _p = _clamp_preco(round(hi, 2), cand)
                _origem = f"{_fonte_m} {_alvo_m:.1f}% -> preço R${_p:.2f}"
            print(f"  ~ {iid}: {tipo} por {_origem} | faixa do ML {_mn:.2f}–{_mx:.2f}", flush=True)
        cand = dict(cand)
        cand["price"] = _p
    ev = rec.avaliar(cand, cat, ltid, access, frete, custo)
    if not ev:
        # rec.avaliar() só devolve None em DOIS casos: falta o preço da oferta
        # (preco_oferta) ou falta o original_price. A mensagem antiga não dizia qual —
        # e sem isso não dá pra consertar, só adivinhar. Agora ela diz, e despeja o
        # candidato inteiro pro caso de aparecer um terceiro motivo que não previmos.
        falta = []
        if not rec.preco_oferta(cand):
            falta.append("preço da oferta (price/deal_price/…)")
        if not cand.get("original_price"):
            falta.append("original_price")
        det = {"promo_id": cand.get("id"), "tipo": cand.get("type"),
               "status": cand.get("status"), "ref_id": cand.get("ref_id"),
               "price": cand.get("price"), "original_price": cand.get("original_price"),
               "min": cand.get("min_discounted_price"), "max": cand.get("max_discounted_price"),
               "ITEM_price": it.get("price")}
        msg = "não deu pra avaliar: faltou " + (", ".join(falta) or "motivo desconhecido")
        gravar(fila["id"], {"status": "erro",
                            "resultado": msg + " || CAND " + json.dumps(det, ensure_ascii=False)})
        print(f"  ! sem_avaliar {iid}: {msg} | {json.dumps(det, ensure_ascii=False)}", flush=True)
        return "sem_avaliar"
    abaixo_do_piso = ev["margem"] < piso
    if abaixo_do_piso and not IGNORAR_PISO:
        gravar(fila["id"], {"status": "erro",
                            "resultado": f"margem caiu pra {ev['margem']:.1f}% (< piso {piso:.0f}%) — não apliquei"})
        return "abaixo_piso"
    # IGNORAR_PISO ligado: segue em frente, mas o aviso viaja junto até o campo
    # resultado (nos dois desfechos, simulado e real) e aparece no painel.
    aviso_piso = ""
    if abaixo_do_piso:
        aviso_piso = (f"⚠️ ABAIXO DO PISO — margem {ev['margem']:.1f}% < piso {piso:.0f}% "
                      f"(aplicado por decisão manual no Acelerar) || ")
        print(f"  ⚠ {iid}: margem {ev['margem']:.1f}% abaixo do piso {piso:.0f}% "
              f"— aplicando porque IGNORAR_PISO=1", flush=True)
    # cofinanciadas que exigem data no POST (ex.: "OFERTAS RELÂMPAGOS IMPERDÍVEIS" -> erro START_DATE):
    # o candidato não traz as datas; pega do DETALHE da promoção. As datas têm que ir em formato
    # LOCAL (sem 'Z') e o start NÃO pode ser no passado (regras da doc).
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "DEAL") and not cand.get("start_date"):
        pd = _promo_detalhe_cached(cand.get("id"), tipo, access)
        if isinstance(pd, dict):
            hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d")   # hoje em BRT
            ini = str(pd.get("start_date") or "")[:10]
            fim = str(pd.get("finish_date") or "")[:10]
            if ini and ini < hoje:
                ini = hoje                                   # start não pode ser anterior a hoje
            cand = dict(cand)
            if ini:
                cand["start_date"] = ini + "T00:00:00"       # começo do dia, formato local
            if fim:
                cand["finish_date"] = fim + "T23:59:59"
            light_diag += " promo_datas=" + json.dumps({"ini": cand.get("start_date"), "fim": cand.get("finish_date"), "status": pd.get("status")}, ensure_ascii=False)
    corpo = corpo_post(tipo, cand, ev["pb"])
    if not corpo:
        gravar(fila["id"], {"status": "erro", "resultado": f"não montei corpo pro tipo {tipo}"})
        return "sem_corpo"
    sair_de = [(o.get("name") or o.get("type") or "?") for o in antigas] if acao == "trocar" else []
    if DRY:
        if acao == "trocar" and tipo == "PRICE_DISCOUNT":
            plano = "TROCA (desconto individual): SAI das ativas ANTES e só então entra "
        else:
            plano = (f"TROCA: entra na sugerida e SAI das ativas " if acao == "trocar" else "ENTRADA ")
        gravar(fila["id"], {"status": "aprovada",
                            "resultado": f"{aviso_piso}[SIMULADO] {plano}| POST {json.dumps(corpo)} | margem prevista {ev['margem']:.1f}%",
                            "preco_aplicado": ev["pb"], "margem_aplicada": ev["margem"]})
        print(f"  [DRY] {acao} {iid} {tipo} -> {json.dumps(corpo)} (margem {ev['margem']:.1f}%)", flush=True)
        return "simulado"
    # ---- APLICAÇÃO REAL ----
    # ORDEM (à prova de falha, em DUAS travas):
    #   1) ENTRA na nova. Se o ML RECUSAR, não mexemos em nada (anúncio segue como estava).
    #   2) SÓ SAI das antigas depois que o sale_price CONFIRMAR que a nova está no ar (2ª passada).
    # Aprendizado das últimas passadas: o ML às vezes ACEITA a entrada (201) mas demora segundos/
    # minutos pra refletir o desconto no preço (comum nas cofinanciadas -> estado 'ativando').
    # Se saíssemos das antigas já no 201, existiria uma janela em que a nova foi aceita mas ainda
    # não pegou E a antiga já saiu -> anúncio SEM desconto. Por isso a saída fica gated pelo PREÇO.
    aviso = ""
    # ---- EXCEÇÃO DE ORDEM: desconto individual em item que já tem campanha ativa ----
    # A regra geral deste aplicador é ENTRAR primeiro e só sair da antiga depois que o
    # preço confirmar — assim o anúncio nunca fica descoberto. Para PRICE_DISCOUNT a
    # ordem se INVERTE, por uma regra da própria ML (doc do desconto individual):
    #   "Se ao iniciar o desconto o item estiver participando de um DEAL, o desconto não
    #    será aplicado até que o DEAL associado seja finalizado."
    # Entrando primeiro, o desconto ficaria DORMENTE: o sale_price nunca mudaria, a
    # confirmação nunca viria e o item ficaria preso em 'aguardando' reconferindo à toa.
    # CUSTO CONHECIDO E ACEITO: entre o DELETE e o POST o anúncio fica alguns segundos
    # sem desconto nenhum. Se o POST for recusado nesse intervalo, ele fica SEM desconto
    # até você agir — por isso esse caso é gritado no log e no resultado, não escondido.
    saiu_antes = ""
    if acao == "trocar" and tipo == "PRICE_DISCOUNT":
        _saiu, _falhou, _rest = sair_das_outras(iid, str(fila.get("seller_id") or ""), access,
                                                manter_tipo="PRICE_DISCOUNT")
        saiu_antes = f" | saí de {len(_saiu)} antiga(s) ANTES de entrar (regra da ML p/ desconto individual)"
        if _falhou:
            saiu_antes += " | ⚠️ DELETE recusado em: " + ", ".join(_falhou)
        print(f"  ~ {iid}: saí de {len(_saiu)} campanha(s) antes de aplicar o desconto individual"
              + (f" | falhou em {len(_falhou)}" if _falhou else ""), flush=True)
    sc, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
    ok = sc in (200, 201)
    if acao == "trocar":
        if tipo == "PRICE_DISCOUNT":
            aviso = saiu_antes + (" | entrei no desconto individual ✓" if ok else
                    " | 🚨 ENTRADA RECUSADA DEPOIS DE SAIR — o anúncio está SEM desconto agora. Reveja no painel.")
        elif ok:
            # NÃO saímos das antigas AINDA. A rede antiga fica de pé até o sale_price CONFIRMAR
            # (2ª passada / reconferência da fila) que a nova está no ar. Assim o anúncio NUNCA
            # fica sem desconto na janela entre "aceita (201)" e "preço realmente aplicado".
            aviso = " | entrou na sugerida ✓ (saio das antigas só após confirmar o preço)"
        else:
            # entrada recusada -> NÃO removemos nada: rede de segurança intacta
            aviso = " | ⚠️ entrada recusada — NÃO mexi nas promoções atuais (anúncio segue como estava)"
    # DESFECHO do POST. Por padrão um erro é 'erro_post' (transitório: 429/rede/candidato sumiu ->
    # retenta). MAS dois erros são "FAÇA NA MÃO" e NÃO adianta reter — precisam de você:
    #   • CREDIBILITY: o ML só aceita a relâmpago com desconto maior que seu piso permite.
    #   • START_DATE : a campanha exige uma data que o ML não aceitou pela API.
    # Esses viram 'faca_na_mao' (terminal): saem da fila, ficam pra revisão, PARAM de retentar.
    motivo = ""
    cod_erro = "erro_post"
    if not ok:
        _t = json.dumps(resp, ensure_ascii=False).upper()
        if "CREDIBILITY" in _t:
            # a doc do desconto individual chama isso de error_credibility_price:
            # "O desconto aplicado não é suficiente para ser considerado crível."
            motivo = ("FAÇA NA MÃO — o ML recusou por CREDIBILIDADE: o desconto escolhido é raso demais. "
                      f"Escolha um preço mais baixo (o ML aceita de R${cand.get('min_discounted_price')} "
                      f"a R${cand.get('max_discounted_price')} neste anúncio). ")
            cod_erro = "faca_na_mao"
        elif "START_DATE" in _t:
            motivo = "FAÇA NA MÃO — essa campanha exige data que o ML não aceitou pela API. "
            cod_erro = "faca_na_mao"
    gravar(fila["id"], {
        "status": "aplicada" if ok else "erro",
        "resultado": (f"{aviso_piso}OK {sc}{aviso}: {json.dumps(resp, ensure_ascii=False)[:220]}{light_diag}" if ok
                      else f"{aviso_piso}{motivo}{'ERRO ' + str(sc) if not aviso else 'ATENÇÃO'}{aviso} [enviei {json.dumps(corpo, ensure_ascii=False)}]: {json.dumps(resp, ensure_ascii=False)[:170]}{light_diag}"),
        "preco_aplicado": ev["pb"] if ok else None,
        "margem_aplicada": ev["margem"] if ok else None,
    })
    return "aplicado" if ok else cod_erro
def grava_status(estado, resumo=None):
    """Grava o status do aplicador pro painel ler (não depende da API do GitHub)."""
    try:
        agora = datetime.now(timezone.utc).isoformat()
        row = {"workflow": "aplicador", "seller_id": SELLER_FILTRO or "todas", "estado": estado}
        if estado == "rodando":
            row["inicio"] = agora
            row["fim"] = None
            row["resumo"] = None
        else:
            row["fim"] = agora
            row["resumo"] = resumo
        sb.table("repricer_status").upsert(row, on_conflict="workflow").execute()
    except Exception as e:
        print("aviso: não gravei status:", e, flush=True)
def _mapa(func, itens):
    """Roda func em cada item — em paralelo se WORKERS>1 (pool controlado, mesma mecânica do
    piloto/sugestões), senão sequencial (comportamento idêntico ao de antes). Preserva a ORDEM."""
    if WORKERS > 1 and len(itens) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            return list(ex.map(func, itens))
    return [func(x) for x in itens]
def main():
    grava_status("rodando")
    rec.preload()
    # alinha o piso dos itens "Padrão" ao mesmo valor da simulação (ex.: 17),
    # pra não recusar no apply o que a sugestão mostrou como OK.
    if MARGEM_MIN:
        try:
            rec.MARGEM_PADRAO = float(MARGEM_MIN.replace(",", "."))
            print(f"piso padrão (Padrão) = {rec.MARGEM_PADRAO}%", flush=True)
        except ValueError:
            pass
    if RETRY_ONLY:
        # cron horário: NÃO aplica a fila 'aprovada' (isso é você clicando no painel);
        # só reprocessa os que já erraram/aguardam na tabela de retry.
        fila = []
        print("MODO RETRY_ONLY — só a fila de reprocessamento (não toca em 'aprovada')", flush=True)
    else:
        q = sb.table("repricer_promo_fila").select("*").eq("status", "aprovada")
        if SELLER_FILTRO:
            q = q.eq("seller_id", SELLER_FILTRO)
        # escopo por anúncio: lista (ITEM_IDS, o painel manda os selecionados) tem prioridade;
        # senão 1 item (ITEM_ID); senão a fila inteira da conta.
        if ITENS_FILTRO:
            q = q.in_("item_id", ITENS_FILTRO)
        elif ITEM_FILTRO:
            q = q.eq("item_id", ITEM_FILTRO)
        fila = (q.execute().data) or []
        if MAX_APLICAR:
            fila = fila[:MAX_APLICAR]
    # ---- FILA DE REPROCESSAMENTO: puxa os que erraram/aguardam e já estão na hora ----
    # Só se as SUGESTÕES não estiverem rodando agora (pra não agir em cima de sugestão meio-escrita).
    ids_fila = {f["id"] for f in fila}
    if not DRY:
        if _sugestoes_rodando():
            print("sugestões RODANDO agora — pulo a fila de retry nesta rodada (evito agir em sugestão meio-escrita)", flush=True)
        else:
            retry = carregar_retry_devidos()
            # não duplica um item que já veio como 'aprovada' nesta mesma rodada
            itens_ja = {f["item_id"] for f in fila}
            retry = [r for r in retry if r["item_id"] not in itens_ja]
            if retry:
                print(f"fila de retry: {len(retry)} item(ns) na hora de reprocessar", flush=True)
                fila += retry
    escopo = (f" | itens {len(ITENS_FILTRO)} selecionados" if ITENS_FILTRO
              else (f" | item {ITEM_FILTRO}" if ITEM_FILTRO else " | FILA INTEIRA da conta"))
    print(f"{'SIMULAÇÃO (DRY_RUN)' if DRY else 'APLICAÇÃO REAL'} — {len(fila)} item(ns)"
          + (f" | conta {SELLER_FILTRO}" if SELLER_FILTRO else "") + escopo
          + (f" | {WORKERS} em paralelo" if WORKERS > 1 else ""), flush=True)
    if not fila:
        grava_status("concluido", "{}")
        return
    # resolve UM access token por conta — mesma mecânica do repricer_sugestoes.main()
    # obter_access(sb, seller_id, refresh) -> (access, sid_real, refresh)
    acessos = {}
    for seller_id, refresh in rec.contas():
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
            acessos[str(sid)] = access
        except Exception as e:
            print(f"  !! não consegui token de {seller_id}: {e}", flush=True)
    resumo = {}
    codigo_final = {}   # id -> código do desfecho final (1ª ou 2ª passada), pra alimentar a fila de retry
    res1_mem = {}       # id -> (status, resultado) da 1ª passada (evita reler o banco na 2ª passada)
    preco_ap_mem = {}   # id -> preço REALMENTE aplicado. Na promoção individual quem calcula
                        # o preço é o próprio aplicador, então preco_alvo vem nulo da fila e a
                        # 2ª passada não tinha contra o que comparar o sale_price.
    # ---- 1ª PASSADA (por item; em paralelo se WORKERS>1) ----
    def _passo1(f):
        sid = str(f["seller_id"])
        access = acessos.get(sid)
        if not access:
            gravar(f["id"], {"status": "erro", "resultado": f"sem acesso à conta {sid} (token não resolveu)"})
            return (f["id"], "erro_post", ("erro", "sem acesso à conta"))
        r = processar(f, access)
        print(f"  = {f['item_id']} [{f.get('acao', '?')}] -> {r}", flush=True)
        # captura o que ficou gravado (banco pra fila normal; patch em memória pra retry)
        if isinstance(f.get("id"), str) and str(f["id"]).startswith("retry:"):
            p = _RETRY_PATCH.get(f["id"], {})
            st1, rs1 = (p.get("status") or ""), (p.get("resultado") or "")
            preco_ap_mem[f["id"]] = p.get("preco_aplicado")
        else:
            st1, rs1 = "", ""
            try:
                atual = (sb.table("repricer_promo_fila").select("status,resultado,preco_aplicado")
                         .eq("id", f["id"]).execute().data)
                if atual:
                    st1, rs1 = (atual[0].get("status") or ""), (atual[0].get("resultado") or "")
                    preco_ap_mem[f["id"]] = atual[0].get("preco_aplicado")
            except Exception:
                pass
        return (f["id"], r, (st1, rs1))
    for fid, r, st in _mapa(_passo1, fila):
        codigo_final[fid] = r
        res1_mem[fid] = st
        resumo[r] = resumo.get(r, 0) + 1
    # ---- 2ª PASSADA (depois de mexer em TODOS os itens -> deu tempo do ML propagar) ----
    # sair: varre de novo e confere; entrar/trocar: confirma pelo PREÇO REAL (sale_price).
    alvos = [f for f in fila if f.get("acao") in ("sair", "entrar", "trocar") and acessos.get(str(f["seller_id"]))]
    if alvos:
        time.sleep(PROPAG_SLEEP)   # respiro de propagação (o resto vira retry, não bloqueia)
        def _passo2(f):
            access = acessos.get(str(f["seller_id"]))
            st1, res1 = res1_mem.get(f["id"], ("", ""))   # da 1ª passada, sem reler o banco
            f["resultado"] = res1
            # sem preco_alvo (promoção individual), a referência é o preço que ELE aplicou
            _pa = preco_ap_mem.get(f["id"])
            if _pa is not None and f.get("preco_alvo") in (None, ""):
                f["preco_alvo"] = _pa
            if st1 != "aplicada":
                return (f["id"], None)                     # 1ª passada falhou — não sobrescreve o erro
            if "já estava ATIVA" in res1:
                return (f["id"], None)                     # já-ativa não tem 2ª passada
            rr = revarrer_sair(f, access) if f.get("acao") == "sair" else confirmar_pos_entrada(f, access)
            return (f["id"], rr)
        for fid, rr in _mapa(_passo2, alvos):
            if rr:
                codigo_final[fid] = rr
                resumo["2p_" + rr] = resumo.get("2p_" + rr, 0) + 1
    # ---- BOOKKEEPING DA FILA DE RETRY: cada item entra/atualiza/sai conforme o desfecho ----
    if not DRY:
        for f in fila:
            code = codigo_final.get(f["id"])
            if not code:
                continue
            # texto do resultado pra registrar (retry = patch em memória; fila = já em f['resultado'])
            if isinstance(f.get("id"), str) and str(f["id"]).startswith("retry:"):
                txt = (_RETRY_PATCH.get(f["id"], {}) or {}).get("resultado") or f.get("resultado")
            else:
                txt = (res1_mem.get(f["id"], ("", ""))[1]) or f.get("resultado")
            registrar_retry(f, code, txt)
    print("resumo:", json.dumps(resumo, ensure_ascii=False), flush=True)
    grava_status("concluido", json.dumps(resumo, ensure_ascii=False))
if __name__ == "__main__":
    main()
