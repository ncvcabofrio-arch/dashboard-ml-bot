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
import threading
import requests
import repricer_sugestoes as rec
from datetime import datetime, timezone, timedelta
from ml_auth import obter_access
API = rec.API
sb = rec.sb
# REDE: retenta leitura do ML. Em 20/ago a rodada dos 16 morreu num
# "Connection reset by peer" no shipping_options, porque rec.get() nao retenta.
# Substituo a funcao SO neste processo (o repricer_sugestoes e compartilhado com
# piloto e reator, e nao pode ser alterado). So erro de REDE e retentado:
# resposta do ML passa direto — retentar um "nao" nao muda o "nao".
_REC_GET_ORIGINAL = rec.get


def _rec_get_com_retry(*args, **kwargs):
    _ultimo = None
    for _i in range(3):
        try:
            return _REC_GET_ORIGINAL(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            _ultimo = e
            print(f"  aviso: rede caiu lendo o ML ({type(e).__name__}), "
                  f"tentativa {_i + 1}/3", flush=True)
            time.sleep(1.0 * (_i + 1))
    raise _ultimo


rec.get = _rec_get_com_retry
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
    # o ML disse que o anúncio não é elegível -> retentar é insistir no não
    "sem_candidatura_ml": "terminal",
    # ja tem desconto individual em vigor: retentar da o mesmo nao
    "ja_tem_individual": "terminal",
    # o ML só aceita um preço que entrega menos margem que a sua: retentar dá o mesmo
    "margem_menor_que_pedida": "terminal",
    # o ML recusou tirar o individual atual: nada foi tocado, retentar nao muda
    "nao_removi_individual": "terminal",
    # decisão sua, não falha transitória: retentar igual dá o mesmo resultado
    "fora_da_faixa_doc": "terminal",
    "so_reduz": "terminal",
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
        # 423_ENTITY_LOCKED entra aqui por instrução explícita da doc:
        #   "o item está temporariamente bloqueado para realizar requisições.
        #    A solicitação pode ser tentada novamente após alguns segundos."
        # Ele saía como falha definitiva e virava 'erro_post' sem motivo visível.
        # A espera é maior que a dos outros: a doc fala em segundos, não em ms.
        if r.status_code == 423:
            time.sleep(2.0 * (i + 1))
            continue
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

    MESMA RÉGUA DO SUGERIDOR: procura primeiro entre as AVALIÁVEIS (com price e
    original_price — sem os dois, rec.avaliar devolve None). Aceitar cands[0] sem
    esse filtro era a origem dos 'sem_avaliar' sem motivo.
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
# LIMITES DE DESCONTO — da doc, não estimados. Só valem quando o ML NÃO devolve
# min/max; quando devolve, a faixa dele manda.
#   PRICE_DISCOUNT  5%–80%  (doc: erro buyer_discount_not_in_range (5, 80))
#   SELLER_CAMPAIGN 10%–80% A doc se contradiz no teto (texto 80%, erro 70%). Quem
#     desempatou foi o ML, 19/ago, MLB4264653013 (lista R$199,00): faixa devolvida
#     R$39,80 (=80% off) a R$179,10 (=10% off). Medido, não lido.
#   DEAL            herda de SELLER_CAMPAIGN. NÃO é medição, é escolha conservadora.
DESC_MIN_PCT = {"PRICE_DISCOUNT": 5.0,  "SELLER_CAMPAIGN": 10.0, "DEAL": 10.0}
DESC_MAX_PCT = {"PRICE_DISCOUNT": 80.0, "SELLER_CAMPAIGN": 80.0, "DEAL": 80.0}


def faixa_da_doc(tipo, preco_lista):
    """(preco_min, preco_max) permitidos pela DOC quando o ML não devolve faixa.

    preco_max = maior preço aceito = MENOR desconto permitido.
    preco_min = menor preço aceito = MAIOR desconto permitido.
    Devolve (None, None) quando não há preço de lista ou o tipo não tem regra."""
    tipo = (tipo or "").upper()
    if tipo not in DESC_MIN_PCT:
        return None, None
    try:
        base = float(preco_lista or 0)
    except (TypeError, ValueError):
        return None, None
    if base <= 0:
        return None, None
    pmax = round(base * (1 - DESC_MIN_PCT[tipo] / 100.0), 2)
    pmin = round(base * (1 - DESC_MAX_PCT[tipo] / 100.0), 2)
    return pmin, pmax


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
def req_put(path, access, body, tent=3):
    """PUT com a mesma política de retry do post(), inclusive o 423 da doc.
    Existe por causa do 'Modificar itens' da campanha do vendedor, que é a
    única forma de trocar o preço sem excluir a participação."""
    r = None
    for i in range(tent):
        r = requests.put(API + path,
                         headers={"Authorization": "Bearer " + access,
                                  "Content-Type": "application/json"},
                         json=body, timeout=25)
        if r.status_code == 423:
            time.sleep(2.0 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)


def req_delete(path, access, tent=3):
    r = None
    for i in range(tent):
        r = requests.delete(API + path, headers={"Authorization": "Bearer " + access}, timeout=25)
        # 423_ENTITY_LOCKED entra aqui por instrução explícita da doc:
        #   "o item está temporariamente bloqueado para realizar requisições.
        #    A solicitação pode ser tentada novamente após alguns segundos."
        # Ele saía como falha definitiva e virava 'erro_post' sem motivo visível.
        # A espera é maior que a dos outros: a doc fala em segundos, não em ms.
        if r.status_code == 423:
            time.sleep(2.0 * (i + 1))
            continue
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
# DUAS FONTES, UMA LISTA. A varredura por campanha só enxerga o que tem
# promotion_id; individual, relâmpago e oferta do dia são de NÍVEL DE ITEM
# (promo_id null) e só aparecem no endpoint por item. Sem juntar as duas, uma troca
# de individual->cofinanciada não removia o individual: o anúncio ficava nas DUAS e
# vendia pela mais barata. Todo caminho que remove promoção usa esta função.
TIPOS_NIVEL_ITEM = {"PRICE_DISCOUNT", "LIGHTNING", "DOD"}
# VARREDURA DE PARTICIPAÇÃO — mesma regra do repricer_sugestoes, só que rápida.
# Descobrir em que promoções um item está custava ~41 requisições (1 + 1 por
# campanha), duas vezes por item que troca: 10 minutos em 52 itens. Aqui muda só o
# TRANSPORTE — as perguntas saem juntas e a lista de campanhas é lida 1x por rodada.
# É tudo GET. NENHUMA escrita foi paralelizada, de propósito: irmãos sincronizados
# compartilham preço, e mexer em dois ao mesmo tempo é um sobrescrever o outro.
LEITURA_PARALELA = int(os.environ.get("LEITURA_PARALELA", "6"))
# Itens que confirmaram num preço diferente do pedido. Não é erro de aplicação —
# o desconto entrou — mas é dinheiro diferente do combinado, e antes não aparecia
# em lugar nenhum: ia junto com os "✓ CONFIRMADO" e ninguém via.
PRECO_DIVERGENTE = []
# Anúncios que saíram das campanhas e tiveram a entrada RECUSADA logo depois:
# ficaram SEM DESCONTO NENHUM. É o pior desfecho possível de uma rodada e até
# agora não aparecia no log — só num campo de texto no banco.
SEM_DESCONTO_AGORA = []
# Participações refeitas com sucesso depois de uma entrada recusada. Ficam num
# relatório à parte porque são o oposto de um problema — mas o dono precisa saber
# que o robô mexeu duas vezes no mesmo anúncio na mesma rodada.
REENTRADAS = []
# Itens em que o ML NÃO permite o preço pedido: max_discounted_price do anúncio é
# menor que o alvo da tela, e _clamp_preco cortou antes do envio. A causa aqui é
# MEDIDA (veio do próprio candidato), então fica numa lista separada — misturar
# isso com divergência de verdade era o que produzia diagnóstico inventado.
TETO_DO_ML = []
_CAMPANHAS_CACHE = {}                 # seller_id -> campanhas (uma leitura por rodada)
_CAMPANHAS_LOCK = threading.Lock()


def campanhas_do_vendedor(seller_id, access):
    """Campanhas do vendedor, lidas UMA vez por rodada.
    A lista não muda em dez minutos; relê-la a cada verificação era uma
    chamada jogada fora por item."""
    chave = str(seller_id or "")
    with _CAMPANHAS_LOCK:
        if chave in _CAMPANHAS_CACHE:
            return _CAMPANHAS_CACHE[chave]
    lista = rec.promocoes_do_vendedor(seller_id, access) or []
    with _CAMPANHAS_LOCK:
        _CAMPANHAS_CACHE.setdefault(chave, lista)
        return _CAMPANHAS_CACHE[chave]


def participacoes_ativas_rapido(item_id, seller_id, access):
    """Idêntico ao rec.participacoes_ativas — só com as consultas por campanha
    em paralelo. Retorna a mesma lista de dicts, na mesma ordem das campanhas."""
    campanhas = [pr for pr in campanhas_do_vendedor(seller_id, access)
                 if (pr.get("status") or "").lower() in ("started", "pending")
                 and pr.get("id") and pr.get("type")]
    if not campanhas:
        return []

    def _uma(pr):
        pid, ptipo = pr.get("id"), (pr.get("type") or "")
        st, d = rec.get(f"/seller-promotions/promotions/{pid}/items"
                        f"?promotion_type={ptipo}&item_id={item_id}&app_version=v2", access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        for it in res:
            if str(it.get("id")) != str(item_id):
                continue
            sti = (it.get("status") or "").lower()
            if sti not in ("started", "pending"):
                continue
            return {"promotion_id": pid, "type": ptipo.upper(),
                    "offer_id": it.get("offer_id") or it.get("ref_id"),
                    "name": pr.get("name"), "status": sti}
        return None

    n = max(1, min(LEITURA_PARALELA, len(campanhas)))
    if n == 1:
        achados = [_uma(pr) for pr in campanhas]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=n) as ex:
            achados = list(ex.map(_uma, campanhas))
    out, vistos = [], set()
    for a in achados:
        if not a:
            continue
        chave = (a["promotion_id"], a["type"])
        if chave in vistos:
            continue
        vistos.add(chave)
        out.append(a)
    return out


def candidatas_por_campanha(item_id, seller_id, access):
    """CANDIDATURAS do item varrendo as CAMPANHAS DA CONTA — a segunda fonte.

    POR QUE: o /seller-promotions/items/{id} devolveu [] para o MLB4120093236
    enquanto a varredura achava o mesmo item como 'candidate' em DUAS campanhas.
    O endpoint por item também esconde CANDIDATURAS, não só participações ativas —
    e o aplicador concluía "expirou" estando cego.

    Devolve no MESMO formato do ofertas_do_item: 'id' é o id da PROMOÇÃO (no corpo
    cru desse endpoint 'id' é o do item, e trocar faria achar_candidato nunca casar).
    Custa ~40 chamadas em paralelo, e só roda quando a porta barata volta vazia.
    """
    campanhas = [pr for pr in campanhas_do_vendedor(seller_id, access)
                 if (pr.get("status") or "").lower() in ("started", "pending")
                 and pr.get("id") and pr.get("type")]
    if not campanhas:
        return []

    def _uma(pr):
        pid, ptipo = pr.get("id"), (pr.get("type") or "")
        st, d = rec.get(f"/seller-promotions/promotions/{pid}/items"
                        f"?promotion_type={ptipo}&item_id={item_id}&app_version=v2", access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        for it in res:
            if str(it.get("id")) != str(item_id):
                continue
            if (it.get("status") or "").lower() != "candidate":
                continue
            o = dict(it)
            o["id"] = pid                      # id da PROMOÇÃO, não o do item
            o["type"] = (ptipo or "").upper()
            o["status"] = "candidate"
            o["name"] = pr.get("name")
            if not o.get("ref_id"):
                o["ref_id"] = it.get("offer_id")
            # percentuais às vezes vêm só na campanha, não no item
            for k in ("meli_percentage", "seller_percentage"):
                if o.get(k) is None and pr.get(k) is not None:
                    o[k] = pr.get(k)
            o["_via"] = "campanhas"
            return o
        return None

    n = max(1, min(LEITURA_PARALELA, len(campanhas)))
    if n == 1:
        achados = [_uma(pr) for pr in campanhas]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=n) as ex:
            achados = list(ex.map(_uma, campanhas))
    return [a for a in achados if a]


def ofertas_das_duas_fontes(iid, seller_id, access):
    """Ofertas do item pela porta barata; se vier vazia, pela varredura das campanhas.
    Devolve (ofertas, origem) — 'origem' entra no log pra você saber por onde veio."""
    ofertas = rec.ofertas_do_item(iid, access) or []
    if ofertas:
        return ofertas, "item"
    achadas = candidatas_por_campanha(iid, seller_id, access)
    if achadas:
        nomes = ", ".join(f"{(o.get('name') or o.get('type'))}[{o.get('type')}]" for o in achadas)
        print(f"  ~ {iid}: endpoint por item devolveu VAZIO; varrendo campanhas achei "
              f"{len(achadas)} candidatura(s): {nomes}", flush=True)
        return achadas, "campanhas"
    return [], "nenhuma"


def participacoes_completas(iid, seller_id, access, ofertas=None):
    """Todas as promoções ATIVAS do item, das duas fontes, deduplicadas.
    As de nível de item entram com nivel_item=True e promotion_id possivelmente None —
    o remover_participacao já trata esses três tipos pelo promotion_type, que é a forma
    que a doc do ML manda para eles."""
    achadas = list(participacoes_ativas_rapido(iid, seller_id, access) or [])
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
def sair_das_outras(iid, seller_id, access, manter_pid=None, manter_tipo=None,
                    saiu_dicts=None):
    """SAI de TODAS as promoções em que o item está ATIVO, exceto a que queremos manter.
    Usa o caminho CONFIÁVEL da doc (rec.participacoes_ativas): users/{seller} cruzado com
    promotions/{id}/items?item_id=... — porque /seller-promotions/items/{id} NÃO traz as
    ativas de campanha cofinanciada/marketplace (só candidatas). Sai por TIPO, uma a uma.
    Retorna (saiu[], falhou[], restantes[]).

    saiu_dicts: se você passar uma lista, ela recebe os DICIONÁRIOS das participações
    removidas (não só os rótulos). É o que torna possível desfazer a saída quando a
    entrada seguinte é recusada — sem isso, o que foi removido vira texto e some."""
    ativas = participacoes_completas(iid, seller_id, access)
    saiu, falhou, restantes = [], [], []
    for p in ativas:
        if manter_pid and p.get("promotion_id") == manter_pid:
            continue
        if manter_pid is None and manter_tipo and (p.get("type") or "").upper() == manter_tipo:
            continue  # sem id da nova (ex.: relâmpago): não sai de outra do mesmo tipo
        scd, body = remover_participacao(iid, p, access)
        rot = f"{p.get('name') or p.get('type')}({p.get('type')}:{scd})"
        if scd in (200, 201):
            saiu.append(rot)
            if saiu_dicts is not None:
                saiu_dicts.append(p)
        else:
            falhou.append(rot)
            restantes.append(p)      # só o que o ML RECUSOU remover
    # ANTES havia aqui uma segunda varredura completa (~41 chamadas) só pra montar
    # 'restantes'. Os DOIS lugares que chamam esta função descartam esse valor — e um
    # deles explica por quê, em comentário: a remoção no ML é assíncrona, então relistar
    # agora dá falso "ainda ativas". Quem confere de verdade é a 2ª passada do main(),
    # depois do tempo de propagação. Então 'restantes' passa a ser o que de fato ficou
    # para trás: as promoções cujo DELETE foi RECUSADO. Mais honesto e de graça.
    return saiu, falhou, restantes
# Tipos cuja reentrada NÃO dá para reconstituir: exigem estoque/datas que só
# existiam no momento em que a promoção foi montada.
TIPOS_SEM_REENTRADA = {"LIGHTNING", "DOD"}

# REMOVER O INDIVIDUAL EM VIGOR PARA CRIAR OUTRO: DESLIGADO.
# Medido em 19/ago: MLB4160427809 teve o desconto removido e o ML recusou o novo
# IGUAL; as 6 tentativas de restaurar voltaram 400. Saldo: 6 anúncios perderam o
# que tinham e não ganharam nada. Religue com TROCAR_INDIVIDUAL=1 quando houver
# medição que justifique.
TROCAR_INDIVIDUAL = (os.environ.get("TROCAR_INDIVIDUAL", "0").strip() == "1")
# Quanto a margem pode ficar ABAIXO da pedida sem o robô recusar, em pontos.
# 0,25 saiu da rodada de 125: lá a trava barrou 5, e os números se separam sozinhos —
# graves (16,04%->-10,00% e 18%->13,83%) contra marginais (18% contra 17,81/17,81/17,94).
# 0,25 passa os marginais e mantém os graves barrados. MARGEM_TOL_PP=0 deixa estrito.
try:
    MARGEM_TOL_PP = abs(float(os.environ.get("MARGEM_TOL_PP", "0.25").replace(",", ".")))
except (TypeError, ValueError):
    MARGEM_TOL_PP = 0.25


def individual_em_vigor(ofertas):
    """O desconto individual que JÁ ESTÁ VALENDO no anúncio, ou None.

    Usa exatamente o mesmo critério de 'ativa' que participacoes_completas
    (rec.eh_ativa) — de propósito. Um segundo critério aqui significaria que o
    robô considera o mesmo desconto ativo num lugar e inativo no outro."""
    for o in (ofertas or []):
        if not isinstance(o, dict):
            continue
        if (o.get("type") or "").upper() != "PRICE_DISCOUNT":
            continue
        try:
            if not rec.eh_ativa(o):
                continue
        except Exception:
            continue
        return o
    return None


def esperar_saida_individual(iid, access, tentativas=9, espera=2.0):
    """Espera o DELETE do desconto individual TERMINAR de verdade.

    A doc lista 'restore_requested' como um status do item: "processo pendente de
    remoção do desconto". O 200 do DELETE só quer dizer que o pedido foi aceito.
    Postar o desconto novo antes desse processo fechar foi o que produziu seis
    "No candidates found" seguidos em 19/ago.

    Considera terminado quando, no /seller-promotions/items/{id}, não sobra
    nenhum PRICE_DISCOUNT em started/pending/sync_requested/restore_requested —
    ou quando aparece um PRICE_DISCOUNT 'candidate', que é o lugar vago.

    Devolve (pronto: bool, quantas_esperas: int, motivo: str). Nunca levanta:
    se a consulta falhar, devolve pronto=False e o robô decide sem chutar.
    """
    # SO A CANDIDATURA E PROVA. "Nenhum ocupando" e ambiguo (pode ser "terminou" ou
    # "ainda nao apareceu"): em 20/ago o MLB6762374998 saiu da espera em 6s por essa
    # leitura fraca e levou "No candidates found" no POST e na restauracao. Agora so a
    # presenca de um PRICE_DISCOUNT 'candidate' encerra a espera.
    _OCUPADO = {"started", "pending", "sync_requested", "restore_requested"}
    _vazio_desde = None
    for n in range(tentativas):
        time.sleep(espera)
        try:
            st, d = rec.get(f"/seller-promotions/items/{iid}?app_version=v2", access)
        except Exception as e:
            return False, n + 1, f"não consegui consultar o item ({e})"
        if not isinstance(d, list):
            return False, n + 1, f"resposta inesperada do ML (HTTP {st})"
        pds = [o for o in d if isinstance(o, dict)
               and (o.get("type") or "").upper() == "PRICE_DISCOUNT"]
        if any((o.get("status") or "").lower() == "candidate" for o in pds):
            return True, n + 1, "candidatura de desconto individual disponível"
        if not any((o.get("status") or "").lower() in _OCUPADO for o in pds):
            # lugar vago, mas o ML ainda nao reabriu a candidatura. Isso NAO
            # basta: espera mais, e so aceita se persistir ate o fim.
            if _vazio_desde is None:
                _vazio_desde = n + 1
    if _vazio_desde is not None:
        return True, tentativas, (f"nenhum desconto individual ocupando o item (vago desde a "
                                  f"{_vazio_desde}ª consulta, mas o ML não reabriu candidatura)")
    return False, tentativas, ("o desconto antigo ainda aparece ocupando o item depois de "
                              f"{tentativas * espera:.0f}s")


def restaurar_individual(iid, access, oferta):
    """Recria o desconto individual removido, com o MESMO preço e as MESMAS datas.

    Só é chamada quando o DELETE do antigo devolveu 200/201: existe prova de que
    havia algo ali. Sem essa prova o robô não recria nada — inventar um desconto
    que não existia seria pior que o problema que estamos consertando.

    Se o ML recusar por causa da data de início (que ficou no passado), tenta uma
    segunda vez com início hoje e o MESMO fim. Retorna (ok, descricao)."""
    preco = None
    try:
        preco = rec.preco_oferta(oferta)
    except Exception:
        preco = None
    if preco in (None, "") or float(preco) <= 0:
        return False, "não sei o preço que o desconto antigo tinha"
    # A restauracao levava "No candidates found" pelo mesmo motivo do POST novo:
    # recriar antes de o ML reabrir a candidatura. Espera primeiro.
    esperar_saida_individual(iid, access)
    corpo = {"deal_price": round(float(preco), 2), "promotion_type": "PRICE_DISCOUNT"}
    ini, fim = oferta.get("start_date"), oferta.get("finish_date")
    if ini:
        corpo["start_date"] = ini
    if fim:
        corpo["finish_date"] = fim
    try:
        scd, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
    except Exception as e:
        return False, f"erro de rede ao restaurar: {e}"
    if scd in (200, 201):
        return True, f"R${float(preco):.2f} (datas originais)"
    # 2ª tentativa: a data de início já passou. Recomeça hoje, mantendo o fim.
    if ini:
        hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d")
        corpo["start_date"] = hoje + "T00:00:00"
        try:
            scd, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
        except Exception as e:
            return False, f"erro de rede na 2ª tentativa: {e}"
        if scd in (200, 201):
            return True, f"R${float(preco):.2f} (início refeito para hoje, fim mantido)"
    # O corpo do 400 é a única pista de POR QUE o ML recusa recriar um desconto que
    # ele mesmo tinha. Sem ele sobra "(400)", que não permite consertar nada.
    return False, (f"ML recusou a restauração ({scd}) — preço antigo era R${float(preco):.2f} "
                   f"|| ML disse: {json.dumps(resp, ensure_ascii=False)[:220]}")


def reentrar_nas_campanhas(iid, seller_id, access, perdidas):
    """DESFAZ a saída: refaz as participações removidas quando a entrada no desconto
    individual foi recusada logo depois. Objetivo é devolver o anúncio ao estado
    anterior — não melhorá-lo.

    'perdidas' são os dicionários que sair_das_outras removeu com 200/201.
    Retorna (voltou[], nao_voltou[]) com rótulos legíveis.

    O preço não vem de lugar nenhum guardado: participacoes_completas não registra
    preço. Para os tipos em que o preço é do ML isso não importa (o corpo é só
    id+tipo). Para DEAL/SELLER_CAMPAIGN o robô usa o SUGERIDO do ML e, na falta
    dele, o TETO da faixa — o menor desconto possível, que é a escolha de menor
    risco de margem. Qual número foi usado sai escrito no log."""
    voltou, nao_voltou = [], []
    if not perdidas:
        return voltou, nao_voltou
    # relê as candidaturas AGORA: depois do DELETE o ML costuma reabrir a candidatura
    # da campanha de onde o item saiu, e é dela que sai a faixa de preço válida.
    try:
        ofertas, _org = ofertas_das_duas_fontes(iid, seller_id, access)
    except Exception as e:
        for p in perdidas:
            nao_voltou.append(f"{p.get('name') or p.get('type')}(não consegui reler as ofertas: {e})")
        return voltou, nao_voltou
    ofertas = ofertas or []
    for p in perdidas:
        t = (p.get("type") or "").upper()
        rot = p.get("name") or t
        if t in TIPOS_SEM_REENTRADA:
            nao_voltou.append(f"{rot}({t}: exige estoque/data — refaça no ML)")
            continue
        # acha a candidatura correspondente: por id quando existe, por tipo nos
        # casos de nível de item (onde promotion_id costuma vir nulo).
        alvo = None
        for o in ofertas:
            if not isinstance(o, dict):
                continue
            if (o.get("type") or "").upper() != t:
                continue
            if p.get("promotion_id") and str(o.get("id") or "") != str(p.get("promotion_id")):
                continue
            alvo = o
            break
        if alvo is None:
            nao_voltou.append(f"{rot}({t}: o ML não oferece mais candidatura)")
            continue
        _preco, _fonte = None, ""
        if t in ("DEAL", "SELLER_CAMPAIGN"):
            # NÃO REPÕE. (1) O preço anterior não existe em lugar nenhum —
            # participacoes_completas guarda id, tipo, offer_id e nome, nunca preço.
            # (2) Com a oferta ativa a doc só permite REDUZIR ("New deal_price must be
            # lower than current deal_price"): repor pelo sugerido ou pelo teto é
            # recusado, ou aceito deixando MAIS BARATO. Nos dois casos eu mexeria no
            # seu preço sem ordem sua.
            nao_voltou.append(f"{rot}({t}: quem define o preço aqui é você e eu não guardei "
                              f"o anterior — refaça no ML para não trocar seu preço por um "
                              f"que você não escolheu)")
            continue
        corpo = corpo_post(t, alvo, _preco)
        if not corpo:
            nao_voltou.append(f"{rot}({t}: não sei montar o corpo desse tipo)")
            continue
        try:
            scd, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
        except Exception as e:
            nao_voltou.append(f"{rot}({t}: erro de rede {e})")
            continue
        if scd in (200, 201):
            det = f" a R${float(_preco):.2f} ({_fonte})" if _preco not in (None, "") else ""
            voltou.append(f"{rot}({t}){det}")
        else:
            nao_voltou.append(f"{rot}({t}: ML recusou a volta, {scd} — "
                              f"{json.dumps(resp, ensure_ascii=False)[:160]})")
    return voltou, nao_voltou


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
        # PREÇO DIFERENTE DO ALVO: a regra aceita qualquer preço ABAIXO do alvo, por
        # qualquer distância — foi escrita pra tolerar arredondamento. Foi assim que o
        # MLB7393879448 fechou em R$1.722,21 (o preço do IRMÃO) com alvo R$1.809,66 e
        # saiu carimbado com ✓. Continua sendo tratado como aplicado, porque o desconto
        # existe de verdade; mas agora aparece, em vez de sumir no meio dos acertos.
        try:
            _pedido = fila.get("_preco_pedido")
            if pv is not None and alvo not in (None, "") and abs(float(pv) - float(alvo)) > float(alvo) * 0.01:
                # O ML entregou preço diferente do que ENVIAMOS. Isto sim é inexplicado.
                PRECO_DIVERGENTE.append((iid, float(alvo), float(pv)))
                nota = (f"⚠️ O ML APLICOU PREÇO DIFERENTE DO QUE ENVIEI — cliente paga "
                        f"R${float(pv):.2f}, enviei R${float(alvo):.2f} "
                        f"(diferença R${float(alvo) - float(pv):.2f}). NÃO MEDI A CAUSA. "
                        f"Hipótese a conferir: preço propagado de anúncio irmão sincronizado.")
            elif _pedido is not None and float(alvo) < float(_pedido) - 0.005:
                # TETO DE VERDADE: o preço enviado ficou MENOR que o pedido, ou seja, o
                # corte da faixa (max_discounted_price) realmente apertou. Essa causa foi
                # medida no envio.
                TETO_DO_ML.append((iid, float(_pedido), float(alvo)))
                nota += (f" || TETO DO ML: você pediu R${float(_pedido):.2f}, mas o ML só "
                         f"aceita desconto até R${float(alvo):.2f} neste anúncio "
                         f"(max_discounted_price) — enviei o teto")
            elif _pedido is not None:
                # Preço final MAIOR que o pedido. Teto do ML não faz isso — ele só pode
                # te fazer cobrar menos. A mensagem antiga carimbava "teto" aqui e saía
                # com número negativo ("R$-4,60 a menos do que você queria cobrar"), que
                # lido em voz alta é uma contradição. A causa mais provável é o preço vir
                # do anúncio irmão sincronizado — e o log acima já mostra quando é isso.
                # Aqui eu digo o que aconteceu e NÃO invento a causa.
                nota += (f" || PREÇO MAIOR QUE O PEDIDO: você pediu R${float(_pedido):.2f} "
                         f"e entrou R${float(alvo):.2f} (R${float(alvo) - float(_pedido):.2f} "
                         f"a mais). NÃO É teto do ML — teto só faz cobrar menos. "
                         f"NÃO MEDI A CAUSA.")
        except (TypeError, ValueError):
            pass
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
def _faixa_individual(iid, seller_id):
    """A FAIXA CRÍVEL do desconto individual, lida da sugestão guardada no banco.

    O individual não é candidatura de campanha e o /seller-promotions/items/{id}
    às vezes devolve []; sem candidato vivo o aplicador parava em 'sem_candidato'
    (18 promoções travaram assim). Mas a faixa nunca se perdeu: o repricer_sugestoes
    já a grava em 'ofertas' como preco_min/preco_max/preco_sug.

    Ignora o 'escolha_manual' de propósito: aquele sinalizador protege a DECISÃO, e
    faixa não é decisão, é MEDIÇÃO. Devolve o dict da oferta individual, ou None.
    """
    try:
        rows = (sb.table("repricer_sugestoes")
                .select("ofertas,preco_atual,criado_em")
                .eq("seller_id", str(seller_id)).eq("item_id", iid)
                .order("criado_em", desc=True).limit(1).execute().data) or []
    except Exception as e:
        print(f"  aviso: não li a faixa guardada de {iid}: {e}", flush=True)
        return None
    if not rows:
        return None
    lista = rows[0].get("ofertas")
    if isinstance(lista, str):
        try:
            lista = json.loads(lista)
        except (ValueError, TypeError):
            return None
    for o in (lista or []):
        if isinstance(o, dict) and (o.get("individual") is True
                                    or (o.get("tipo") or "").upper() == "PRICE_DISCOUNT"):
            return o
    return None


def candidato_individual_da_sugestao(iid, seller_id, preco_lista):
    """Monta o 'candidato' do desconto individual a partir da faixa guardada.

    Traduz os nomes do painel para os nomes que o resto do aplicador espera
    (rec.avaliar, _clamp_preco, corpo_post). Nenhum valor é inventado: todos
    vieram do ML quando a sugestão rodou.

    SEM FAIXA TAMBÉM VALE: o individual é criado pelo VENDEDOR (o POST é só
    promotion_type + deal_price + datas). A faixa do ML é DICA de credibilidade, não
    permissão — tratá-la como impedimento travava 305 anúncios por um dado opcional.
    Sem ela o teto é o preço de lista, e quem julga é o ML no POST. Em nenhum caso o
    preço é inventado: sai da sua margem, com a comissão real da API.
    """
    fx = _faixa_individual(iid, seller_id) or {}
    sem_faixa = (fx.get("sem_faixa") is True) or (fx.get("preco_min") is None
                                                 and fx.get("preco_max") is None)
    if sem_faixa:
        # ANTES: teto = preço de lista, piso = None (virava R$0,50 lá na frente).
        # Os dois são impossíveis pela doc: teto no preço de lista significa 0% de
        # desconto, e a doc exige no mínimo 5%; piso em R$0,50 significa ~99%, e o
        # máximo é 80%. O log mostrava as duas coisas — "desconto 0%" numa ponta e
        # margem de -7400% na outra. Agora a faixa sintética obedece a regra escrita.
        pmin, pmax = faixa_da_doc("PRICE_DISCOUNT", preco_lista)
        if pmax is None:
            pmax = preco_lista
    else:
        pmin = fx.get("preco_min")
        pmax = fx.get("preco_max")
    return {"id": fx.get("promocao_id"), "ref_id": fx.get("promocao_ref_id"),
            "type": "PRICE_DISCOUNT", "status": "candidate",
            "name": fx.get("nome") or "Desconto individual",
            "price": 0,                                   # é o ML dizendo "escolhe você"
            "original_price": preco_lista,
            "min_discounted_price": pmin,
            "max_discounted_price": pmax,
            "suggested_discounted_price": fx.get("preco_sug"),
            "meli_percentage": fx.get("rebate"),
            "seller_percentage": fx.get("desconto_vendedor"),
            "_via": ("sem_faixa" if sem_faixa else "faixa_guardada"),
            "_sem_faixa": sem_faixa}


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
    # DUAS FONTES: o endpoint por item mente por omissao (devolveu [] para um item
    # que estava candidate em duas campanhas). Se ele vier vazio, varremos as
    # campanhas antes de concluir qualquer coisa.
    ofertas, _origem_ofertas = ofertas_das_duas_fontes(
        iid, str(fila.get("seller_id") or ""), access)
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
        # ANTES: status 'erro' e o texto "(candidato expirou?)" — um palpite com
        # interrogação, escrito no banco e lido depois como se fosse diagnóstico.
        # Ninguém tinha verificado expiração nenhuma. Agora só se escreve isto
        # depois de as DUAS fontes voltarem vazias, e o texto diz o que foi feito.
        gravar(fila["id"], {"status": "expirado",
            "resultado": "nenhuma candidatura agora — nem no endpoint por item, "
                         "nem varrendo as campanhas da conta"})
        return "sem_oferta"
    # snapshot das ATIVAS de agora. 'trocar' vai sair de todas elas e ficar só na sugerida.
    # 'entrar' não mexe nas outras. Só entra em quem está 'candidate' agora (a sugerida nunca é ativa).
    antigas = [o for o in ofertas if rec.eh_ativa(o)]
    cand = achar_candidato(ofertas, fila)
    # DESCONTO INDIVIDUAL SEM CANDIDATO VIVO: não é impedimento real. Ele não mora em
    # campanha nenhuma (a varredura não acha) e o endpoint por item às vezes vem vazio.
    # A faixa que o ML deu está guardada na sugestão — usamos de lá. O preço continua
    # saindo da SUA margem; a faixa só serve pra não pedir um desconto que o ML recusaria.
    if not cand and tipo == "PRICE_DISCOUNT":
        _stp, _itp = rec.get(f"/items/{iid}", access)
        _plista = None
        if isinstance(_itp, dict):
            _plista = _itp.get("base_price") or _itp.get("price")
        if _plista:
            cand = candidato_individual_da_sugestao(iid, str(fila.get("seller_id") or ""), float(_plista))
            if cand and cand.get("_sem_faixa"):
                print(f"  ~ {iid}: desconto individual sem faixa do ML — preço sai da sua "
                      f"margem-alvo; o ML julga a credibilidade no envio", flush=True)
            elif cand:
                print(f"  ~ {iid}: sem candidato vivo de desconto individual — usando a faixa "
                      f"guardada na sugestão (R${cand.get('min_discounted_price')}"
                      f"–R${cand.get('max_discounted_price')})", flush=True)
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
        if tipo == "PRICE_DISCOUNT":
            gravar(fila["id"], {"status": "expirado",
                "resultado": "desconto individual sem faixa de preço: o ML não devolveu candidatura "
                             "agora e a sugestão guardada não tem a faixa crível. Rode a sugestão de novo."})
            return "sem_candidato"
        gravar(fila["id"], {"status": "expirado",
            "resultado": "a candidatura aprovada não está mais disponível — rode a sugestão de novo"})
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
    # O SKU mora em TRÊS lugares (seller_custom_field, atributo SELLER_SKU, variações) e
    # rec.sku_do_item() resolve os três. Lendo só os dois primeiros, um anúncio com SKU só
    # no atributo (MLB3967924417 / MLPA003) chegava como "sem custo" tendo custo.
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
    # ---- PREÇO LIVRE (PRICE_DISCOUNT, DEAL, SELLER_CAMPAIGN): o preço é SEU ----
    # O candidato chega com price=0: não é dado faltando, é o ML dizendo "escolhe você".
    # O que ele manda é a FAIXA CRÍVEL em min/max_discounted_price (medido: min = 20% do
    # original; max = 5% a 10% de desconto, calculado item a item). Quando não manda nem
    # faixa (ex.: ARCOS BASE-08-26, price:0/min:null/max:null), o teto vira o preço de
    # lista — desconto acima do cheio não existe — e quem julga credibilidade é o ML no
    # POST. LIGHTNING/DOD ficam fora: têm bloco próprio e dependem da faixa.
    #
    # O PREÇO SAI DA MARGEM que você escolheu (ver a condição abaixo). Sem escolha sua o
    # aplicador PARA em 'sem_preco_alvo': cair no piso do grupo seria aplicar uma margem
    # que você não pediu.
    if (tipo in ("DEAL", "SELLER_CAMPAIGN")
            and not rec.preco_oferta(cand)
            and cand.get("min_discounted_price") is None
            and cand.get("max_discounted_price") is None):
        _teto_lista = cand.get("original_price") or it.get("price")
        try:
            _teto_lista = float(_teto_lista or 0)
        except (TypeError, ValueError):
            _teto_lista = 0
        _pmin_doc, _pmax_doc = faixa_da_doc(tipo, _teto_lista)
        if _pmax_doc is not None:
            cand = dict(cand)
            cand["max_discounted_price"] = _pmax_doc
            cand["min_discounted_price"] = _pmin_doc
            cand["original_price"] = cand.get("original_price") or _teto_lista
            cand["_sem_faixa"] = True
            print(f"  ~ {iid}: {tipo} de preço livre e o ML não devolveu faixa — usando os "
                  f"limites da DOC ({DESC_MIN_PCT[tipo.upper()]:.0f}% a "
                  f"{DESC_MAX_PCT[tipo.upper()]:.0f}% de desconto sobre R${_teto_lista:.2f}) "
                  f"= R${_pmin_doc:.2f}–R${_pmax_doc:.2f}; o preço sai da sua margem-alvo",
                  flush=True)
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
        # A MARGEM MANDA. O preço só decide quando você NÃO escolheu margem.
        #
        # Era o contrário, e custou caro: o painel estima o preço a partir de uma foto
        # do banco, grava em preco_alvo, e o aplicador obedecia sem recalcular. Cinco
        # anúncios premium saíram com a comissão do clássico embutida no preço (desvio
        # de exatos 5,00 pontos), e um deles aplicou em setembro um preço calculado em
        # julho — a fila é upsert por item, então a linha fica parada lá.
        #
        # O caminho da margem não estima nada: busca binária sobre rec.avaliar, que
        # consulta a comissão REAL na API do ML. Se a margem não couber, ele para com
        # 'margem_inalcancavel' em vez de aplicar menos do que você pediu.
        #
        # Vale para os quatro tipos de preço livre: individual, DEAL, SELLER_CAMPAIGN, DOD.
        # O botão "melhor margem possível" manda preco_alvo SEM margem_alvo_manual, então
        # continua caindo aqui — e ali o preço é do aplicador, medido, não estimado.
        if _marg_tela in (None, "") and _preco_tela not in (None, ""):
            # caminho 1: PREÇO fixado e sem margem-alvo. Ele manda; só encaixamos na faixa.
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
            # A faixa só é "do ML" quando veio dele. No caminho sem faixa, _mn é o
            # limite da busca binária e _mx é o preço de lista — atribuir isso ao
            # Mercado Livre seria inventar uma fonte, que é exatamente o defeito
            # que este arquivo passou o dia consertando.
            _fx_fonte = ("faixa usada (sem faixa do ML; teto = preço de lista)"
                         if cand.get("_sem_faixa") else "faixa do ML")
            print(f"  ~ {iid}: {tipo} por {_origem} | {_fx_fonte} {_mn:.2f}–{_mx:.2f}", flush=True)
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
        # DUAS casas, não uma. A comparação usa a margem cheia (17,96) e a impressão
        # arredondava para 18,0 — saía "margem 18.0% abaixo do piso 18%", que lido em voz
        # alta é uma contradição. A decisão sempre esteve certa; era o texto que mentia.
        aviso_piso = (f"⚠️ ABAIXO DO PISO — margem {ev['margem']:.2f}% < piso {piso:.0f}% "
                      f"(aplicado por decisão manual no Acelerar) || ")
        print(f"  ⚠ {iid}: margem {ev['margem']:.2f}% abaixo do piso {piso:.0f}% "
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
    # ================= TRAVA DA MARGEM PEDIDA =================
    # O 'ev' acima foi calculado com o preço que a gente QUERIA. O corpo_post pode
    # ter cortado esse preço na faixa do anúncio (_clamp_preco) — e preço menor é
    # margem menor. Antes desta trava o robô mandava o teto do ML assim mesmo e
    # gravava a margem antiga: o MLB5344013854 entrou a ~2,2% registrado como
    # 11,17%. Aqui a margem é refeita NO PREÇO QUE VAI SER ENVIADO.
    _p_env = None
    try:
        if corpo.get("deal_price") is not None:
            _p_env = float(corpo["deal_price"])
    except (TypeError, ValueError):
        _p_env = None
    if _p_env is None:
        _p_env = rec.preco_oferta(cand)          # tipos em que o preço é do ML
    _mg_ped = fila.get("margem_alvo_manual")
    if _mg_ped in (None, ""):
        _mg_ped = fila.get("margem_prevista")    # a margem que a tela mostrou
    try:
        _mg_ped = float(_mg_ped) if _mg_ped not in (None, "") else None
    except (TypeError, ValueError):
        _mg_ped = None
    if _p_env is not None and _mg_ped is not None:
        _ev_env = ev
        try:
            if abs(float(_p_env) - float(ev["pb"])) > 0.005:
                _c_env = dict(cand)
                _c_env["price"] = round(float(_p_env), 2)
                _ev_env = rec.avaliar(_c_env, cat, ltid, access, frete, custo) or ev
        except (TypeError, ValueError, KeyError):
            _ev_env = ev
        _mg_real = _ev_env.get("margem")
        if _mg_real is not None and float(_mg_real) < float(_mg_ped) - MARGEM_TOL_PP:
            gravar(fila["id"], {"status": "erro", "resultado": (
                f"NÃO APLIQUEI. Você pediu {float(_mg_ped):.2f}% e o preço que o ML aceita "
                f"(R${float(_p_env):.2f}) entrega {float(_mg_real):.2f}% — margem MENOR que a sua. "
                f"O preço que daria a sua margem era R${float(ev['pb']):.2f}, e o ML não aceita "
                f"esse valor neste anúncio. Nada foi tocado.")})
            print(f"  ! margem_menor_que_pedida {iid}: pedida {float(_mg_ped):.2f}% | sairia "
                  f"{float(_mg_real):.2f}% a R${float(_p_env):.2f} — NÃO apliquei", flush=True)
            return "margem_menor_que_pedida"
        # margem confirmada no preço real: é ELA que vai para o registro
        ev = _ev_env
    # ===========================================================
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
    # ---- APLICAÇÃO REAL ---- ORDEM, em duas travas:
    #   1) ENTRA na nova; se o ML recusar, nada é tocado.
    #   2) SÓ SAI das antigas depois que o sale_price confirmar a nova (2ª passada).
    # O ML às vezes aceita (201) e demora minutos pra refletir no preço ('ativando'). Sair
    # já no 201 abriria uma janela com a nova ainda não valendo e a antiga já fora.
    aviso = ""
    # ---- EXCEÇÃO DE ORDEM: desconto individual em item com campanha ativa ----
    # A regra geral é ENTRAR primeiro e sair da antiga só depois de confirmar. Para
    # PRICE_DISCOUNT inverte, por regra da ML: "Se ao iniciar o desconto o item estiver
    # participando de um DEAL, o desconto não será aplicado até que o DEAL associado seja
    # finalizado" — entrando primeiro ele ficaria dormente para sempre.
    # CUSTO ACEITO: entre o DELETE e o POST o anúncio fica segundos sem desconto; se o
    # POST for recusado aí, fica SEM até você agir. Por isso esse caso é gritado no log.
    saiu_antes = ""
    _saiu_nomes = []          # nomes das campanhas de onde ele REALMENTE saiu (DELETE 200)
    _saiu_dicts = []          # as participações inteiras, pra poder desfazer a saída
    _ind_antigo = None        # o desconto individual que estava em vigor e foi removido
    # ESCOLHA SUA = SAI DE TODAS E ENTRA NA QUE VOCE PEDIU.
    # Sem isto, entrar numa campanha com individual ativo cria oferta DORMENTE: aceita
    # (201) e sem efeito no preco (MLB2977506201, SMART a R$1.400 aceita e cliente
    # pagando R$1.617,08). Vale para QUALQUER alvo, inclusive um novo individual.
    # So quando a escolha e sua: piloto e reator seguem a ordem antiga (entra, depois sai).
    _sai_de_todas = (acao == "trocar" and bool(fila.get("escolha_manual")))
    if _sai_de_todas:
        # PRE-VOO DA FAIXA — so quando o alvo e o desconto individual.
        # A doc do ML exige desconto entre 5% e 80%. Se o preco escolhido nao
        # couber, o POST e recusa garantida: nao vale destruir o que existe.
        if tipo == "PRICE_DISCOUNT":
            _lista_sd = None
            try:
                _lista_sd = float(cand.get("original_price") or it.get("price") or 0)
            except (TypeError, ValueError):
                _lista_sd = 0
            _dmin_sd, _dmax_sd = faixa_da_doc("PRICE_DISCOUNT", _lista_sd)
            _alvo_sd = None
            try:
                _alvo_sd = float(ev["pb"])
            except (TypeError, ValueError, KeyError):
                _alvo_sd = None
            if (_dmax_sd is not None and _alvo_sd is not None
                    and not (_dmin_sd <= _alvo_sd <= _dmax_sd)):
                _pct_sd = (1 - _alvo_sd / _lista_sd) * 100 if _lista_sd else 0
                gravar(fila["id"], {"status": "erro", "resultado": (
                    f"NÃO MEXI EM NADA. O preço R${_alvo_sd:.2f} dá {_pct_sd:.1f}% de desconto "
                    f"sobre R${_lista_sd:.2f}, e o ML só aceita entre "
                    f"{DESC_MIN_PCT['PRICE_DISCOUNT']:.0f}% e {DESC_MAX_PCT['PRICE_DISCOUNT']:.0f}%. "
                    f"Escolha uma margem que caia entre R${_dmin_sd:.2f} e R${_dmax_sd:.2f}.")})
                print(f"  ! fora_da_faixa_doc {iid}: {_pct_sd:.1f}% de desconto, fora de "
                      f"{DESC_MIN_PCT['PRICE_DISCOUNT']:.0f}–{DESC_MAX_PCT['PRICE_DISCOUNT']:.0f}% "
                      f"— NÃO saí de nada", flush=True)
                return "fora_da_faixa_doc"
        _ind_ativo = individual_em_vigor(ofertas)
        # INDIVIDUAL -> INDIVIDUAL: troca so o preco, nao toca em campanha. Se o
        # individual atual esta em vigor, nenhuma campanha o bloqueia (doc: DEAL ativo
        # impede o individual de valer), logo o substituto tambem nao sera bloqueado.
        # TRAVA (22/ago): remover para recriar falhou 16 de 16, com 15 anuncios ficando
        # SEM desconto. TROCAR_INDIVIDUAL=1 devolve o comportamento anterior.
        if (tipo == "PRICE_DISCOUNT" and _ind_ativo is not None
                and not TROCAR_INDIVIDUAL):
            _pv_tv = None
            try:
                _pv_tv = rec.preco_oferta(_ind_ativo)
            except Exception:
                _pv_tv = None
            _txt_tv = (f"R${float(_pv_tv):.2f}" if _pv_tv else "preço não lido")
            # ---- JÁ ESTÁ NO ALVO? ----
            # Mede a margem NO PREÇO QUE JÁ ESTÁ EM VIGOR. Se ela já entrega o que
            # você pediu, não há o que trocar: o desfecho é 'resolvido', não 'bloqueado'.
            # Acontece muito por causa do ANÚNCIO IRMÃO: o ML propaga o desconto pela
            # família, então o irmão aplicado nesta mesma rodada já deixou este aqui no
            # preço certo. Na rodada de 70 foram pelo menos 4 assim, ao centavo.
            #
            # A comparação é de MARGEM, não de preço: um centavo de diferença não pode
            # reprovar um item que está certo. Tolerância é a mesma da trava da margem.
            _mg_alvo = fila.get("margem_alvo_manual")
            if _mg_alvo in (None, ""):
                _mg_alvo = fila.get("margem_prevista")
            try:
                _mg_alvo = float(_mg_alvo) if _mg_alvo not in (None, "") else None
            except (TypeError, ValueError):
                _mg_alvo = None
            _mg_vigor = None
            if _pv_tv and _mg_alvo is not None:
                try:
                    _c_tv = dict(cand)
                    _c_tv["price"] = round(float(_pv_tv), 2)
                    _e_tv = rec.avaliar(_c_tv, cat, ltid, access, frete, custo)
                    _mg_vigor = _e_tv.get("margem") if _e_tv else None
                except (TypeError, ValueError, KeyError):
                    _mg_vigor = None
            # A comparação é uma FAIXA, não um piso. Eu tinha escrito ">= alvo", e isso
            # confundiu duas coisas diferentes: "já está no ponto que você pediu" com
            # "está acima do que você pediu". Na rodada de 27 isso engoliu 18 anúncios —
            # um deles com 34,93% de margem enquanto você pedia 18%, ou seja, R$18,74 de
            # desconto que você mandou dar e não foi dado. No Acelerar, margem ACIMA do
            # alvo não é item resolvido: é justamente o item que falta descontar.
            if (_mg_vigor is not None and _mg_alvo is not None
                    and abs(float(_mg_vigor) - float(_mg_alvo)) <= MARGEM_TOL_PP):
                gravar(fila["id"], {"status": "aplicada",
                                    "preco_aplicado": round(float(_pv_tv), 2),
                                    "margem_aplicada": _mg_vigor,
                                    "resultado": (
                    f"JÁ ESTÁ NO ALVO ✓ — o desconto individual em vigor ({_txt_tv}) "
                    f"entrega {float(_mg_vigor):.2f}%, e você pediu {float(_mg_alvo):.2f}% "
                    f"(diferença de {abs(float(_mg_vigor)-float(_mg_alvo)):.2f} ponto, dentro da tolerância). "
                    f"Não havia o que trocar. Quando o preço bate com o que seria enviado, "
                    f"em geral é o anúncio IRMÃO sincronizado que já aplicou por este.")})
                print(f"  = {iid}: já está no alvo ✓ — em vigor {_txt_tv} entrega "
                      f"{float(_mg_vigor):.2f}% (pedido {float(_mg_alvo):.2f}%)", flush=True)
                return "ja_ativa"
            # ---- TEM OUTRA PROMOÇÃO ATIVA? É ISSO QUE DECIDE ----
            # O piloto troca individual por individual todo dia e funciona. A regra
            # dele (linha 515) é pular quando existe OUTRA promoção ativa — e o 16/16
            # de 22/ago foi feito justamente nesses. A doc explica: "Se ao iniciar o
            # desconto o item estiver participando de um DEAL, o desconto não será
            # aplicado até que o DEAL associado seja finalizado."
            #
            # participacoes_completas junta as DUAS fontes de propósito: o endpoint por
            # item não devolve participação ATIVA de campanha cofinanciada, que é
            # exatamente a que atrapalha aqui.
            _outras_ativas = [p for p in (participacoes_completas(
                                  iid, str(fila.get("seller_id") or ""), access, ofertas) or [])
                              if (p.get("type") or "").upper() != "PRICE_DISCOUNT"]
            if _outras_ativas:
                _nomes_out = ", ".join(
                    f"{(p.get('name') or p.get('promotion_id') or '?')}({(p.get('type') or '?')})"
                    for p in _outras_ativas[:6])
                gravar(fila["id"], {"status": "erro", "resultado": (
                    f"NÃO MEXI EM NADA. O anúncio tem desconto individual em vigor "
                    f"({_txt_tv}) E TAMBÉM {len(_outras_ativas)} promoção(ões) ativa(s): "
                    f"{_nomes_out}. Trocar o individual assim é o caso que falhou 16 de 16 "
                    f"em 22/ago — a doc do ML diz que com um DEAL ativo o desconto novo "
                    f"não passa a valer. Para trocar, é preciso sair dessas promoções antes, "
                    f"e isso eu não faço sozinho: de DEAL o robô não sabe devolver o preço.")})
                print(f"  ! ja_tem_individual {iid}: em vigor {_txt_tv} + {len(_outras_ativas)} "
                      f"promoção(ões) ativa(s) ({_nomes_out}) — não troco com campanha ativa",
                      flush=True)
                return "ja_tem_individual"
            # Nada mais ativo: é o caminho limpo, o mesmo que o piloto roda todo dia.
            # Segue para o bloco _so_o_preco: remove o individual, ESPERA a candidatura
            # reabrir (melhor que o sleep(0.3) do piloto) e recria com os 14 dias.
            print(f"  ~ {iid}: só o nosso desconto individual está ativo ({_txt_tv}) — "
                  f"trocando o preço pela receita do piloto", flush=True)
        _so_o_preco = (tipo == "PRICE_DISCOUNT" and _ind_ativo is not None)
        if _so_o_preco:
            _pv_sd = None
            try:
                _pv_sd = rec.preco_oferta(_ind_ativo)
            except Exception:
                _pv_sd = None
            _scd_sd, _b_sd = remover_participacao(
                iid, {"type": "PRICE_DISCOUNT",
                      "promotion_id": _ind_ativo.get("id"),
                      "offer_id": _ind_ativo.get("offer_id") or _ind_ativo.get("ref_id")},
                access)
            _txt_sd = (f"R${float(_pv_sd):.2f}" if _pv_sd else "preço não lido")
            if _scd_sd in (200, 201):
                _ind_antigo = _ind_ativo
                saiu_antes = (f" | ESCOLHA SUA (individual→individual): removi só o desconto "
                              f"individual ({_txt_sd}) e mantive as campanhas")
                print(f"  ~ {iid}: troca de individual — removi só o desconto atual "
                      f"({_txt_sd}), campanhas intactas", flush=True)
                _pronto, _voltas, _motivo = esperar_saida_individual(iid, access)
                saiu_antes += f" | esperei a remoção fechar ({_voltas}x): {_motivo}"
                print(f"  ⏳ {iid}: esperei a remoção fechar — {_voltas} consulta(s), {_motivo}",
                      flush=True)
            else:
                gravar(fila["id"], {"status": "erro", "resultado": (
                    f"NÃO MEXI EM NADA. Tentei remover o desconto individual atual "
                    f"({_txt_sd}) para pôr o novo e o ML recusou (HTTP {_scd_sd}). "
                    f"Nenhuma campanha foi tocada.")})
                print(f"  ! nao_removi_individual {iid}: ML recusou o DELETE (HTTP {_scd_sd}) "
                      f"— campanhas intactas", flush=True)
                return "nao_removi_individual"
        else:
            _saiu, _falhou, _rest = sair_das_outras(
                iid, str(fila.get("seller_id") or ""), access,
                manter_pid=cand.get("id"),          # mantem SO a que vamos entrar
                saiu_dicts=_saiu_dicts)
            _saiu_nomes = list(_saiu)
            saiu_antes = (f" | ESCOLHA SUA: saí de {len(_saiu)} promoção(ões) antes de entrar "
                          f"na que você pediu")
            if _falhou:
                saiu_antes += " | ⚠️ DELETE recusado em: " + ", ".join(_falhou)
            print(f"  ~ {iid}: escolha sua — saí de {len(_saiu)} promoção(ões) antes de entrar"
                  + (f" | falhou em {len(_falhou)}" if _falhou else ""), flush=True)
            # ESPERAR A REMOÇÃO FECHAR sempre que o alvo for individual — não só quando
            # já havia um individual. A remoção no ML é assíncrona (restore_requested):
            # quem tinha SÓ campanhas postava antes de o item ficar livre e levava "No
            # candidates found". Custou 4 anúncios (MLB4288780921; MLB1539564488,
            # MLB3164233836, MLB4419645793). A espera encerra no sinal certo: quando o
            # ML volta a oferecer candidatura de PRICE_DISCOUNT.
            if _ind_ativo is not None or (tipo == "PRICE_DISCOUNT" and _saiu):
                if _ind_ativo is not None:
                    _ind_antigo = _ind_ativo
                _pronto, _voltas, _motivo = esperar_saida_individual(iid, access)
                _oq = ("o desconto individual sair" if _ind_ativo is not None
                       else f"a candidatura reabrir depois de sair de {len(_saiu)} campanha(s)")
                saiu_antes += f" | esperei a remoção fechar ({_voltas}x): {_motivo}"
                print(f"  ⏳ {iid}: esperei {_oq} — {_voltas} consulta(s), {_motivo}",
                      flush=True)
    if acao == "trocar" and tipo == "PRICE_DISCOUNT" and not _sai_de_todas:
        # Caminho do piloto/reator (escolha automatica); com escolha sua, o bloco acima
        # ja resolveu e este nao roda.
        # PRE-VOO: decidir ANTES de tocar em campanha. A ordem anterior saia primeiro e
        # so entao via que havia individual em vigor — custou 7 anuncios em 20/ago. Que o
        # ML recusa candidatura nova com individual ativo esta MEDIDO: 17 de 17.
        _ind_atual = individual_em_vigor(ofertas)
        if _ind_atual is not None and not TROCAR_INDIVIDUAL:
            _pv_at = None
            try:
                _pv_at = rec.preco_oferta(_ind_atual)
            except Exception:
                _pv_at = None
            # Se o preco em vigor JA e o alvo, o irmao sincronizado aplicou por
            # nos: nao ha o que trocar e o desfecho e sucesso, nao bloqueio.
            try:
                if (_pv_at is not None and ev.get("pb") is not None
                        and abs(float(_pv_at) - float(ev["pb"])) <= max(0.02, float(ev["pb"]) * 0.01)):
                    gravar(fila["id"], {
                        "status": "aplicada",
                        "preco_aplicado": round(float(_pv_at), 2),
                        "margem_aplicada": ev.get("margem"),
                        "resultado": (f"já estava no preço-alvo (R${float(_pv_at):.2f}) por desconto "
                                      f"individual em vigor — nada a trocar.")})
                    print(f"  = {iid}: já estava no preço-alvo (R${float(_pv_at):.2f}) — "
                          f"desconto individual em vigor ✓", flush=True)
                    return "ja_ativa"
            except (TypeError, ValueError):
                pass
            _txt_at = (f"R${float(_pv_at):.2f}" if _pv_at else "preço não lido")
            _fim_at = _ind_atual.get("finish_date") or "sem fim declarado"
            gravar(fila["id"], {"status": "erro", "resultado": (
                f"NÃO MEXI EM NADA. Este anúncio já tem desconto individual em vigor "
                f"({_txt_at}, até {_fim_at}) e o ML não abre candidatura para um segundo — "
                f"medido em 17 de 17 casos. Para trocar o preço é preciso remover o atual, "
                f"e isso está desligado (TROCAR_INDIVIDUAL=0) porque a remoção falhou em "
                f"19/ago. Faça a troca no ML, ou espere o desconto atual terminar.")})
            print(f"  ! ja_tem_individual {iid}: em vigor {_txt_at} até {_fim_at} — "
                  f"NÃO saí de campanha nenhuma", flush=True)
            return "ja_tem_individual"
        _saiu, _falhou, _rest = sair_das_outras(iid, str(fila.get("seller_id") or ""), access,
                                                manter_tipo="PRICE_DISCOUNT",
                                                saiu_dicts=_saiu_dicts)
        _saiu_nomes = list(_saiu)
        saiu_antes = f" | saí de {len(_saiu)} antiga(s) ANTES de entrar (regra da ML p/ desconto individual)"
        if _falhou:
            saiu_antes += " | ⚠️ DELETE recusado em: " + ", ".join(_falhou)
        print(f"  ~ {iid}: saí de {len(_saiu)} campanha(s) antes de aplicar o desconto individual"
              + (f" | falhou em {len(_falhou)}" if _falhou else ""), flush=True)
        # A causa do "No candidates found" segue DESCONHECIDA — remover o individual
        # em vigor NÃO abre a candidatura (medição de 19/ago, ver TROCAR_INDIVIDUAL).
        # Por isso o padrão é não tocar nele: relatar que existe é tudo que dá pra afirmar.
        _ind_antigo = _ind_atual        # ja lido no pré-voo, nao consulta de novo
        if _ind_antigo is not None:
            # ---- PRÉ-VOO: o preço que vamos pedir CABE na regra? ----
            # A doc exige desconto entre 5% e 80% no PRICE_DISCOUNT. Se o alvo
            # estiver fora, o POST é recusa garantida — e destruir o desconto que
            # existe para tentar algo que já sabemos que falha é o erro de ontem
            # (MLB4214321589: alvo a 0% de desconto, apagamos o que tinha).
            _lista_pv = None
            try:
                _lista_pv = float(cand.get("original_price") or it.get("price") or 0)
            except (TypeError, ValueError):
                _lista_pv = 0
            _dmin, _dmax = faixa_da_doc("PRICE_DISCOUNT", _lista_pv)
            _alvo_pv = None
            try:
                _alvo_pv = float(ev["pb"])
            except (TypeError, ValueError, KeyError):
                _alvo_pv = None
            if _dmax is not None and _alvo_pv is not None and not (_dmin <= _alvo_pv <= _dmax):
                _pct = (1 - _alvo_pv / _lista_pv) * 100 if _lista_pv else 0
                gravar(fila["id"], {"status": "erro", "resultado": (
                    f"NÃO MEXI EM NADA. O preço-alvo R${_alvo_pv:.2f} dá {_pct:.1f}% de "
                    f"desconto sobre R${_lista_pv:.2f}, e a doc do ML só aceita entre "
                    f"{DESC_MIN_PCT['PRICE_DISCOUNT']:.0f}% e {DESC_MAX_PCT['PRICE_DISCOUNT']:.0f}% "
                    f"(erro buyer_discount_not_in_range). O ML recusaria, e este anúncio JÁ TEM "
                    f"desconto individual em vigor — não vale destruí-lo para tentar. "
                    f"Escolha uma margem que caia entre R${_dmin:.2f} e R${_dmax:.2f}.")})
                print(f"  ! fora_da_faixa_doc {iid}: alvo R${_alvo_pv:.2f} = {_pct:.1f}% de "
                      f"desconto, fora de {DESC_MIN_PCT['PRICE_DISCOUNT']:.0f}–"
                      f"{DESC_MAX_PCT['PRICE_DISCOUNT']:.0f}% — NÃO removi o desconto atual",
                      flush=True)
                return "fora_da_faixa_doc"
            _pa_ant = None
            try:
                _pa_ant = rec.preco_oferta(_ind_antigo)
            except Exception:
                _pa_ant = None
            _scd_ant, _b_ant = remover_participacao(
                iid, {"type": "PRICE_DISCOUNT",
                      "promotion_id": _ind_antigo.get("id"),
                      "offer_id": _ind_antigo.get("offer_id") or _ind_antigo.get("ref_id")},
                access)
            if _scd_ant in (200, 201):
                _txt_ant = (f"R${float(_pa_ant):.2f}" if _pa_ant else "preço não lido")
                _fim_ant = _ind_antigo.get("finish_date") or "sem fim declarado"
                saiu_antes += (f" | removi o desconto individual em vigor ({_txt_ant}, até "
                               f"{_fim_ant}) pra poder criar o novo")
                print(f"  ~ {iid}: removi o desconto individual em vigor ({_txt_ant}, até "
                      f"{_fim_ant}) — é o que libera a candidatura do novo", flush=True)
                # O 200 do DELETE é só o aceite do pedido: a doc lista
                # 'restore_requested' como "processo pendente de remoção do desconto".
                # Postar antes disso fechar foi o que deu 6 recusas em 19/ago.
                _pronto, _voltas, _motivo = esperar_saida_individual(iid, access)
                saiu_antes += f" | esperei a remoção fechar ({_voltas} consulta(s)): {_motivo}"
                print(f"  ⏳ {iid}: esperei a remoção fechar — {_voltas} consulta(s), {_motivo}",
                      flush=True)
                if not _pronto:
                    print(f"  ⚠ {iid}: a remoção NÃO fechou a tempo — o POST a seguir tem "
                          f"chance alta de ser recusado", flush=True)
            else:
                # Não saiu: não há o que restaurar, e o POST provavelmente será recusado.
                # Dizer isso agora é melhor que descobrir no erro genérico do ML.
                print(f"  ⚠ {iid}: NÃO consegui remover o desconto individual em vigor "
                      f"(HTTP {_scd_ant}) — o ML deve recusar a criação do novo", flush=True)
                saiu_antes += f" | ⚠️ não removi o individual em vigor (HTTP {_scd_ant})"
                _ind_antigo = None
    # ---- CAMPANHA DO VENDEDOR JÁ ATIVA: MODIFICA, não recria ----
    # Doc /pt_br/campanhas-do-vendedor, "Modificar itens":
    #     PUT /seller-promotions/items/$ITEM_ID  { promotion_id, promotion_type, deal_price }
    #     "só é possível modificar itens que pertencem a campanhas com sub_type
    #      FLEXIBLE_PERCENTAGE"
    #     oferta ativa: "Os preços só podem ser reduzidos"
    # É o caminho seguro: não sai de nada, não abre janela sem desconto, não gasta
    # candidatura. Só serve quando o item JÁ ESTÁ ativo nesta campanha — se ainda é
    # candidato, entrar é POST mesmo.
    _put_feito = False
    if acao == "trocar" and tipo == "SELLER_CAMPAIGN":
        _ativa_aqui = next((o for o in ofertas if isinstance(o, dict)
                            and (o.get("type") or "").upper() == "SELLER_CAMPAIGN"
                            and str(o.get("id") or "") == str(cand.get("id") or "")
                            and (o.get("status") or "").lower() == "started"), None)
        if _ativa_aqui is not None:
            _atual = rec.preco_oferta(_ativa_aqui)
            _novo = ev["pb"]
            if _atual is not None and float(_novo) >= float(_atual):
                # A doc é explícita: "New deal_price must be lower than current deal_price".
                # Tentar assim é recusa certa; dizer isso é mais útil que gastar a chamada.
                gravar(fila["id"], {"status": "erro", "resultado": (
                    f"{aviso_piso}NÃO MEXI. Nesta campanha do vendedor o item já está ativo a "
                    f"R${float(_atual):.2f} e você pediu R${float(_novo):.2f}. Com a oferta ativa "
                    f"o ML só aceita REDUZIR o preço (\"New deal_price must be lower than current "
                    f"deal_price\"). Para subir, é preciso sair da campanha — e isso eu não faço "
                    f"sozinho.")})
                print(f"  ! so_reduz {iid}: ativo a R${float(_atual):.2f}, pedido R${float(_novo):.2f} "
                      f"— a campanha do vendedor só aceita reduzir", flush=True)
                return "so_reduz"
            _corpo_put = {"promotion_id": cand.get("id"), "promotion_type": "SELLER_CAMPAIGN",
                          "deal_price": _clamp_preco(_novo, cand)}
            sc, resp = req_put(f"/seller-promotions/items/{iid}?app_version=v2", access, _corpo_put)
            _put_feito = True
            corpo = _corpo_put
            print(f"  ~ {iid}: campanha do vendedor JÁ ATIVA — modifiquei por PUT "
                  f"(R${float(_atual):.2f} -> R${_corpo_put['deal_price']:.2f}), sem sair de nada",
                  flush=True)
    if not _put_feito:
        sc, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
    ok = sc in (200, 201)
    if acao == "trocar" and _sai_de_todas:
        # Ja saimos de tudo antes de entrar: nao ha antiga para sair depois.
        aviso = saiu_antes + (" | entrei na que você escolheu ✓" if ok else
                " | 🚨 ENTRADA RECUSADA DEPOIS DE SAIR — o anúncio está SEM promoção agora.")
        if (not ok) and _ind_antigo is not None:
            _rok, _rtxt = restaurar_individual(iid, access, _ind_antigo)
            aviso += (f" | ↩️ restaurei o desconto individual: {_rtxt}" if _rok
                      else f" | 🚨 NÃO restaurei o individual: {_rtxt}")
            print(("  ↩️ " if _rok else "  🚨 ") + f"{iid}: individual — {_rtxt}", flush=True)
            if not _rok:
                SEM_DESCONTO_AGORA.append((iid, [f"desconto individual ({_rtxt})"]))
        if (not ok) and _saiu_nomes:
            _voltou, _nvoltou = reentrar_nas_campanhas(
                iid, str(fila.get("seller_id") or ""), access, _saiu_dicts)
            if _voltou:
                REENTRADAS.append((iid, list(_voltou)))
                aviso += " | ↩️ devolvi: " + ", ".join(_voltou)
                print(f"  ↩️ {iid}: devolvi {len(_voltou)} — " + ", ".join(_voltou), flush=True)
            if _nvoltou:
                SEM_DESCONTO_AGORA.append((iid, list(_nvoltou)))
                aviso += " | 🚨 NÃO devolvi: " + ", ".join(_nvoltou)
                print(f"  🚨 {iid}: NÃO devolvi {len(_nvoltou)} — " + ", ".join(_nvoltou),
                      flush=True)
    elif acao == "trocar" and _put_feito:
        # O PUT não removeu nada e não tem antiga pra sair: o desfecho é só o do PUT.
        aviso = (" | modifiquei o preço na campanha do vendedor por PUT ✓" if ok
                 else " | ⚠️ o ML recusou o PUT — nada foi alterado, o anúncio segue como estava")
    elif acao == "trocar":
        if tipo == "PRICE_DISCOUNT":
            aviso = saiu_antes + (" | entrei no desconto individual ✓" if ok else
                    " | 🚨 ENTRADA RECUSADA DEPOIS DE SAIR — o anúncio está SEM desconto agora. Reveja no painel.")
            # Este aviso existia, mas viajava só até o campo 'resultado' no banco. Quem
            # lia o log da rodada não tinha como distinguir o anúncio que perdeu 3
            # campanhas do que não perdeu nenhuma: os dois imprimiam a mesma linha de
            # desfecho. Agora o caso grave grita na hora e com nome das perdas.
            if (not ok) and _ind_antigo is not None:
                # Primeiro devolve o desconto individual: é o que o cliente estava
                # pagando até segundos atrás, e é o de maior impacto no preço.
                _rok, _rtxt = restaurar_individual(iid, access, _ind_antigo)
                if _rok:
                    aviso += f" | ↩️ RESTAUREI o desconto individual anterior: {_rtxt}"
                    print(f"  ↩️ {iid}: restaurei o desconto individual anterior — {_rtxt}",
                          flush=True)
                else:
                    SEM_DESCONTO_AGORA.append((iid, [f"desconto individual anterior ({_rtxt})"]))
                    aviso += f" | 🚨 NÃO RESTAUREI o desconto individual anterior: {_rtxt}"
                    print(f"  🚨 {iid}: NÃO consegui restaurar o desconto individual anterior "
                          f"— {_rtxt}", flush=True)
            if (not ok) and _saiu_nomes:
                print(f"  🚨 {iid}: SAÍ de {len(_saiu_nomes)} campanha(s) e a entrada foi RECUSADA "
                      f"— desfazendo a saída agora. Tinha: " + ", ".join(_saiu_nomes), flush=True)
                _voltou, _nvoltou = reentrar_nas_campanhas(
                    iid, str(fila.get("seller_id") or ""), access, _saiu_dicts)
                if _voltou:
                    REENTRADAS.append((iid, list(_voltou)))
                    aviso += " | ↩️ DESFIZ A SAÍDA: " + ", ".join(_voltou)
                    print(f"  ↩️ {iid}: reentrou em {len(_voltou)} — " + ", ".join(_voltou), flush=True)
                if _nvoltou:
                    SEM_DESCONTO_AGORA.append((iid, list(_nvoltou)))
                    aviso += " | 🚨 NÃO CONSEGUI DEVOLVER: " + ", ".join(_nvoltou)
                    print(f"  🚨 {iid}: NÃO consegui devolver {len(_nvoltou)} — "
                          + ", ".join(_nvoltou) + " — esse anúncio precisa de você.", flush=True)
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
        if "NO CANDIDATES FOUND" in _t:
            # "No candidates found for item" NÃO é "não é elegível". Verificado no
            # navegador em 3 dos 12 recusados de uma rodada: os três já estavam no
            # preço-alvo. Nas 12, um IRMÃO sincronizado aplicara o mesmo preço na mesma
            # rodada — o ML propaga pela família e não sobra candidatura para pedir de
            # novo. O trabalho foi feito, só não por este POST. Então perguntamos ao ML
            # quanto o cliente paga antes de concluir. (Ao conferir na página: "Desconto
            # no Pix" tira mais uns R$10; o sale_price vem sem ele.)
            _alvo_ok = None
            try:
                _pv, _pt = preco_venda_real(iid, access)
                _ref = fila.get("preco_alvo")
                if _ref in (None, ""):
                    _ref = (ev.get("pb") if isinstance(ev, dict) else None)
                if _pv is not None and _ref not in (None, ""):
                    _alvo_ok = abs(float(_pv) - float(_ref)) <= max(0.02, float(_ref) * 0.01)
            except (TypeError, ValueError):
                _alvo_ok = None
            if _alvo_ok:
                gravar(fila["id"], {
                    "status": "aplicada",
                    "preco_aplicado": round(float(_pv), 2),
                    "resultado": (f"já aplicado via IRMÃO sincronizado ✓ — cliente paga R${float(_pv):.2f}, "
                                  f"que é o alvo. O ML respondeu \"No candidates found\" porque o desconto "
                                  f"já vale para este produto e não sobra candidatura.")})
                print(f"  = {iid}: já estava no preço-alvo (R${float(_pv):.2f}) — aplicado pelo irmão ✓", flush=True)
                return "ja_ativa"
            motivo = ("O ML respondeu \"No candidates found for item\" e o preço atual "
                      + (f"(R${float(_pv):.2f}) " if _pv is not None else "(não consegui ler) ")
                      + "NÃO é o alvo. NÃO é conclusão de inelegibilidade: essa resposta "
                        "também aparece quando o lugar já está ocupado. Se o anúncio tinha "
                        "desconto individual em vigor, o robô removeu antes de pedir — veja "
                        "acima neste mesmo texto se isso aconteceu. Retentar igual não resolve.")
            cod_erro = "sem_candidatura_ml"
        elif "CREDIBILITY" in _t:
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
            # A referência da 2ª passada é o preço que o aplicador REALMENTE ENVIOU ao
            # ML — não o que a tela pediu. Os dois divergem sempre que _clamp_preco
            # encaixou o pedido na faixa do anúncio. Comparar contra o pedido fazia um
            # item obediente sair marcado como divergente, com uma causa inventada.
            # O pedido original fica guardado à parte pra o corte do teto ser relatado
            # como o que ele é: limite do ML, medido no envio.
            _pa = preco_ap_mem.get(f["id"])
            if _pa is not None:
                _pedido = f.get("preco_alvo")
                try:
                    if _pedido not in (None, "") and abs(float(_pedido) - float(_pa)) > 0.005:
                        f["_preco_pedido"] = float(_pedido)
                except (TypeError, ValueError):
                    pass
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
    _cod_por_fila = {}      # código -> [ids de repricer_promo_fila]
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
                _cod_por_fila.setdefault(code, []).append(f["id"])
            registrar_retry(f, code, txt)

    # ---- CÓDIGO DO DESFECHO NA FILA ----
    # O status ('erro') não distingue "o ML recusou" de "eu me recusei de propósito",
    # e o painel pintava as duas coisas com a mesma pílula vermelha. O código separa.
    # Os ids "retry:..." ficam de fora de propósito: não são linha de repricer_promo_fila.
    #
    # Um update por CÓDIGO (não por item): 5 a 10 chamadas numa rodada de centenas.
    if _cod_por_fila:
        _ok, _erro = 0, None
        for _code, _ids in _cod_por_fila.items():
            try:
                sb.table("repricer_promo_fila").update({"codigo": _code}).in_("id", _ids).execute()
                _ok += len(_ids)
            except Exception as e:
                _erro = e
                break
        if _erro is not None:
            # Não derruba a rodada: o status e o resultado JÁ foram gravados item a item.
            # Esta coluna é informação a mais.
            print(f"  aviso: não gravei o código do desfecho na fila ({_erro}). "
                  f"Se a mensagem falar em coluna inexistente, rode o fase_codigo_fila.sql "
                  f"no Supabase — a rodada em si não foi afetada.", flush=True)
        else:
            print(f"  código do desfecho gravado em {_ok} linha(s) da fila "
                  f"({len(_cod_por_fila)} desfecho(s) distinto(s)).", flush=True)
    if REENTRADAS:
        print(f"\n↩️  {len(REENTRADAS)} anúncio(s) tiveram a saída DESFEITA: o ML recusou o "
              f"desconto individual e o robô refez as participações.", flush=True)
        for _iid, _campanhas in REENTRADAS:
            print(f"      {_iid}: voltou para {len(_campanhas)} — " + ", ".join(_campanhas), flush=True)
        print("      Onde aparece um preço, ele NÃO é o preço anterior (esse não fica guardado "
              "em lugar nenhum): é o sugerido pelo ML ou o teto da faixa. Confira se importa.",
              flush=True)
        resumo["reentradas"] = len(REENTRADAS)
    if SEM_DESCONTO_AGORA:
        # Um anúncio pode entrar aqui DUAS vezes (perdeu o individual E as campanhas).
        # Contar as linhas dizia "10 anúncios" quando eram 6 — inflar o próprio estrago
        # não é mais honesto que escondê-lo.
        _quantos = len({_i for _i, _ in SEM_DESCONTO_AGORA})
        print(f"\n🚨 {_quantos} anúncio(s) PERDERAM promoção e o robô NÃO conseguiu "
              f"devolver ({len(SEM_DESCONTO_AGORA)} registro(s)).", flush=True)
        print("   Saíram das campanhas (regra da ML p/ desconto individual), o ML recusou a "
              "entrada, e a volta também não deu.", flush=True)
        for _iid, _campanhas in SEM_DESCONTO_AGORA:
            print(f"      {_iid}: " + ", ".join(_campanhas), flush=True)
        print("   AÇÃO SUA: refaça essas participações no ML. O robô NÃO vai retentar sozinho "
              "(o desfecho é terminal).", flush=True)
        resumo["sem_desconto_agora"] = len({_i for _i, _ in SEM_DESCONTO_AGORA})
    if TETO_DO_ML:
        print(f"\n📏 {len(TETO_DO_ML)} item(ns): o ML limita o desconto e enviei o teto dele:", flush=True)
        for _iid, _ped, _ap in TETO_DO_ML:
            print(f"      {_iid}: você pediu R${_ped:.2f} | teto do ML R${_ap:.2f} "
                  f"(R${_ped - _ap:.2f} a menos do que você queria cobrar)", flush=True)
        print("      Causa MEDIDA no envio (max_discounted_price do anúncio). "
              "Não é erro: é o limite do ML. Pra cobrar mais, o caminho é o preço cheio.", flush=True)
        resumo["teto_do_ml"] = len(TETO_DO_ML)
    if PRECO_DIVERGENTE:
        print(f"\n⚠️  {len(PRECO_DIVERGENTE)} item(ns): o ML aplicou preço DIFERENTE do que enviei:", flush=True)
        for _iid, _alvo, _pv in PRECO_DIVERGENTE:
            print(f"      {_iid}: enviei R${_alvo:.2f} -> cliente paga R${_pv:.2f} "
                  f"(diferença R${_alvo - _pv:.2f})", flush=True)
        print("      NÃO MEDI A CAUSA. Hipótese a conferir: preço propagado de irmão sincronizado.",
              flush=True)
        resumo["preco_divergente"] = len(PRECO_DIVERGENTE)
    print("resumo:", json.dumps(resumo, ensure_ascii=False), flush=True)
    grava_status("concluido", json.dumps(resumo, ensure_ascii=False))
if __name__ == "__main__":
    main()
