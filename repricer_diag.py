"""
DIAGNÓSTICO COMPLETO DE PROMOÇÕES DE UM ANÚNCIO (somente leitura — não altera NADA).
Roda no GitHub Actions (lá tem o token OAuth) e despeja TUDO no LOG, usando todos os GETs.
Esta versão SONDA cada campanha por várias vias (sem filtro, status=started, status=candidate,
e PAGINANDO a lista inteira) pra descobrir se a participação 'started' está sendo perdida por
paginação ou por falta de filtro.
NOVO: seção 5) SIMULAÇÃO DE ENTRADA — reproduz TODAS as checagens do aplicador pra a promoção
recomendada e mostra o POST EXATO que ele enviaria, SEM escrever nada (nem no ML, nem no banco).
NOVO: seção 0) RAIO-X — traz TUDO que dá pra consultar do anúncio pela API (preço de venda x lista,
todos os preços, tarifa detalhada, frete, concorrência/price_to_win, catálogo, visitas/demanda) e
calcula a MARGEM ATUAL. Base pra desenhar estratégia melhor.
Uso (inputs do workflow): ITEM_ID (obrigatório) e SELLER_ID (conta).
"""
import os
import json
import repricer_sugestoes as rec
from datetime import datetime, timezone, timedelta
from ml_auth import obter_access
sb = rec.sb
ITEM = (os.environ.get("ITEM_ID") or os.environ.get("DIAG_ITEM") or "").strip()
SELLER = (os.environ.get("SELLER_ID") or "").strip()
TIPOS_SO_TIPO = {"PRICE_DISCOUNT", "LIGHTNING", "DOD"}
TIPOS_COM_OFFER = {"SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "MARKETPLACE_CAMPAIGN",
                   "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "VOLUME"}
def brl(v):
    try:
        return "R$ " + format(float(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)
def como_sair(iid, ptipo, pid, offer_id, item_status):
    t = (ptipo or "").upper()
    if t in ("LIGHTNING", "DOD") and (item_status or "").lower() == "started":
        return f"⚠️ {t} ATIVA não sai por API (só pausando o anúncio)"
    if t in TIPOS_SO_TIPO:
        return f"DELETE /items/{iid}?promotion_type={t}"
    base = f"DELETE /items/{iid}?promotion_type={t}&promotion_id={pid}"
    if t in TIPOS_COM_OFFER:
        return base + f"&offer_id={offer_id or '??FALTA_OFFER_ID??'}"
    return base
def _achar(res, iid):
    for x in (res or []):
        if str(x.get("id")) == str(iid):
            return x
    return None
# ---------- funções PURAS copiadas do aplicador (pra a simulação bater 100% com ele) ----------
def achar_candidato(ofertas, alvo):
    """Localiza, na resposta ATUAL do ML, a promoção candidata recomendada (mesma lógica do aplicador)."""
    tipo = (alvo.get("promocao_tipo") or "").upper()
    pid = alvo.get("promocao_id")
    nome = alvo.get("promocao_nome")
    cands = [o for o in ofertas if isinstance(o, dict)
             and (o.get("status") or "").lower() == "candidate"
             and (o.get("type") or "").upper() == tipo]
    for o in cands:
        if pid and o.get("id") == pid:
            return o
    for o in cands:
        if nome and (o.get("name") or "") == nome:
            return o
    return cands[0] if len(cands) == 1 else None
def _com_datas(corpo, cand):
    ini = cand.get("start_date")
    fim = cand.get("finish_date") or cand.get("end_date")
    if ini:
        corpo["start_date"] = ini
    if fim:
        corpo["finish_date"] = fim
    return corpo
def corpo_post(tipo, cand, preco_alvo):
    """Monta o corpo do POST conforme o tipo (idêntico ao aplicador)."""
    tipo = (tipo or "").upper()
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL"):
        oid = cand.get("ref_id") or cand.get("offer_id") or cand.get("candidate_id") or cand.get("id")
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": tipo, "offer_id": oid}, cand)
    if tipo == "LIGHTNING":
        st = cand.get("stock") or {}
        estoque = st.get("min") or st.get("remaining_stock") or 1
        return {"deal_price": round(preco_alvo, 2), "stock": int(estoque), "promotion_type": "LIGHTNING"}
    if tipo == "DEAL":
        preco = round(preco_alvo, 2)
        try:
            mx = cand.get("max_discounted_price")
            mn = cand.get("min_discounted_price")
            if mx is not None:
                preco = min(preco, float(mx))
            if mn is not None:
                preco = max(preco, float(mn))
        except (TypeError, ValueError):
            pass
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": "DEAL", "deal_price": round(preco, 2)}, cand)
    return None
def sonda_campanha(pid, tipo, iid, access):
    """Procura o item na campanha por VÁRIAS vias. Retorna dict {via: (status, offer_id)}
    e, se as vias com item_id não acharem 'started', PAGINA a lista inteira procurando."""
    out = {}
    base = f"/seller-promotions/promotions/{pid}/items?promotion_type={tipo}&item_id={iid}&app_version=v2"
    for nome, extra in (("item_id", ""), ("+started", "&status=started"),
                        ("+candidate", "&status=candidate"), ("+active", "&status_item=active")):
        st, d = rec.get(base + extra, access)
        res = (d.get("results") if isinstance(d, dict) else None) or []
        it = _achar(res, iid)
        out[nome] = (it.get("status"), it.get("offer_id")) if it else (None, None)
    achou_started = any(v[0] == "started" for v in out.values())
    if not achou_started:
        sa, npag, achado = None, 0, None
        for _ in range(15):
            npag += 1
            url = f"/seller-promotions/promotions/{pid}/items?promotion_type={tipo}&app_version=v2&limit=50"
            if sa:
                url += f"&search_after={sa}"
            st, d = rec.get(url, access)
            if not isinstance(d, dict):
                break
            res = d.get("results") or []
            it = _achar(res, iid)
            if it:
                achado = (it.get("status"), it.get("offer_id"), npag)
                break
            pag = d.get("paging") or {}
            sa = d.get("search_after") or pag.get("search_after") or pag.get("searchAfter")
            if not sa:
                break
        out["PAGINANDO"] = (f"{achado[0]} (pág {achado[2]}, offer={achado[1]})" if achado
                            else f"não achou em {npag} pág", None)
    return out
def dump(label, obj, corte=6000):
    print(f"\n===== {label} =====", flush=True)
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:corte], flush=True)
    except Exception:
        print(str(obj)[:corte], flush=True)
def _g(path, access):
    """GET tolerante — devolve (status, corpo) e nunca explode."""
    try:
        return rec.get(path, access)
    except Exception as e:
        return None, {"erro": str(e)}
def _attr_sku(bloco):
    """SKU guardado como ATRIBUTO 'SELLER_SKU' — serve pro item e pra variação.

    Existe porque o Mercado Livre guarda o SKU do vendedor em TRÊS lugares:
      item.seller_custom_field        campo antigo (ERP, formulário velho)
      item.attributes[SELLER_SKU]     formulário atual — é o que o robô NÃO lê
      variations[].(os dois acima)    quando o anúncio tem variação
    Devolve None pra atributo em branco: SKU vazio é pior que SKU nenhum,
    porque casaria com qualquer produto sem SKU do catálogo."""
    for a in (bloco.get("attributes") or []):
        if a.get("id") == "SELLER_SKU":
            return (a.get("value_name") or "").strip() or None
    return None
def raio_x(item_id, it, access):
    """0) RAIO-X: TUDO que dá pra consultar do anúncio pela API + MARGEM ATUAL.
    Base pra bolar estratégia: preço venda x lista, todos os preços, tarifa detalhada, frete,
    concorrência (price_to_win + catálogo), demanda (visitas/vendidos) e a margem de HOJE."""
    print("\n################ 0) RAIO-X COMPLETO DO ANÚNCIO ################", flush=True)
    if not isinstance(it, dict):
        it = {}
    lista = it.get("price")
    cat = it.get("category_id")
    ltid = it.get("listing_type_id")
    sku = it.get("seller_sku") or it.get("seller_custom_field")
    # [SKU] ONDE ESTÁ O SKU — os 3 lugares possíveis, e o robô só olha 2.
    # Motivo: a IRMAOS_BROTHERS tem SKU nos anúncios do ML e chegou no banco
    # com ZERO. Zero exato não é "não preencheu", é campo que ninguém leu.
    # Este bloco NÃO muda nada: só imprime os três lugares lado a lado.
    _sku_attr = _attr_sku(it)
    _vars = it.get("variations") or []
    _skus_var = []
    for _v in _vars:
        _s = (_v.get("seller_custom_field") or "").strip() or _attr_sku(_v)
        if _s:
            _skus_var.append(_s)
    print(f"\n  [SKU] seller_custom_field={it.get('seller_custom_field')!r} | "
          f"seller_sku={it.get('seller_sku')!r} | atributo SELLER_SKU={_sku_attr!r} | "
          f"variacoes={len(_vars)} -> {sorted(set(_skus_var))[:6]}", flush=True)
    if not sku and (_sku_attr or _skus_var):
        print("        >>> O ROBO ESTA PERDENDO ESTE SKU: ele existe no anuncio, mas nao "
              "nos dois campos que o robo le hoje.", flush=True)
        if len(set(_skus_var)) > 1:
            print("        >>> ATENCAO: as variacoes tem SKUs DIFERENTES entre si. Um custo "
                  "unico por anuncio seria errado aqui - este caso pede decisao, nao conserto.", flush=True)
    elif not sku:
        print("        >>> este anuncio nao tem SKU em lugar nenhum - e cadastro mesmo.", flush=True)
    catalog_pid = it.get("catalog_product_id")
    ship = it.get("shipping") or {}
    logistic = ship.get("logistic_type")
    smode = ship.get("mode")
    print(f"  lista(price)={brl(lista)} | base_price={brl(it.get('base_price'))} | estoque={it.get('available_quantity')} "
          f"| vendidos={it.get('sold_quantity')} | health={it.get('health')} | catálogo={catalog_pid} "
          f"| logística={logistic}/{smode} | frete_grátis={ship.get('free_shipping')}", flush=True)
    # [A] preço de VENDA atual x lista + promoção associada
    st, sp = _g(f"/items/{item_id}/sale_price?context=channel_marketplace", access)
    amount = regular = ptipo = pid = None
    if isinstance(sp, dict):
        amount = sp.get("amount")
        regular = sp.get("regular_amount")
        m = sp.get("metadata") or {}
        ptipo = m.get("promotion_type")
        pid = m.get("promotion_id")
    tem_promo = (amount is not None and regular is not None and float(amount) < float(regular) - 0.01)
    if tem_promo:
        pct = (1 - float(amount) / float(regular)) * 100 if regular else 0
        print(f"\n  [A] SALE_PRICE: venda ATUAL={brl(amount)} | lista(regular)={brl(regular)} "
              f"-> EM PROMOÇÃO ✅ ({pct:.1f}% OFF) tipo={ptipo} promo_id={pid}", flush=True)
    else:
        print(f"\n  [A] SALE_PRICE: venda ATUAL={brl(amount)} | lista(regular)={brl(regular)} -> SEM promoção", flush=True)
    # [B] TODOS os preços (standard + promotion, com datas e canais)
    st, pr = _g(f"/items/{item_id}/prices", access)
    dump("  [B] /items/{id}/prices — todos os preços (standard e promotion, com vigência/canal)", pr, 3500)
    # [C] tarifa DETALHADA no preço de venda de hoje (sale_fee_details)
    preco_fee = amount or lista
    q = f"/sites/MLB/listing_prices?price={preco_fee}"
    if cat:
        q += f"&category_id={cat}"
    if ltid:
        q += f"&listing_type_id={ltid}"
    if logistic and smode:
        q += f"&logistic_type={logistic}&shipping_mode={smode}"
    st, lp = _g(q, access)
    dump(f"  [C] LISTING_PRICES — tarifa no preço de venda {brl(preco_fee)} (gross = tarifa CHEIA, sem abater promoção)", lp, 2500)
    # [D] frete estimado
    frete, forg = rec.frete_de(sku, item_id, access)
    print(f"\n  [D] FRETE estimado: {brl(frete)} ({forg})", flush=True)
    # [E] concorrência / buy box
    st, ptw = _g(f"/items/{item_id}/price_to_win?version=v2", access)
    dump("  [E] PRICE_TO_WIN — concorrência / preço pra ganhar a caixa", ptw, 2500)
    # [F] concorrentes no catálogo (outros vendedores no mesmo produto)
    if catalog_pid:
        st, comp = _g(f"/products/{catalog_pid}/items?limit=10", access)
        dump("  [F] CATÁLOGO — ofertas dos concorrentes (/products/{id}/items)", comp, 3000)
    else:
        print("\n  [F] CATÁLOGO: anúncio NÃO é de catálogo (sem catalog_product_id) — sem concorrência de catálogo", flush=True)
    # [G] demanda: visitas nos últimos 30 dias
    st, vis = _g(f"/items/{item_id}/visits/time_window?last=30&unit=day", access)
    dump("  [G] VISITAS — demanda (últimos 30 dias)", vis, 1500)
    # [H] MARGEM ATUAL (a conta que interessa)
    custo = rec.custo_efetivo(item_id, sku)
    try:
        piso, grupo = rec.margem_minima_do(sku)
    except Exception:
        piso, grupo = None, None
    print("\n  [H] MARGEM ATUAL (no preço de venda de hoje):", flush=True)
    if amount is None or custo is None:
        print(f"      não dá pra fechar (venda={brl(amount)} | custo={brl(custo)} | piso={piso}% {grupo})", flush=True)
    else:
        # meli_percentage: se a promoção ativa é cofinanciada, abate a tarifa; senão 0 (desconto próprio)
        mp = 0.0
        for o in (rec.ofertas_do_item(item_id, access) or []):
            if isinstance(o, dict) and rec.eh_ativa(o) and o.get("meli_percentage"):
                try:
                    mp = float(o.get("meli_percentage"))
                    break
                except (TypeError, ValueError):
                    pass
        com = rec.comissao(round(float(amount), 2), cat, ltid, access) or 0
        reducao = mp / 100.0 * float(regular or amount)
        tarifa = max(com - reducao, 0)
        recebe = float(amount) - tarifa - (frete or 0)
        margem = (recebe - custo) / float(amount) * 100
        ttxt = "CHEIA (desconto próprio)" if mp == 0 else f"com abate de {mp}% do ML"
        print(f"      venda {brl(amount)} − tarifa {brl(tarifa)} [{ttxt}] − frete {brl(frete)} − custo {brl(custo)} = recebe {brl(recebe)}", flush=True)
        print(f"      -> MARGEM ATUAL = {margem:.1f}%  (piso {piso}% · grupo {grupo})  "
              f"{'✅ acima do piso' if (piso is None or margem >= piso) else '⚠️ ABAIXO do piso'}", flush=True)
        if tem_promo and mp == 0 and ptipo not in ("PRICE_DISCOUNT", "custom", "CUSTOM", None):
            print(f"      OBS: promo '{ptipo}' pode ser cofinanciada, mas não achei meli% na oferta ativa — margem acima está com tarifa CHEIA (conservadora; a real pode ser melhor).", flush=True)
    # [I] REFERÊNCIA DE PREÇO DO ML (o motor de recomendação do próprio Mercado Livre)
    st, ref = _g(f"/suggestions/items/{item_id}/details", access)
    if isinstance(ref, dict) and isinstance(ref.get("status"), str):
        sug = (ref.get("suggested_price") or {}).get("amount")
        low = (ref.get("lowest_price") or {}).get("amount")
        cur = (ref.get("current_price") or {}).get("amount")
        costs = ref.get("costs") or {}
        print(f"\n  [I] REFERÊNCIA DE PREÇO (ML): status={ref.get('status')} | atual={brl(cur)} | "
              f"SUGERIDO={brl(sug)} | mínimo mercado={brl(low)} | dif={ref.get('percent_difference')}% | aplicável={ref.get('applicable_suggestion')}", flush=True)
        print(f"      custos que o ML calcula: tarifa={brl(costs.get('selling_fees'))} | frete={brl(costs.get('shipping_fees'))}", flush=True)
        pd = ref.get("promotion_detail")
        if pd:
            print(f"      >>> OPORTUNIDADE (item parado): campanha {pd.get('campaign_name')} {pd.get('discount_percent')}% "
                  f"[{pd.get('promotion_id')}] {pd.get('campaign_start_date')}..{pd.get('campaign_end_date')} (motivo {pd.get('unhealthy_reason')})", flush=True)
        graf = (ref.get("metadata") or {}).get("graph") or []
        if graf:
            print("      similares no mercado (preço | vendidos):", flush=True)
            for g in graf[:6]:
                p = (g.get("price") or {}).get("amount")
                info = g.get("info") or {}
                print(f"        - {brl(p)} | vendidos {info.get('sold_quantity')} | {(info.get('title') or '')[:34]}", flush=True)
    else:
        print(f"\n  [I] REFERÊNCIA DE PREÇO (ML): sem referência pra este item (status HTTP {st})", flush=True)
    # [J] AUTOMAÇÃO DE PREÇO (item automatizado BLOQUEIA PUT de preço a partir de 18/03/2026)
    tags = it.get("tags") or []
    automatizado_tag = "dynamic_standard_price" in tags
    st, aut = _g(f"/pricing-automation/items/{item_id}/automation", access)
    st2, rules = _g(f"/pricing-automation/items/{item_id}/rules", access)
    print(f"\n  [J] AUTOMAÇÃO DE PREÇO: tag dynamic_standard_price={automatizado_tag}", flush=True)
    if isinstance(aut, dict) and aut.get("status") in ("ACTIVE", "PAUSED"):
        sd = aut.get("status_detail") or {}
        print(f"      ⚠️ AUTOMATIZADO ({aut.get('status')}) regra={(aut.get('item_rule') or {}).get('rule_id')} "
              f"min={brl(aut.get('min_price'))} max={brl(aut.get('max_price'))}"
              + (f" | pausada por {sd.get('cause')}" if sd.get('cause') else ""), flush=True)
        print("      >>> NÃO mexer no preço via PUT nesse item — será rejeitado/ignorado.", flush=True)
    else:
        rl = (rules.get("rules") if isinstance(rules, dict) else None) or []
        print(f"      sem automação atribuída. Regras que o item aceitaria: {[r.get('rule_id') for r in rl] or 'nenhuma'}", flush=True)
    # [K] USER PRODUCT (MLBU) + condições de venda irmãs (mesmo produto, preços/ tipos diferentes)
    upid = it.get("user_product_id")
    if upid:
        st, up = _g(f"/user-products/{upid}", access)
        nome_up = up.get("name") if isinstance(up, dict) else None
        st, irmas = _g(f"/users/{it.get('seller_id')}/items/search?user_product_id={upid}", access)
        ids = (irmas.get("results") if isinstance(irmas, dict) else None) or []
        print(f"\n  [K] USER PRODUCT: {upid} '{nome_up}' | family_id={it.get('family_id')} | condições de venda (irmãs): {len(ids)}", flush=True)
        for x in ids[:12]:
            print(f"        - {x}{'  <- ESTE' if str(x) == str(item_id) else ''}", flush=True)
    else:
        print("\n  [K] USER PRODUCT: item ainda no modelo antigo (sem user_product_id)", flush=True)
    print("\n################ FIM DO RAIO-X ################", flush=True)
def _sugestao_fresca(item_id, sid):
    """Lê a recomendação MAIS NOVA e VIVA do robô pra o item (status != 'aplicada')."""
    try:
        rows = (sb.table("repricer_sugestoes")
                .select("acao,promocao_id,promocao_tipo,promocao_nome,promocao_ref_id,status,criado_em")
                .eq("seller_id", str(sid)).eq("item_id", item_id)
                .neq("status", "aplicada").order("criado_em", desc=True).limit(1).execute().data) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"  (não consegui ler a sugestão: {e})", flush=True)
        return None
def simular_entrada(item_id, access, sid):
    """SÓ-LEITURA: reproduz a decisão do aplicador pra ENTRAR na promoção recomendada e mostra o
    POST exato que ele enviaria — sem tocar no ML nem gravar no banco. Diz GO/NO-GO e o motivo."""
    print("\n===== 5) SIMULAÇÃO DE ENTRADA (só leitura — NÃO altera NADA) =====", flush=True)
    sug = _sugestao_fresca(item_id, sid)
    if not sug:
        print("  (sem sugestão viva pra este item — nada a simular)", flush=True)
        return
    acao = (sug.get("acao") or "").lower()
    tipo = (sug.get("promocao_tipo") or "").upper()
    pid = sug.get("promocao_id")
    nome = sug.get("promocao_nome")
    print(f"  Recomendação atual do robô: {acao.upper()} -> {nome} [{tipo}/{pid}]", flush=True)
    if acao == "manter":
        print("  >>> NO-GO (nem precisa): a sugestão é MANTER — o item já está na melhor promoção.", flush=True)
        return
    if acao not in ("entrar", "trocar"):
        print(f"  >>> ação '{acao}' não é entrada — nada a simular.", flush=True)
        return
    ofertas = rec.ofertas_do_item(item_id, access)
    if not isinstance(ofertas, list):
        ofertas = []
    cand = achar_candidato(ofertas, {"promocao_tipo": tipo, "promocao_id": pid, "promocao_nome": nome})
    if not cand:
        ja = any((o.get("type") or "").upper() == tipo and rec.eh_ativa(o) for o in ofertas)
        print("  >>> NO-GO: " + ("o item JÁ ESTÁ ATIVO nessa promoção — não precisa entrar ✓"
                                 if ja else "o candidato não está mais disponível (rode a sugestão de novo)"), flush=True)
        return
    # (a) vigência da CAMPANHA
    if not rec.cand_vigente(cand, access):
        print("  >>> NO-GO: a CAMPANHA não está vigente (programada/futura/encerrada) — o aplicador NÃO entraria.", flush=True)
        return
    # (b) vigência do ITEM (mostra a data — é aqui que pega a janela futura, ex.: 22/jul)
    try:
        stx, dix = rec.get(f"/seller-promotions/promotions/{cand.get('id')}/items"
                           f"?promotion_type={tipo}&item_id={item_id}&app_version=v2", access)
        for it in ((dix.get("results") if isinstance(dix, dict) else None) or []):
            if str(it.get("id")) == str(item_id):
                ini = it.get("start_date")
                print(f"  vigência do ITEM na promoção: start_date={ini} | end_date={it.get('end_date')}", flush=True)
                if ini and not rec._vigente({"start_date": ini}):
                    print(f"  >>> NO-GO: a vigência do ITEM é FUTURA (começa {str(ini)[:16]}) — "
                          f"o aplicador BLOQUEIA (não entra em promoção que ainda não começou).", flush=True)
                    return
                break
    except Exception as e:
        print(f"  (não consegui checar a vigência do item: {e})", flush=True)
    # (c) margem (re-checagem, igual ao aplicador)
    st, it = rec.get(f"/items/{item_id}", access)
    if not isinstance(it, dict):
        print("  >>> NO-GO: não consegui ler o item pra recalcular a margem.", flush=True)
        return
    ltid = it.get("listing_type_id")
    cat = it.get("category_id")
    sku = it.get("seller_sku") or it.get("seller_custom_field")
    custo = rec.custo_efetivo(item_id, sku)
    if custo is None:
        print("  >>> NO-GO: sem custo cadastrado — o aplicador não avalia margem sem custo.", flush=True)
        return
    frete, _ = rec.frete_de(sku, item_id, access)
    piso, grupo = rec.margem_minima_do(sku)
    # LIGHTNING: acha o maior desconto que mantém o piso (preço crível) — idêntico ao aplicador
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
                lo, hi = max(mn, 0.5), mx
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
        print("  >>> NO-GO: não deu pra avaliar a oferta agora.", flush=True)
        return
    print(f"  margem recalculada: {ev['margem']:.2f}%  (piso {piso:.0f}% · grupo {grupo})", flush=True)
    if ev["margem"] < piso:
        print(f"  >>> NO-GO: margem {ev['margem']:.2f}% ABAIXO do piso {piso:.0f}% — o aplicador NÃO entraria.", flush=True)
        return
    # (d) START_DATE: cofinanciadas exigem data no POST; pega do detalhe da promoção (formato local, não no passado)
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL", "DEAL") and not cand.get("start_date"):
        pd = rec._promo_detalhe(cand.get("id"), tipo, access)
        if isinstance(pd, dict):
            hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d")
            ini = str(pd.get("start_date") or "")[:10]
            fim = str(pd.get("finish_date") or "")[:10]
            if ini and ini < hoje:
                ini = hoje
            cand = dict(cand)
            if ini:
                cand["start_date"] = ini + "T00:00:00"
            if fim:
                cand["finish_date"] = fim + "T23:59:59"
    corpo = corpo_post(tipo, cand, ev["pb"])
    if not corpo:
        print(f"  >>> NO-GO: não montei o corpo do POST pro tipo {tipo}.", flush=True)
        return
    print("  >>> GO ✓ — o aplicador ENVIARIA este POST (aqui NÃO enviei nada):", flush=True)
    print(f"      POST /seller-promotions/items/{item_id}?app_version=v2", flush=True)
    print("      BODY " + json.dumps(corpo, ensure_ascii=False), flush=True)
    print(f"      (margem prevista {ev['margem']:.2f}% · preço comprador R${ev['pb']:.2f})", flush=True)
    if acao == "trocar":
        antigas = [o for o in ofertas if rec.eh_ativa(o) and o.get("id") != cand.get("id")]
        nomes = ", ".join((o.get("name") or o.get("type") or "?") for o in antigas) or "nenhuma"
        print(f"      + DEPOIS (só se o POST voltar 200) sairia das outras ativas: {nomes}", flush=True)
def main():
    if not ITEM:
        print("!! defina ITEM_ID", flush=True)
        return
    access, SID = None, None
    for seller_id, refresh in rec.contas():
        try:
            a, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"  token de {seller_id} falhou: {e}", flush=True)
            continue
        if not SELLER or str(sid) == SELLER:
            access, SID = a, str(sid)
            if SELLER:
                break
    if not access:
        print("!! não consegui token", flush=True)
        return
    print(f"################ DIAGNÓSTICO {ITEM} — conta {SID} ################", flush=True)
    # carrega custos/grupos ANTES (pro raio-x e a simulação calcularem margem)
    mm = (os.environ.get("MARGEM_MIN") or "").strip()
    if mm:
        try:
            rec.MARGEM_PADRAO = float(mm.replace(",", "."))
        except ValueError:
            pass
    # PASSA A CONTA: sem o argumento, o preload carrega o catálogo da CASA
    # mesmo diagnosticando anúncio de cliente - e aí um SKU coincidente
    # mostraria o SEU custo como se fosse o dele. Diagnóstico que mente é
    # pior que diagnóstico nenhum.
    rec.preload(SID)
    # 1) o anúncio — COMPLETO (o raio-x usa vários campos: catálogo, shipping, vendidos, health...)
    st, it = rec.get(f"/items/{ITEM}", access)
    dump("1) ANÚNCIO (/items/{id})", it, 2500)
    # 0) RAIO-X — tudo que dá pra consultar do anúncio + MARGEM ATUAL
    raio_x(ITEM, it, access)
    # 2) mapa do item — CONTANDO quantos vêm (pra ver se está truncado/paginado)
    st, of = rec.get(f"/seller-promotions/items/{ITEM}?app_version=v2", access)
    n = len(of) if isinstance(of, list) else "?"
    print(f"\n===== 2) /seller-promotions/items/{{id}} — devolveu {n} promoções (started = participando) =====", flush=True)
    dump("   conteúdo", of)
    # 3) todas as campanhas do vendedor
    todas = rec.promocoes_do_vendedor(SID, access)
    print(f"\n===== 3) CAMPANHAS DA CONTA: {len(todas)} =====", flush=True)
    for p in todas:
        print(f"  {(p.get('name') or '?')} [{p.get('type')}/{p.get('status')}/{p.get('id')}]", flush=True)
    # 4) SONDA a participação do item em CADA campanha started/pending, por várias vias + paginação
    print("\n===== 4) SONDA DA PARTICIPAÇÃO (item_id x status=started x candidate x active x PAGINANDO) =====", flush=True)
    ativas_reais = []
    for p in todas:
        if (p.get("status") or "").lower() not in ("started", "pending"):
            continue
        pid, tipo = p.get("id"), (p.get("type") or "")
        vias = sonda_campanha(pid, tipo, ITEM, access)
        apareceu = any(v[0] for v in vias.values())
        if not apareceu:
            continue
        resumo = " | ".join(f"{k}={v[0]}" for k, v in vias.items())
        print(f"\n  >>> {(p.get('name') or '?')} [{tipo}/{pid}]", flush=True)
        print(f"      vias: {resumo}", flush=True)
        started_via = next((v for v in vias.values() if v[0] == "started"), None)
        if started_via:
            oid = started_via[1]
            ativas_reais.append((p.get("name"), tipo, pid, oid))
            print(f"      >>> PARTICIPAÇÃO ATIVA (started). COMO SAIR: {como_sair(ITEM, tipo, pid, oid, 'started')}", flush=True)
    print("\n----- RESUMO: participações ATIVAS (started) encontradas -----", flush=True)
    if ativas_reais:
        for nome, tipo, pid, oid in ativas_reais:
            print(f"  ✓ {nome} [{tipo}/{pid}] offer={oid}", flush=True)
    else:
        print("  (nenhuma via encontrou o item como 'started')", flush=True)
    # 5) SIMULAÇÃO DE ENTRADA (só leitura) — reproduz o aplicador e mostra o POST exato
    simular_entrada(ITEM, access, SID)
    print("\n################ FIM — nada foi alterado (só leitura) ################", flush=True)
if __name__ == "__main__":
    main()
