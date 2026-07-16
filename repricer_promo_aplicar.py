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
ITEM_FILTRO = (os.environ.get("ITEM_ID") or "").strip()        # vazio = todos da fila; setado = só esse anúncio
MAX_APLICAR = int(os.environ.get("MAX_APLICAR", "0"))          # 0 = sem limite
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


def corpo_post(tipo, cand, preco_alvo):
    """Monta o corpo do POST conforme o tipo (docs seller-promotions v2)."""
    tipo = (tipo or "").upper()
    if tipo in ("SMART", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL"):
        # cofinanciada automatizada / preços competitivos: precisa do offer_id do candidato
        oid = cand.get("ref_id") or cand.get("offer_id") or cand.get("candidate_id") or cand.get("id")
        return {"promotion_id": cand.get("id"), "promotion_type": tipo, "offer_id": oid}
    if tipo == "LIGHTNING":
        st = cand.get("stock") or {}
        estoque = st.get("min") or st.get("remaining_stock") or 1
        return {"deal_price": round(preco_alvo, 2), "stock": int(estoque), "promotion_type": "LIGHTNING"}
    if tipo == "DEAL":
        return {"promotion_id": cand.get("id"), "promotion_type": "DEAL", "deal_price": round(preco_alvo, 2)}
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


def remover_oferta(iid, o, access):
    """Remove UMA oferta ativa do item (usada no 'trocar', pra sair das antigas)."""
    otipo = (o.get("type") or "").upper()
    if otipo in ("LIGHTNING", "DOD"):
        return None, "relâmpago/DOD não sai por API"
    qs = f"?promotion_type={otipo}&app_version=v2"
    if o.get("id"):
        qs += f"&promotion_id={o['id']}"
    if o.get("ref_id"):
        qs += f"&offer_id={o['ref_id']}"
    sc, _ = req_delete(f"/seller-promotions/items/{iid}{qs}", access)
    return sc, otipo


def processar(fila, access):
    iid = fila["item_id"]
    tipo = (fila.get("promocao_tipo") or "").upper()
    acao = fila.get("acao")
    if acao not in ("entrar", "trocar"):
        gravar(fila["id"], {"status": "erro", "resultado": f"ação '{acao}' não aplicável aqui"})
        return "acao_invalida"
    if tipo not in TIPOS_OK:
        gravar(fila["id"], {"status": "erro", "resultado": f"tipo {tipo} ainda não suportado pelo aplicador"})
        return "tipo_nao_suportado"

    ofertas = rec.ofertas_do_item(iid, access)
    if not isinstance(ofertas, list) or not ofertas:
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

    # ---- RE-CHECAGEM de margem (a trava de segurança) ----
    st, it = rec.get(f"/items/{iid}", access)
    if not isinstance(it, dict):
        gravar(fila["id"], {"status": "erro", "resultado": "não consegui ler o item pra recalcular margem"})
        return "sem_item"
    ltid = it.get("listing_type_id")
    cat = it.get("category_id")
    sku = it.get("seller_sku") or it.get("seller_custom_field") or fila.get("sku")
    custo = rec.custo_de(sku)
    if custo is None:
        gravar(fila["id"], {"status": "erro", "resultado": "sem custo pra recalcular margem"})
        return "sem_custo"
    frete, _ = rec.frete_de(sku, iid, access)
    piso, grupo = rec.margem_minima_do(sku)
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

    # ---- APLICAÇÃO REAL: ordem segura = ENTRA na nova primeiro; só depois SAI das antigas ----
    sc, resp = post(f"/seller-promotions/items/{iid}?app_version=v2", access, corpo)
    ok = sc in (200, 201)
    aviso = ""
    if ok and acao == "trocar" and antigas:
        removidas, falhas = [], []
        for o in antigas:
            scd, rot = remover_oferta(iid, o, access)
            if scd in (200, 201):
                removidas.append(rot)
            else:
                falhas.append(rot)
        if removidas:
            aviso += f" | saiu de: {', '.join(removidas)}"
        if falhas:
            aviso += f" | NÃO saiu de: {', '.join(falhas)} (remova na mão)"
    gravar(fila["id"], {
        "status": "aplicada" if ok else "erro",
        "resultado": (f"OK {sc}{aviso}: {json.dumps(resp)[:340]}" if ok else f"ERRO {sc}: {json.dumps(resp)[:340]}"),
        "preco_aplicado": ev["pb"] if ok else None,
        "margem_aplicada": ev["margem"] if ok else None,
    })
    print(f"  [{'OK' if ok else 'ERRO'}] {acao} {iid} {tipo} {sc}{aviso}", flush=True)
    return "aplicado" if ok else "erro_post"


def main():
    rec.preload()
    q = sb.table("repricer_promo_fila").select("*").eq("status", "aprovada")
    if SELLER_FILTRO:
        q = q.eq("seller_id", SELLER_FILTRO)
    if ITEM_FILTRO:
        q = q.eq("item_id", ITEM_FILTRO)
    fila = (q.execute().data) or []
    if MAX_APLICAR:
        fila = fila[:MAX_APLICAR]
    print(f"{'SIMULAÇÃO (DRY_RUN)' if DRY else 'APLICAÇÃO REAL'} — {len(fila)} item(ns) na fila"
          + (f" | conta {SELLER_FILTRO}" if SELLER_FILTRO else "")
          + (f" | item {ITEM_FILTRO}" if ITEM_FILTRO else ""), flush=True)
    if not fila:
        return

    tokens = {sid: rt for sid, rt in rec.contas()}
    cache_access = {}
    resumo = {}
    for f in fila:
        sid = str(f["seller_id"])
        rt = tokens.get(sid) or tokens.get(int(sid) if sid.isdigit() else sid)
        if not rt:
            gravar(f["id"], {"status": "erro", "resultado": f"sem refresh_token da conta {sid}"})
            resumo["sem_token"] = resumo.get("sem_token", 0) + 1
            continue
        if sid not in cache_access:
            cache_access[sid] = obter_access(rt)
        r = processar(f, cache_access[sid])
        resumo[r] = resumo.get(r, 0) + 1
    print("resumo:", json.dumps(resumo, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
