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
from datetime import datetime, timezone
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
        return _com_datas({"promotion_id": cand.get("id"), "promotion_type": "DEAL", "deal_price": round(preco_alvo, 2)}, cand)
    return None


def detalhe_relampago(iid, promotion_id, access):
    """Preço SUGERIDO e faixa crível da relâmpago só vêm na consulta POR PROMOÇÃO.
    Retorna {price(sugerido), min_discounted_price, max_discounted_price, stock:{min,max}} ou None."""
    if not promotion_id:
        return None
    st, d = rec.get(f"/seller-promotions/promotions/{promotion_id}/items"
                    f"?promotion_type=LIGHTNING&item_id={iid}&app_version=v2", access)
    res = (d.get("results") if isinstance(d, dict) else None) or []
    for r in res:
        if r.get("id") == iid:
            return r
    return res[0] if res else None


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


def remover_oferta(iid, o, access):
    """Remove UMA oferta ativa (usada no 'trocar' DEPOIS de entrar na nova — a remoção
    em massa tiraria a nova junto). Relâmpago/DOD não saem por API."""
    otipo = (o.get("type") or "").upper()
    if otipo in ("LIGHTNING", "DOD"):
        return None, f"{otipo}(não sai por API)", None
    qs = f"?promotion_type={otipo}&app_version=v2"
    if o.get("id"):
        qs += f"&promotion_id={o['id']}"
    if o.get("ref_id"):
        qs += f"&offer_id={o['ref_id']}"
    sc, body = req_delete(f"/seller-promotions/items/{iid}{qs}", access)
    return sc, otipo, body


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


def executar_sair(fila, iid, ofertas, access):
    """SAIR: remove a(s) promoção(ões) ATIVA(s) do item (ex.: promo com prejuízo)."""
    ativas = [o for o in ofertas if rec.eh_ativa(o)]
    if not ativas:
        gravar(fila["id"], {"status": "aplicada", "resultado": "não havia promoção ativa (nada a remover)"})
        return "nada_a_remover"
    nomes = [(o.get("name") or o.get("type") or "?") for o in ativas]
    lights = [(o.get("name") or o.get("type") or "?") for o in ativas
              if (o.get("type") or "").upper() in ("LIGHTNING", "DOD")]
    if DRY:
        gravar(fila["id"], {"status": "aprovada", "resultado": f"[SIMULADO] SAIR de: {', '.join(nomes)}"})
        print(f"  [DRY] sair {iid} -> {nomes}", flush=True)
        return "simulado"
    sc, resumo, body = remover_todas(iid, access)
    ok = sc in (200, 201)
    aviso = (f" | relâmpago/DOD não sai por API: {', '.join(lights)} (pause o anúncio)" if lights else "")
    gravar(fila["id"], {
        "status": "aplicada" if ok else "erro",
        "resultado": f"{resumo}{aviso} | {json.dumps(body, ensure_ascii=False)[:300]}",
    })
    print(f"  [{'OK' if ok else 'ERRO'}] sair {iid} {resumo}", flush=True)
    return "saiu" if ok else "erro_sair"


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
        ja = any((o.get("type") or "").upper() == tipo and rec.eh_ativa(o) for o in ofertas)
        msg = "essa promoção já está ATIVA no item" if ja else "candidato aprovado não está mais disponível"
        gravar(fila["id"], {"status": "erro", "resultado": msg})
        return "sem_candidato"
    # trava de vigência (consulta o detalhe da promoção): nunca entrar em promo programada/futura.
    # Fica ANTES da remoção das outras, pra nunca deixar o item sem promoção por causa disso.
    if not rec.cand_vigente(cand, access):
        gravar(fila["id"], {"status": "erro",
                            "resultado": "promoção ainda não está vigente (programada) — não apliquei (item mantido como estava)"})
        return "programada"

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
    # relâmpago: o preço SUGERIDO e a faixa crível só vêm na consulta por promoção.
    # usamos o preço sugerido pelo ML (crível) e o estoque mínimo pedido.
    if tipo == "LIGHTNING":
        det = detalhe_relampago(iid, cand.get("id"), access)
        if det and det.get("price"):
            try:
                px = float(det["price"])                       # preço sugerido pelo ML (crível)
                mx = det.get("max_discounted_price")
                mn = det.get("min_discounted_price")
                if mx is not None:
                    px = min(px, float(mx))
                if mn is not None:
                    px = max(px, float(mn))
                cand = dict(cand)
                cand["price"] = round(px, 2)
                cand["stock"] = det.get("stock") or cand.get("stock")   # {min,max} da relâmpago
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

    corpo = corpo_post(tipo, cand, ev["pb"])
    if not corpo:
        gravar(fila["id"], {"status": "erro", "resultado": f"não montei corpo pro tipo {tipo}"})
        return "sem_corpo"

    sair_de = [(o.get("name") or o.get("type") or "?") for o in antigas] if acao == "trocar" else []
    if DRY:
        plano = (f"TROCA: entra na sugerida e SAI de {sair_de} " if acao == "trocar" else "ENTRADA ")
        gravar(fila["id"], {"status": "aprovada",
                            "resultado": f"[SIMULADO] {plano}| POST {json.dumps(corpo)} | margem prevista {ev['margem']:.1f}%",
                            "preco_aplicado": ev["pb"], "margem_aplicada": ev["margem"]})
        print(f"  [DRY] {acao} {iid} {tipo} -> {json.dumps(corpo)} (margem {ev['margem']:.1f}%)"
              + (f" | sairia de {sair_de}" if sair_de else ""), flush=True)
        return "simulado"

    # ---- APLICAÇÃO REAL ----
    # ORDEM SEGURA: ENTRA na nova PRIMEIRO. Se falhar, NÃO remove nada — o item fica
    # exatamente como estava (nunca sem promoção). Só depois de entrar é que sai das antigas.
    sc, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
    ok = sc in (200, 201)
    aviso = ""
    if ok and acao == "trocar" and antigas:
        removidas, falhas = [], []
        for o in antigas:
            scd, rot, _ = remover_oferta(iid, o, access)   # uma a uma (bulk tiraria a nova junto)
            (removidas if scd in (200, 201) else falhas).append(rot)
        if removidas:
            aviso += f" | saiu de: {', '.join(removidas)}"
        if falhas:
            aviso += f" | não saiu de: {', '.join(falhas)}"
    gravar(fila["id"], {
        "status": "aplicada" if ok else "erro",
        "resultado": (f"OK {sc}{aviso}: {json.dumps(resp, ensure_ascii=False)[:280]}" if ok
                      else f"ERRO {sc} [enviei {json.dumps(corpo, ensure_ascii=False)}]: {json.dumps(resp, ensure_ascii=False)[:210]} — item mantido como estava"),
        "preco_aplicado": ev["pb"] if ok else None,
        "margem_aplicada": ev["margem"] if ok else None,
    })
    return "aplicado" if ok else "erro_post"


def main():
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
    print("resumo:", json.dumps(resumo, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
