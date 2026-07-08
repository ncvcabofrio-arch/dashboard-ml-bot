"""
Puxador de DEVOLUCOES / RMA  Mercado Livre -> Supabase
(mesmo estilo do puxador de vendas: refresh token por conta, Supabase,
 Telegram, roda no GitHub Actions)

Filtro da API: a busca de claims exige ao menos um filtro. Usamos
'player_role=respondent' + 'player_user_id' (o vendedor e o respondente da
reclamacao) e cortamos por data no cliente (ultimos DIAS dias).

Modos:
  (sem MODO)  -> rodada normal (ultimos DIAS dias, todas as contas)
  MODO=debug  -> NAO grava nada; testa filtros e mostra resposta crua.
"""
import os
import time
import json
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client
from ml_auth import obter_access

CLIENT_ID     = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH  = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
DIAS          = int((os.environ.get("DIAS_DEVOLUCAO") or os.environ.get("DIAS") or "30"))
SO_SELLER     = os.environ.get("SO_SELLER", "").strip()

TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFICAR = os.environ.get("NOTIFICAR", "1") == "1"
DEBUG     = os.environ.get("MODO", "") == "debug"

API = "https://api.mercadolibre.com"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)
MOTIVO_CACHE = {}


# ---------------- Telegram ----------------
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        print("Aviso: falha ao enviar Telegram:", e)


# ---------------- Mercado Livre ----------------
def renovar_token(refresh_token):
    r = requests.post(API + "/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Falha ao renovar token: " + str(d))
    return d


def ml_get(path, access, tentativas=3):
    r = None
    for i in range(tentativas):
        r = requests.get(API + path,
                         headers={"Authorization": "Bearer " + access}, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        j = r.json()
        if isinstance(j, dict):
            j.setdefault("_http", r.status_code)
        return j
    except Exception:
        return {"_http": r.status_code, "_text": r.text[:300]}


def lista_refresh_tokens():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    tokens = [(c["seller_id"], c["refresh_token"])
              for c in (res.data or []) if c.get("refresh_token")]
    if not tokens and SEED_REFRESH:
        tokens = [(None, SEED_REFRESH)]
    return tokens


# ---------------- Busca de claims/devolucoes ----------------
def _antes_do_corte(date_str, corte):
    if not date_str:
        return False
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt < corte
    except Exception:
        return False


def buscar_claims(access, seller_id):
    """Reclamacoes onde a conta e respondente, dos ultimos DIAS dias."""
    corte = datetime.now(timezone.utc) - timedelta(days=DIAS)
    claims, offset, total = [], 0, 1
    while offset < total and offset < 3000:
        path = ("/post-purchase/v1/claims/search"
                f"?player_role=respondent&player_user_id={seller_id}"
                f"&sort=date_created:desc&limit=50&offset={offset}")
        data = ml_get(path, access)
        lote = data.get("data") or data.get("results") or []
        pag = data.get("paging") or {}
        total = pag.get("total", len(lote))
        if not lote:
            break
        parou = False
        for c in lote:
            if _antes_do_corte(c.get("date_created"), corte):
                parou = True
                continue
            claims.append(c)
        if parou:      # lista vem do mais novo pro mais velho -> ja passou do periodo
            break
        offset += 50
        time.sleep(0.3)
    return claims


def detalhe_motivo(reason_id, access):
    if not reason_id:
        return None
    if reason_id in MOTIVO_CACHE:
        return MOTIVO_CACHE[reason_id]
    d = ml_get(f"/post-purchase/v1/claims/reasons/{reason_id}", access)
    txt = None
    if isinstance(d, dict):
        txt = d.get("detail") or d.get("name") or d.get("description")
    MOTIVO_CACHE[reason_id] = txt
    return txt


def order_de_shipment(shipment_id, access):
    """Quando o claim aponta pra um envio, descobre o pedido dele."""
    if not shipment_id:
        return None
    sh = ml_get(f"/shipments/{shipment_id}", access)
    if isinstance(sh, dict):
        oid = sh.get("order_id")
        if oid:
            return str(oid)
        # alguns envios trazem a lista de pedidos
        for o in (sh.get("order_ids") or sh.get("orders") or []):
            if isinstance(o, dict) and o.get("id"):
                return str(o["id"])
    return None


def dados_do_pedido(order_id, access):
    if not order_id:
        return {}
    o = ml_get(f"/orders/{order_id}", access)
    if not isinstance(o, dict) or not o.get("id"):
        return {}
    it = (o.get("order_items") or [{}])[0]
    item = it.get("item") or {}
    buyer = o.get("buyer") or {}
    return {
        "item_id": item.get("id"),
        "sku": item.get("seller_sku") or item.get("seller_custom_field"),
        "titulo": item.get("title"),
        "quantidade": it.get("quantity"),
        "valor": o.get("total_amount"),
        "comprador": buyer.get("nickname"),
    }


def claim_para_linha(c, access, seller_id):
    resource = c.get("resource")
    resource_id = c.get("resource_id")

    # descobre o pedido: direto (resource=order) ou via envio (resource=shipment)
    order_id = str(resource_id) if resource == "order" and resource_id else None
    if not order_id:
        for ent in (c.get("related_entities") or []):
            if isinstance(ent, dict) and ent.get("type") == "order":
                order_id = str(ent.get("id"))
                break
    if not order_id and resource == "shipment" and resource_id:
        try:
            order_id = order_de_shipment(resource_id, access)
        except Exception:
            order_id = None

    # comprador (quem reclamou = complainant)
    comprador_id = None
    for p in (c.get("players") or []):
        if p.get("role") == "complainant":
            comprador_id = p.get("user_id")
            break

    res = c.get("resolution") or {}
    reason_id = c.get("reason_id")
    linha = {
        "claim_id": str(c.get("id")),
        "seller_id": str(seller_id) if seller_id else None,
        "order_id": order_id,
        "pack_id": str(c.get("pack_id")) if c.get("pack_id") else None,
        "tipo": c.get("type"),
        "estagio": c.get("stage"),
        "status_ml": c.get("status"),
        "reason_id": str(reason_id) if reason_id else None,
        "motivo": detalhe_motivo(reason_id, access),
        "resource": resource,
        "resource_id": str(resource_id) if resource_id else None,
        "comprador_id": str(comprador_id) if comprador_id else None,
        "resolucao_motivo": res.get("reason"),
        "resolucao_beneficiado": ",".join(res.get("benefited") or []) or None,
        "resolucao_fechado_por": res.get("closed_by"),
        "resolucao_em": res.get("date_created"),
        "aplicou_cobertura": res.get("applied_coverage"),
        "data_abertura": c.get("date_created"),
        "ml_atualizado_em": c.get("last_updated"),
    }
    if order_id:
        try:
            linha.update({k: v for k, v in dados_do_pedido(order_id, access).items()
                          if v is not None})
        except Exception as e:
            print("  aviso: falha ao enriquecer pedido", order_id, str(e)[:80])
    return linha


def retorno_do_claim(claim_id, access):
    d = ml_get(f"/post-purchase/v2/claims/{claim_id}/returns", access)
    if not isinstance(d, dict) or d.get("_http") in (403, 404):
        return None
    if not (d.get("status") or d.get("shipments") or d.get("subtype")):
        return None
    # o transporte vem em 'shipments' (array); pega o envio de retorno
    shp = {}
    for s in (d.get("shipments") or []):
        if isinstance(s, dict) and s.get("type") == "return":
            shp = s
            break
    if not shp and d.get("shipments"):
        shp = d["shipments"][0] if isinstance(d["shipments"][0], dict) else {}
    return {
        "claim_id": str(claim_id),
        "return_status": d.get("status"),            # delivered / expired / shipped...
        "subtype": d.get("subtype"),                 # return_total / return_partial
        "shipment_id": str(shp.get("shipment_id")) if shp.get("shipment_id") else None,
        "tracking": shp.get("tracking_number"),
        "tracking_status": shp.get("status"),        # delivered / shipped / cancelled...
        "review_status": d.get("status_money"),      # refunded / retained (status do dinheiro)
        "ml_atualizado_em": d.get("last_updated") or d.get("date_created"),
    }


# ---- auto-avanco das etapas iniciais a partir do status do ML ----
ETAPAS_ORDER = ["aberto", "em_transito", "coleta", "recebido_triagem", "para_assistencia",
                "reparo_interno", "retornou_assistencia", "desfecho", "cancelada", "encerrado"]
ETAPA_IDX = {e: i for i, e in enumerate(ETAPAS_ORDER)}


def etapa_do_retorno(ret):
    """Deduz a etapa inicial a partir do status do envio de retorno do ML."""
    if not ret:
        return None
    st = (ret.get("tracking_status") or "").lower()
    top = (ret.get("return_status") or "").lower()
    if st == "delivered" or top == "delivered":
        return "recebido_triagem"
    if st in ("shipped", "in_transit", "in_hub", "in_warehouse", "handling",
              "ready_to_ship", "pending", "picked_up", "out_for_delivery"):
        return "em_transito"
    # devolucoes que morreram (produto nao voltou) -> sai do Aberto
    if top in ("cancelled", "expired", "failed", "closed", "not_delivered") \
       or st in ("cancelled", "not_delivered"):
        return "cancelada"
    return None


def etapas_atuais(claim_ids):
    if not claim_ids:
        return {}
    out = {}
    for i in range(0, len(claim_ids), 200):
        lote = claim_ids[i:i + 200]
        rows = (sb.table("devolucoes").select("claim_id, etapa_interna")
                .in_("claim_id", lote).execute().data) or []
        for r in rows:
            out[r["claim_id"]] = r.get("etapa_interna")
    return out


# ---------------- Gravacao (preserva campos internos) ----------------
def status_atuais(claim_ids):
    if not claim_ids:
        return {}
    out = {}
    for i in range(0, len(claim_ids), 200):
        lote = claim_ids[i:i + 200]
        rows = (sb.table("devolucoes").select("claim_id, status_ml")
                .in_("claim_id", lote).execute().data) or []
        for r in rows:
            out[r["claim_id"]] = r.get("status_ml")
    return out


def gravar_devolucoes(linhas):
    if not linhas:
        return 0
    antes = status_atuais([l["claim_id"] for l in linhas])
    hist = []
    for l in linhas:
        old = antes.get(l["claim_id"])
        new = l.get("status_ml")
        if l["claim_id"] in antes and old != new:
            hist.append({"claim_id": l["claim_id"], "campo": "status_ml",
                         "de_valor": old, "para_valor": new, "por": "puxador"})
    for i in range(0, len(linhas), 200):
        sb.table("devolucoes").upsert(linhas[i:i + 200], on_conflict="claim_id").execute()
    if hist:
        for i in range(0, len(hist), 200):
            sb.table("devolucoes_historico").insert(hist[i:i + 200]).execute()
    return len(linhas)


def gravar_retornos(retornos):
    retornos = [r for r in retornos if r]
    if not retornos:
        return 0
    for i in range(0, len(retornos), 200):
        sb.table("devolucoes_retornos").upsert(
            retornos[i:i + 200], on_conflict="claim_id").execute()
    return len(retornos)


# ---------------- Notificacao ----------------
def notificar_novas():
    if not TG_TOKEN or not TG_CHAT:
        return
    novas = (sb.table("devolucoes").select("*")
             .eq("notificado", False).execute().data) or []
    if not novas:
        return
    contas = {c["seller_id"]: (c.get("apelido") or c["seller_id"])
              for c in (sb.table("contas").select("seller_id, apelido").execute().data or [])}
    if len(novas) > 15:
        tg_send(f"↩️ <b>{len(novas)} devolucoes novas!</b> Confira o painel de RMA.")
    else:
        for d in novas:
            conta = contas.get(d.get("seller_id"), d.get("seller_id") or "-")
            titulo = d.get("titulo") or "(produto)"
            motivo = d.get("motivo") or d.get("reason_id") or "-"
            flag = " 📦(produto volta)" if d.get("tem_retorno") else ""
            tg_send("↩️ <b>Nova devolucao!</b>" + flag + "\n"
                    f"Conta: {conta}\n"
                    f"Produto: {titulo}\n"
                    f"Pedido: {d.get('order_id') or '-'}\n"
                    f"Motivo: {motivo}\n"
                    f"Status ML: {d.get('status_ml') or '-'}")
    ids = [d["claim_id"] for d in novas]
    for i in range(0, len(ids), 200):
        sb.table("devolucoes").update({"notificado": True}) \
            .in_("claim_id", ids[i:i + 200]).execute()


# ---------------- DEBUG: testa filtros e mostra resposta CRUA ----------------
def _dump(rotulo, obj):
    print(f"\n--- {rotulo} ---")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:3000])


def _total(r):
    if isinstance(r, dict):
        lote = r.get("data") or r.get("results") or []
        pag = r.get("paging") or {}
        return pag.get("total", len(lote)), lote
    return 0, []


def rodar_debug():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token em 'contas'.")
    print(f"Contas a testar: {len(tokens)}")
    achou = None
    base = "/post-purchase/v1/claims/search"
    for seller_id, refresh in tokens:
        try:
            d = renovar_token(refresh)
        except Exception as e:
            print(f"[{seller_id}] falha ao renovar token: {e}")
            continue
        access = d["access_token"]
        sid = str(d.get("user_id") or seller_id)
        print(f"\n================ CONTA {sid} ================")
        url = f"{base}?player_role=respondent&player_user_id={sid}&limit=5"
        r = ml_get(url, access)
        tot, lote = _total(r)
        print(f"  respondent -> total={tot}  http={r.get('_http')}")
        if lote:
            c0 = lote[0]
            cid = c0.get("id")
            _dump("JSON CRU DO 1o CLAIM", c0)
            _dump("RETURN /post-purchase/v2/claims/{id}/returns",
                  ml_get(f"/post-purchase/v2/claims/{cid}/returns", access))
            _dump("LINHA que seria gravada em 'devolucoes'",
                  claim_para_linha(c0, access, sid))
            achou = True
            break
    if not achou:
        print("\n>>> Nenhum claim no periodo/filtro em nenhuma conta.")


# ---------------- DEBUG RETORNO: acha um retorno real e imprime o JSON ----------------
def rodar_debug_retorno():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token em 'contas'.")
    achados = 0
    for seller_id, refresh in tokens:
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"[{seller_id}] token: {e}"); continue
        claims = buscar_claims(access, sid)
        print(f"[{sid}] {len(claims)} claims; procurando algum com retorno fisico...", flush=True)
        for i, c in enumerate(claims):
            if i >= 120:  # cap por conta pra nao demorar
                break
            cid = c.get("id")
            d = ml_get(f"/post-purchase/v2/claims/{cid}/returns", access)
            http = d.get("_http") if isinstance(d, dict) else None
            tem = isinstance(d, dict) and http not in (403, 404) and (
                d.get("data") or d.get("status") or d.get("shipping") or d.get("id") or d.get("subtype"))
            if tem:
                print(f"\n===== RETORNO REAL - claim {cid} (tipo={c.get('type')}, status={c.get('status')}, stage={c.get('stage')}) =====", flush=True)
                print(json.dumps(d, indent=2, ensure_ascii=False)[:4000], flush=True)
                achados += 1
                if achados >= 2:
                    return
            time.sleep(0.15)
    if not achados:
        print("\n>>> Nenhum claim retornou dados no endpoint de returns.", flush=True)
        print(">>> Pode ser que as devolucoes com produto de volta usem outro recurso,", flush=True)
        print(">>> ou nao houve retorno fisico no periodo. Aumente DIAS_DEVOLUCAO e tente.", flush=True)


# ---------------- DEBUG CASO: despeja TUDO de um pedido especifico ----------------
def _lista_pedidos():
    raw = os.environ.get("PEDIDO_DEBUG", "")
    for sep in (",", ";", "\n", "\t"):
        raw = raw.replace(sep, " ")
    return [x for x in raw.split() if x]


def rodar_debug_caso():
    oids = _lista_pedidos()
    if not oids:
        print("Defina o(s) pedido(s) no campo 'pedido' (separe por virgula ou espaco).")
        return
    accs = []
    for seller_id, refresh in lista_refresh_tokens():
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
            accs.append((sid, access))
        except Exception as e:
            print(f"[{seller_id}] token: {e}")
    for oid in oids:
        achou = False
        for sid, access in accs:
            r = ml_get(f"/post-purchase/v1/claims/search?order_id={oid}", access)
            lote = (r.get("data") or r.get("results") or []) if isinstance(r, dict) else []
            if not lote:
                continue
            print(f"\n################ PEDIDO {oid} na conta {sid} ################")
            for c in lote:
                cid = c.get("id")
                _dump("CLAIM (busca)", c)
                _dump("CLAIM detalhe", ml_get(f"/post-purchase/v1/claims/{cid}", access))
                ret = ml_get(f"/post-purchase/v2/claims/{cid}/returns", access)
                _dump("RETURN (v2)", ret)
                for shp in ((ret.get("shipments") or []) if isinstance(ret, dict) else []):
                    sidp = shp.get("shipment_id")
                    if sidp:
                        _dump(f"SHIPMENT {sidp} detalhe", ml_get(f"/shipments/{sidp}", access))
            achou = True
            break
        if not achou:
            print(f"\n>>> Pedido {oid}: nenhum claim encontrado nas contas.")


# ---------------- DEBUG PEDIDO: pedido + envio + financeiro (sem depender de claim) ----------------
def rodar_debug_pedido():
    oids = _lista_pedidos()
    if not oids:
        print("Defina o(s) pedido(s) no campo 'pedido' (separe por virgula ou espaco).")
        return
    accs = []
    for seller_id, refresh in lista_refresh_tokens():
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
            accs.append((sid, access))
        except Exception as e:
            print(f"[{seller_id}] token: {e}")
    for oid in oids:
        achou = False
        ultima = None
        for sid, access in accs:
            o = ml_get(f"/orders/{oid}", access)
            ultima = o
            if not (isinstance(o, dict) and o.get("id")):
                continue
            print(f"\n################ PEDIDO {oid} na conta {sid} ################")
            _dump("ORDER /orders/{id}", o)
            ship = (o.get("shipping") or {}).get("id")
            if ship:
                _dump(f"SHIPMENT {ship} (status/substatus)", ml_get(f"/shipments/{ship}", access))
            for p in (o.get("payments") or []):
                pid = p.get("id")
                if pid:
                    _dump(f"COLLECTION /collections/{pid} (financeiro)", ml_get(f"/collections/{pid}", access))
            achou = True
            break
        if not achou:
            print(f"\n>>> Pedido {oid} NAO achado via /orders. Resposta crua da ultima conta:")
            print(json.dumps(ultima, indent=2, ensure_ascii=False)[:700] if ultima is not None else "(sem resposta)")
            # tenta como pack: busca os pedidos do pack
            pk = ml_get(f"/packs/{oid}", accs[0][1]) if accs else None
            if isinstance(pk, dict) and pk.get("orders"):
                print(f">>> {oid} parece ser um PACK. Pedidos dentro dele:",
                      [str((x or {}).get("id")) for x in pk.get("orders", [])])


# ---------------- Principal ----------------
def main():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token em 'contas'. Cadastre as contas primeiro.")
    total = 0
    for seller_id, refresh in tokens:
        if SO_SELLER and str(seller_id) != SO_SELLER:
            continue
        access, sid, refresh = obter_access(sb, seller_id, refresh)

        claims = buscar_claims(access, sid)
        if not claims:
            print(f"[{sid}] nenhuma devolucao nos ultimos {DIAS} dias.")
            continue

        linhas, retornos = [], []
        for c in claims:
            linha = claim_para_linha(c, access, sid)
            ret = None
            try:
                ret = retorno_do_claim(linha["claim_id"], access)
            except Exception as e:
                print("  aviso: falha no return", linha["claim_id"], str(e)[:80])
            linha["tem_retorno"] = bool(ret)
            et_ml = etapa_do_retorno(ret)
            # ML aplicou cobertura e nao ha retorno chegando -> voce precisa COLETAR
            if et_ml not in ("recebido_triagem", "em_transito") and linha.get("aplicou_cobertura"):
                et_ml = "coleta"
            linha["_etapa_ml"] = et_ml
            linhas.append(linha)
            if ret:
                retornos.append(ret)
            time.sleep(0.2)

        # decide os avancos ANTES de gravar e REMOVE o campo temporario das linhas,
        # pra ele nao ir pro upsert em lote (senao o Postgres zera a etapa das
        # outras linhas, e ate as edicoes manuais da equipe).
        atuais = etapas_atuais([l["claim_id"] for l in linhas])
        avancos = []
        for l in linhas:
            ml_et = l.pop("_etapa_ml", None)
            if not ml_et:
                continue
            # 'cancelada' so vale se o claim ja FECHOU no ML. Se ainda esta
            # 'opened', o caso continua ativo -> fica no Aberto.
            if ml_et == "cancelada" and (l.get("status_ml") or "").lower() != "closed":
                continue
            cur = atuais.get(l["claim_id"]) or "aberto"
            if cur in ("aberto", "em_transito") and ETAPA_IDX.get(ml_et, 9) > ETAPA_IDX.get(cur, 0):
                avancos.append((l["claim_id"], cur, ml_et))

        # o upsert em lote leva SO colunas do ML -> nunca toca etapa/laudo/custos
        n = gravar_devolucoes(linhas)
        gravar_retornos(retornos)

        # aplica o auto-avanco um a um (update individual, nao sobrescreve nada)
        for cid, cur, ml_et in avancos:
            try:
                sb.table("devolucoes").update({"etapa_interna": ml_et}) \
                    .eq("claim_id", cid).execute()
                sb.table("devolucoes_historico").insert({
                    "claim_id": cid, "campo": "etapa_interna",
                    "de_valor": cur, "para_valor": ml_et, "por": "ml"}).execute()
            except Exception:
                pass
        total += n
        print(f"[{sid}] {n} devolucoes atualizadas em {datetime.now()}")

    if NOTIFICAR:
        try:
            notificar_novas()
        except Exception as e:
            print("Aviso: falha na notificacao:", e)
    print(f"Total: {total} devolucoes processadas.")


if __name__ == "__main__":
    _modo = os.environ.get("MODO", "")
    if _modo == "debug":
        rodar_debug()
    elif _modo == "retorno":
        rodar_debug_retorno()
    elif _modo == "caso":
        rodar_debug_caso()
    elif _modo == "pedido":
        rodar_debug_pedido()
    else:
        main()
