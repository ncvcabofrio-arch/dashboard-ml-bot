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
TIPOS_OK = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "LIGHTNING", "DEAL"}
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
    """Localiza, na resposta ATUAL do ML, a promoção candidata que foi aprovada."""
    tipo = (fila.get("promocao_tipo") or "").upper()
    pid = fila.get("promocao_id")
    nome = fila.get("promocao_nome")
    cands = [o for o in ofertas if isinstance(o, dict)
             and (o.get("status") or "").lower() == "candidate"
             and (o.get("type") or "").upper() == tipo]
    # 1) casa pelo id da promoção; 2) pelo nome; 3) se só sobrou uma do tipo, usa ela
    for o in cands:
        if pid and o.get("id") == pid:
            return o
    for o in cands:
        if nome and (o.get("name") or "") == nome:
            return o
    return cands[0] if len(cands) == 1 else None
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
def corpo_post(tipo, cand, preco_alvo):
    """Monta o corpo do POST conforme o tipo (docs seller-promotions v2)."""
    tipo = (tipo or "").upper()
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL"):
        # cofinanciada automatizada / preços competitivos: precisa do offer_id do candidato
        oid = cand.get("ref_id") or cand.get("offer_id") or cand.get("candidate_id") or cand.get("id")
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": tipo, "offer_id": oid}, cand)
    if tipo == "LIGHTNING":
        st = cand.get("stock") or {}
        estoque = st.get("min") or st.get("remaining_stock") or 1
        return {"deal_price": round(preco_alvo, 2), "stock": int(estoque), "promotion_type": "LIGHTNING"}
    if tipo == "DEAL":
        # PROVA DE ERRO (credibilidade): o deal_price TEM que ficar dentro da faixa crível
        # [min_discounted_price, max_discounted_price], senão o ML devolve 400 CREDIBILITY.
        preco = round(preco_alvo, 2)
        try:
            mx = cand.get("max_discounted_price")
            mn = cand.get("min_discounted_price")
            if mx is not None:
                preco = min(preco, float(mx))     # não pode ter desconto MENOR que o mínimo exigido
            if mn is not None:
                preco = max(preco, float(mn))     # nem desconto MAIOR que o permitido
        except (TypeError, ValueError):
            pass
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": "DEAL", "deal_price": round(preco, 2)}, cand)
    return None
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
def gravar(fila_id, patch):
    patch["aplicado_em"] = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("repricer_promo_fila").update(patch).eq("id", fila_id).execute()
    except Exception as e:
        print("  !! falha ao gravar resultado:", e, flush=True)
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
                   "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "VOLUME"}
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
def sair_das_outras(iid, seller_id, access, manter_pid=None, manter_tipo=None):
    """SAI de TODAS as promoções em que o item está ATIVO, exceto a que queremos manter.
    Usa o caminho CONFIÁVEL da doc (rec.participacoes_ativas): users/{seller} cruzado com
    promotions/{id}/items?item_id=... — porque /seller-promotions/items/{id} NÃO traz as
    ativas de campanha cofinanciada/marketplace (só candidatas). Sai por TIPO, uma a uma.
    Retorna (saiu[], falhou[], restantes[])."""
    ativas = rec.participacoes_ativas(iid, seller_id, access)
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
    restantes = [p for p in rec.participacoes_ativas(iid, seller_id, access)
                 if not (manter_pid and p.get("promotion_id") == manter_pid)
                 and not (manter_pid is None and manter_tipo and (p.get("type") or "").upper() == manter_tipo)]
    return saiu, falhou, restantes
def executar_sair(fila, iid, ofertas, access):
    """SAIR — 1ª PASSADA: sai de cada promoção em que o item participa (status started).
    NÃO confere agora (o ML leva segundos pra propagar): o sucesso é o 200 do DELETE.
    A conferência de verdade acontece na 2ª passada (revarrer_sair), no fim do main()."""
    seller_id = str(fila.get("seller_id") or "")
    ativas = rec.participacoes_ativas(iid, seller_id, access)
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
    rest = rec.participacoes_ativas(iid, seller_id, access)
    dels2 = []                          # (rótulo, status_http)
    for p in rest:
        scd, body = remover_participacao(iid, p, access)
        dels2.append((f"{(p.get('name') or '?')[:16]}[{p.get('type')}]:{scd}", scd))
    if dels2:
        remover_todas(iid, access)
    final = rec.participacoes_ativas(iid, seller_id, access)   # conferência final
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
    """ENTRAR/TROCAR — 2ª PASSADA (no fim do main, com tempo de propagação):
      - CONFIRMA que o anúncio ficou ATIVO (started) na promoção alvo (confirmar_entrada);
      - no TROCAR, re-varre e sai de qualquer OUTRA que ainda esteja ativa.
    Só mexe se a 1ª passada marcou 'aplicada' (se o POST falhou, mantém o erro)."""
    iid = fila["item_id"]
    seller_id = str(fila.get("seller_id") or "")
    pid = fila.get("promocao_id")
    tipo = (fila.get("promocao_tipo") or "").upper()
    base = (fila.get("resultado") or "").split(" || 2ª passada")[0]
    conf = confirmar_entrada(iid, pid, tipo, access)
    # APRENDIZADO DA DOC: ativação é ASSÍNCRONA. O POST já foi ACEITO na 1ª passada (senão nem
    # chegava aqui). Se ainda não virou 'started', é propagação (candidate/sync_requested) — NÃO
    # é falha. Por isso NÃO rebaixamos pra 'erro' vermelho; mantemos 'aplicada' com aviso ⏳.
    nota = ("entrada CONFIRMADA ✓" if conf is True
            else "⏳ aceita pelo ML, ainda ATIVANDO (assíncrono) — não confirmou 'started' ainda; costuma levar um tempo" if conf is False
            else "entrada não conferível por API (relâmpago)")
    extra = ""
    if (fila.get("acao") or "") == "trocar":
        rest = [p for p in rec.participacoes_ativas(iid, seller_id, access) if p.get("promotion_id") != pid]
        for p in rest:
            remover_participacao(iid, p, access)
        rest2 = [p for p in rec.participacoes_ativas(iid, seller_id, access) if p.get("promotion_id") != pid]
        extra = " | " + ("saiu das outras ✓" if not rest2
                         else "⚠️ ainda ativas: " + ", ".join((p.get('name') or p.get('type')) for p in rest2))
    gravar(fila["id"], {"status": "aplicada",
                        "resultado": f"{base} || 2ª passada — {nota}{extra}"})
    print(f"  [OK] {fila.get('acao')} {iid} (2ª passada) | {nota}", flush=True)
    return "confirmado" if conf is True else ("ativando" if conf is False else "confirmado")
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
    nova = _sugestao_atual(iid, fila.get("seller_id"))
    if nova is not None:
        nacao = (nova.get("acao") or "").lower()
        if nacao == "manter":
            gravar(fila["id"], {"status": "aplicada",
                "resultado": "já estava ATIVA no item ✓ (sugestão atual do robô: manter — não precisou entrar)"})
            return "ja_ativa"
        if nacao in ("entrar", "trocar"):
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
            gravar(fila["id"], {"status": "aplicada",
                "resultado": "já estava ATIVA no item ✓ (não precisou entrar)"})
            return "ja_ativa"
        gravar(fila["id"], {"status": "erro",
            "resultado": "candidato aprovado não está mais disponível — rode a sugestão de novo"})
        return "sem_candidato"
    # trava de vigência (consulta o detalhe da promoção): nunca entrar em promo programada/futura.
    # Fica ANTES da remoção das outras, pra nunca deixar o item sem promoção por causa disso.
    if not rec.cand_vigente(cand, access):
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
    sku = it.get("seller_sku") or it.get("seller_custom_field") or fila.get("sku")
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
    ev = rec.avaliar(cand, cat, ltid, access, frete, custo)
    if not ev:
        gravar(fila["id"], {"status": "erro", "resultado": "não deu pra avaliar a oferta agora"})
        return "sem_avaliar"
    if ev["margem"] < piso:
        gravar(fila["id"], {"status": "erro",
                            "resultado": f"margem caiu pra {ev['margem']:.1f}% (< piso {piso:.0f}%) — não apliquei"})
        return "abaixo_piso"
    # cofinanciadas que exigem data no POST (ex.: "OFERTAS RELÂMPAGOS IMPERDÍVEIS" -> erro START_DATE):
    # o candidato não traz as datas; pega do DETALHE da promoção. As datas têm que ir em formato
    # LOCAL (sem 'Z') e o start NÃO pode ser no passado (regras da doc).
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "DEAL") and not cand.get("start_date"):
        pd = rec._promo_detalhe(cand.get("id"), tipo, access)
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
        plano = (f"TROCA: entra na sugerida e SAI das ativas " if acao == "trocar" else "ENTRADA ")
        gravar(fila["id"], {"status": "aprovada",
                            "resultado": f"[SIMULADO] {plano}| POST {json.dumps(corpo)} | margem prevista {ev['margem']:.1f}%",
                            "preco_aplicado": ev["pb"], "margem_aplicada": ev["margem"]})
        print(f"  [DRY] {acao} {iid} {tipo} -> {json.dumps(corpo)} (margem {ev['margem']:.1f}%)", flush=True)
        return "simulado"
    # ---- APLICAÇÃO REAL ----
    # ORDEM (julgamento técnico, à prova de falha): ENTRAR PRIMEIRO na sugerida e só SAIR das
    # outras se a entrada for ACEITA (200/201). Falha SEGURA: se o ML recusar a entrada, não
    # mexemos em nada e o anúncio segue com as promoções que já tinha (nunca fica descoberto).
    # O ML aceita o item em VÁRIAS promoções ao mesmo tempo (doc), então entrar antes de sair não
    # gera conflito; a sobra sai uma a uma por tipo e é reconferida na 2ª passada.
    aviso = ""
    sc, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
    ok = sc in (200, 201)
    if acao == "trocar":
        if ok:
            seller_id = str(fila.get("seller_id") or "")
            # mantém a NOVA (por promotion_id) e sai de TODAS as outras, uma a uma, por tipo
            saiu, falhou, _ = sair_das_outras(iid, seller_id, access, manter_pid=cand.get("id"))
            aviso = " | entrou na sugerida ✓"
            if saiu:
                aviso += " | saiu das outras: " + ", ".join(saiu)
            if falhou:
                aviso += " | NÃO saiu (confere 2ª passada): " + ", ".join(falhou)
        else:
            # entrada recusada -> NÃO removemos nada: rede de segurança intacta
            aviso = " | ⚠️ entrada recusada — NÃO mexi nas promoções atuais (anúncio segue como estava)"
    motivo = ""
    if not ok:
        _t = json.dumps(resp, ensure_ascii=False).upper()
        if "CREDIBILITY" in _t:
            motivo = "FAÇA NA MÃO — o ML só aceita a relâmpago com um desconto maior do que o seu piso de margem permite. "
        elif "START_DATE" in _t:
            motivo = "FAÇA NA MÃO — essa campanha exige data que o ML não aceitou pela API. "
    gravar(fila["id"], {
        "status": "aplicada" if ok else "erro",
        "resultado": (f"OK {sc}{aviso}: {json.dumps(resp, ensure_ascii=False)[:220]}{light_diag}" if ok
                      else f"{motivo}{'ERRO ' + str(sc) if not aviso else 'ATENÇÃO'}{aviso} [enviei {json.dumps(corpo, ensure_ascii=False)}]: {json.dumps(resp, ensure_ascii=False)[:170]}{light_diag}"),
        "preco_aplicado": ev["pb"] if ok else None,
        "margem_aplicada": ev["margem"] if ok else None,
    })
    return "aplicado" if ok else "erro_post"
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
    escopo = (f" | itens {len(ITENS_FILTRO)} selecionados" if ITENS_FILTRO
              else (f" | item {ITEM_FILTRO}" if ITEM_FILTRO else " | FILA INTEIRA da conta"))
    print(f"{'SIMULAÇÃO (DRY_RUN)' if DRY else 'APLICAÇÃO REAL'} — {len(fila)} item(ns)"
          + (f" | conta {SELLER_FILTRO}" if SELLER_FILTRO else "") + escopo, flush=True)
    if not fila:
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
    for f in fila:
        sid = str(f["seller_id"])
        access = acessos.get(sid)
        if not access:
            gravar(f["id"], {"status": "erro", "resultado": f"sem acesso à conta {sid} (token não resolveu)"})
            resumo["sem_token"] = resumo.get("sem_token", 0) + 1
            continue
        r = processar(f, access)
        resumo[r] = resumo.get(r, 0) + 1
        print(f"  = {f['item_id']} [{f.get('acao', '?')}] -> {r}", flush=True)  # 1 linha por item (todos aparecem)
    # ---- 2ª PASSADA (depois de mexer em TODOS os itens -> deu tempo do ML propagar) ----
    # sair: varre de novo e confere; entrar/trocar: confirma a entrada (e o trocar sai das outras).
    alvos = [f for f in fila if f.get("acao") in ("sair", "entrar", "trocar") and acessos.get(str(f["seller_id"]))]
    if alvos:
        time.sleep(8)   # respiro extra de propagação
        for f in alvos:
            access = acessos.get(str(f["seller_id"]))
            # relê status/resultado da 1ª passada
            st1, res1 = "", ""
            try:
                atual = sb.table("repricer_promo_fila").select("status,resultado").eq("id", f["id"]).execute().data
                if atual:
                    st1 = (atual[0].get("status") or "")
                    res1 = (atual[0].get("resultado") or "")
            except Exception:
                pass
            f["resultado"] = res1
            if st1 != "aplicada":
                continue   # 1ª passada falhou (ex.: margem/credibilidade) — não sobrescreve o erro
            # 'já ativa' (não precisou entrar) não tem 2ª passada de confirmação
            if "já estava ATIVA" in res1:
                continue
            rr = revarrer_sair(f, access) if f.get("acao") == "sair" else confirmar_pos_entrada(f, access)
            resumo["2p_" + rr] = resumo.get("2p_" + rr, 0) + 1
    print("resumo:", json.dumps(resumo, ensure_ascii=False), flush=True)
    grava_status("concluido", json.dumps(resumo, ensure_ascii=False))
if __name__ == "__main__":
    main()
