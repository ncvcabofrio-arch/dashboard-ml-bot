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


# ---------------- SCAN: mede volume e distribuicao de substatus dos envios ----------------
def rodar_scan():
    import urllib.parse
    from collections import Counter
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)) \
        .strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    PROBLEMA = {"confiscated", "not_delivered", "returned_to_sender", "returning_to_sender",
                "stolen", "lost", "damaged", "delivery_failed", "shipment_stopped"}
    CAP = 500  # limite de envios por conta pra nao demorar demais
    for seller_id, refresh in lista_refresh_tokens():
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"[{seller_id}] token: {e}")
            continue
        print(f"\n================ CONTA {sid} (ultimos {DIAS} dias) ================", flush=True)
        ships = {}
        offset, total = 0, 1
        while offset < total and offset < 3000:
            path = ("/orders/search?seller=" + str(sid) +
                    "&order.date_created.from=" + urllib.parse.quote(desde) +
                    "&sort=date_desc&limit=50&offset=" + str(offset))
            data = ml_get(path, access)
            total = (data.get("paging") or {}).get("total", 0) if isinstance(data, dict) else 0
            for o in (data.get("results", []) if isinstance(data, dict) else []):
                shp = (o.get("shipping") or {}).get("id")
                if shp and shp not in ships:
                    it = (o.get("order_items") or [{}])[0]
                    ships[shp] = (str(o.get("id")), (it.get("item") or {}).get("title"), o.get("status"))
            offset += 50
            if len(ships) >= CAP:
                break
            time.sleep(0.2)
        print(f"  pedidos totais no periodo: {total} | envios distintos coletados: {len(ships)}"
              + ("  (CAP atingido)" if len(ships) >= CAP else ""), flush=True)
        dist = Counter()
        problemas = []
        for i, (shp, (oid, tit, ost)) in enumerate(ships.items()):
            sj = ml_get(f"/shipments/{shp}", access)
            sub = sj.get("substatus") if isinstance(sj, dict) else None
            st = sj.get("status") if isinstance(sj, dict) else None
            dist[f"{st} / {sub}"] += 1
            if sub in PROBLEMA:
                problemas.append((oid, sub, ost, tit))
            if (i + 1) % 100 == 0:
                print(f"    ...{i+1}/{len(ships)}", flush=True)
            time.sleep(0.15)
        print("  --- distribuicao status / substatus ---", flush=True)
        for k, v in dist.most_common():
            print(f"    {v:4d}  {k}")
        print(f"  --- envios com PROBLEMA ({len(problemas)}) ---", flush=True)
        for oid, sub, ost, tit in problemas[:60]:
            print(f"    {oid}  [{sub}]  (venda={ost})  {tit}")


# ---------------- BACKFILL: envios dos pedidos CANCELADOS (do ano) via banco ----------------
PERDA_TOTAL = {"confiscated", "lost", "stolen", "damaged"}
RECUPERAVEL = {"returned", "returning_to_sender", "returned_to_warehouse", "returning_to_hub"}


def _tipo_envio(st, sub):
    s = (sub or "").lower()
    if s in PERDA_TOTAL:
        return "perda_total"
    if s in RECUPERAVEL:
        return "recuperavel"
    if (st or "") == "not_delivered":
        return "nao_entregue_outro"
    if (st or "") == "delivered":
        return "entregue_cancelado"
    return None


def rodar_backfill():
    from collections import Counter
    filtro = os.environ.get("PEDIDO_DEBUG", "").strip().lower()   # opcional: filtra por substatus
    ini = f"{datetime.now(timezone.utc).year}-01-01"
    cancel, passo, off = [], 1000, 0
    while True:
        rows = (sb.table("vendas")
                .select("order_id, shipping_id, titulo, seller_id, valor_unitario, data_aprovacao")
                .eq("status", "cancelled").gte("data_aprovacao", ini)
                .range(off, off + passo - 1).execute().data) or []
        cancel += rows
        if len(rows) < passo:
            break
        off += passo
    print(f"Pedidos CANCELADOS no vendas desde {ini}: {len(cancel)}", flush=True)
    porconta = {}
    for r in cancel:
        porconta.setdefault(str(r.get("seller_id")), []).append(r)
    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    dist = Counter()
    achados = []
    grava = []
    for sid, lst in porconta.items():
        refresh = toks.get(sid)
        if not refresh:
            print(f"[{sid}] sem token, pulei {len(lst)}")
            continue
        try:
            access, sid2, refresh = obter_access(sb, sid, refresh)
        except Exception as e:
            print(f"[{sid}] token: {e}"); continue
        print(f"[{sid}] {len(lst)} cancelados; checando envios...", flush=True)
        seen = set()
        for i, r in enumerate(lst):
            shp = r.get("shipping_id")
            if not shp or shp in seen:
                continue
            seen.add(shp)
            sj = ml_get(f"/shipments/{shp}", access)
            st = sj.get("status") if isinstance(sj, dict) else None
            sub = sj.get("substatus") if isinstance(sj, dict) else None
            dist[f"{st} / {sub}"] += 1
            _tp = _tipo_envio(st, sub)
            if _tp:
                grava.append({"order_id": str(r.get("order_id")), "seller_id": sid,
                              "titulo": r.get("titulo"), "valor": r.get("valor_unitario"),
                              "status_envio": st, "substatus": sub, "tipo": _tp,
                              "shipping_id": str(shp),
                              "data_venda": r.get("data_aprovacao")})
            if filtro:
                if (sub or "").lower() == filtro:
                    print(f"\n===== {r.get('order_id')} | {st}/{sub} | R$ {r.get('valor_unitario')} | {r.get('titulo')} =====", flush=True)
                    _dump("SHIPMENT", sj)
                    o = ml_get(f"/orders/{r.get('order_id')}", access)
                    if isinstance(o, dict):
                        _dump("ORDER (id/status/payments)",
                              {"id": o.get("id"), "status": o.get("status"), "payments": o.get("payments")})
                        for p in (o.get("payments") or []):
                            pid = p.get("id")
                            if pid:
                                _dump(f"COLLECTION {pid}", ml_get(f"/collections/{pid}", access))
            else:
                achados.append((r.get("order_id"), st, sub, r.get("valor_unitario"), r.get("titulo")))
            if (i + 1) % 100 == 0:
                print(f"   ...{i+1}/{len(lst)}", flush=True)
            time.sleep(0.15)
    if grava:
        for i in range(0, len(grava), 200):
            try:
                sb.table("envios_problema").upsert(grava[i:i + 200], on_conflict="order_id").execute()
            except Exception as e:
                print("Aviso: falha ao gravar envios_problema:", str(e)[:150])
        print(f"\nGravados {len(grava)} envios com problema em 'envios_problema'.", flush=True)
    print("\n=== DISTRIBUICAO (status/substatus dos envios cancelados) ===", flush=True)
    for k, v in dist.most_common():
        print(f"  {v:4d}  {k}")
    if not filtro:
        print(f"\n=== LISTA ({len(achados)}) order_id | status/substatus | valor | titulo ===", flush=True)
        for oid, st, sub, val, tit in achados:
            print(f"  {oid} | {st}/{sub} | {val} | {tit}")


# ---------------- Monitor (esteira automatica dos envios com problema) ----------------
def _shipping_do_pedido(order_id, access):
    """Descobre o shipping_id a partir do pedido (quando nao esta guardado)."""
    o = ml_get(f"/orders/{order_id}", access)
    if isinstance(o, dict):
        sh = o.get("shipping") or {}
        if sh.get("id"):
            return str(sh["id"])
    return None


def _scan_cancelados_recentes(dias, access_por_conta):
    """Varre os pedidos CANCELADOS dos ultimos 'dias' dias e devolve os que
    tem envio com problema, no formato pronto pra upsert. Pega casos NOVOS."""
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).date().isoformat()
    rows = (sb.table("vendas")
            .select("order_id, shipping_id, titulo, seller_id, valor_unitario, data_aprovacao")
            .eq("status", "cancelled").gte("data_aprovacao", corte)
            .execute().data) or []
    novos, seen = [], set()
    for r in rows:
        shp = r.get("shipping_id")
        sid = str(r.get("seller_id"))
        if not shp or shp in seen:
            continue
        seen.add(shp)
        access = access_por_conta.get(sid)
        if not access:
            continue
        sj = ml_get(f"/shipments/{shp}", access)
        st = sj.get("status") if isinstance(sj, dict) else None
        sub = sj.get("substatus") if isinstance(sj, dict) else None
        _tp = _tipo_envio(st, sub)
        if _tp:
            novos.append({"order_id": str(r.get("order_id")), "seller_id": sid,
                          "titulo": r.get("titulo"), "valor": r.get("valor_unitario"),
                          "status_envio": st, "substatus": sub, "tipo": _tp,
                          "shipping_id": str(shp), "data_venda": r.get("data_aprovacao")})
        time.sleep(0.12)
    return novos


def rodar_monitor():
    """A cada rodada: (1) acha casos NOVOS nos cancelados recentes;
    (2) re-consulta os casos ABERTOS e atualiza status/substatus (a esteira
    anda sozinha, porque classe/etapa saem da view v_envios_problema);
    (3) dispara alerta no Telegram quando um envio VIRA 'confiscated'."""
    dias = int((os.environ.get("DIAS_DEVOLUCAO") or os.environ.get("DIAS") or "20"))
    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    access_por_conta = {}
    for sid, rt in toks.items():
        try:
            access_por_conta[sid] = obter_access(sb, sid, rt)[0]
        except Exception as e:
            print(f"[{sid}] token: {e}")

    # (1) casos novos
    novos = _scan_cancelados_recentes(dias, access_por_conta)
    if novos:
        for i in range(0, len(novos), 200):
            try:
                sb.table("envios_problema").upsert(novos[i:i+200], on_conflict="order_id").execute()
            except Exception as e:
                print("Aviso: falha ao gravar novos:", str(e)[:150])
    print(f"Monitor: {len(novos)} envios-problema nos cancelados dos ultimos {dias} dias.", flush=True)

    # (2) re-consulta os ABERTOS e atualiza
    abertos = (sb.table("envios_problema").select(
        "order_id, seller_id, titulo, valor, shipping_id, substatus, notificado")
        .neq("fechado", True).execute().data) or []
    print(f"Monitor: {len(abertos)} casos abertos pra atualizar.", flush=True)
    alertas = []
    for c in abertos:
        sid = str(c.get("seller_id"))
        access = access_por_conta.get(sid)
        if not access:
            continue
        shp = c.get("shipping_id")
        if not shp:
            shp = _shipping_do_pedido(c["order_id"], access)
        if not shp:
            continue
        sj = ml_get(f"/shipments/{shp}", access)
        st = sj.get("status") if isinstance(sj, dict) else None
        sub = sj.get("substatus") if isinstance(sj, dict) else None
        upd = {"status_envio": st, "substatus": sub, "shipping_id": str(shp),
               "atualizado_em": datetime.now(timezone.utc).isoformat()}
        # alerta: virou confiscado agora e ainda nao avisamos
        if (sub or "").lower() == "confiscated" and not c.get("notificado"):
            alertas.append(c)
            upd["notificado"] = True
        try:
            sb.table("envios_problema").update(upd).eq("order_id", c["order_id"]).execute()
        except Exception as e:
            print("Aviso: falha ao atualizar", c["order_id"], str(e)[:100])
        time.sleep(0.15)

    # (3) alertas de confiscado (gatilho ANTES do ML devolver o dinheiro)
    if NOTIFICAR and alertas:
        for c in alertas:
            tg_send("⚠️ <b>ENVIO CONFISCADO (risco de prejuizo)</b>\n"
                    f"Pedido <code>{c['order_id']}</code>\n"
                    f"{(c.get('titulo') or '')[:80]}\n"
                    f"Valor: R$ {c.get('valor')}\n"
                    "Confiscado pela Receita/transporte. Acompanhe o reembolso ao cliente.")
    print(f"Monitor: {len(alertas)} alertas de confiscado enviados.", flush=True)


# ---------------- Limpar coleta (tira cancelados-sem-envio) ----------------
def rodar_limpar_coleta():
    """Varre os casos em etapa 'coleta' e separa os que foram CANCELADOS SEM
    NUNCA TEREM SIDO ENVIADOS (produto nunca saiu -> nada a coletar).
    Por padrao SO RELATA. Para mover de verdade, rode com o campo 'pedido' = aplicar."""
    aplicar = os.environ.get("PEDIDO_DEBUG", "").strip().lower() == "aplicar"
    casos = (sb.table("devolucoes").select("claim_id, order_id, seller_id, titulo")
             .eq("etapa_interna", "coleta").execute().data) or []
    print(f"Coleta: {len(casos)} casos pra checar. Modo: {'APLICAR' if aplicar else 'so relato'}", flush=True)

    # mapa order_id -> shipping_id (via vendas), em lotes
    oids = [str(c["order_id"]) for c in casos if c.get("order_id")]
    ship_por_order = {}
    for i in range(0, len(oids), 150):
        lote = oids[i:i + 150]
        rows = (sb.table("vendas").select("order_id, shipping_id")
                .in_("order_id", lote).execute().data) or []
        for r in rows:
            if r.get("shipping_id"):
                ship_por_order[str(r["order_id"])] = str(r["shipping_id"])

    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    acc = {}
    nunca, enviados, sem_ship = [], 0, 0
    for idx, c in enumerate(casos):
        sid = str(c.get("seller_id"))
        if sid not in acc:
            rt = toks.get(sid)
            if not rt:
                continue
            try:
                acc[sid] = obter_access(sb, sid, rt)[0]
            except Exception as e:
                print(f"[{sid}] token: {e}"); continue
        access = acc[sid]
        oid = str(c.get("order_id"))
        shp = ship_por_order.get(oid) or _shipping_do_pedido(oid, access)
        if not shp:
            sem_ship += 1
            continue
        sh = ml_get(f"/shipments/{shp}", access)
        hist = (sh.get("status_history") or {}) if isinstance(sh, dict) else {}
        enviou = bool(hist.get("date_shipped") or hist.get("date_delivered"))
        if enviou:
            enviados += 1
        else:
            nunca.append(c)
        if (idx + 1) % 50 == 0:
            print(f"   ...{idx+1}/{len(casos)}", flush=True)
        time.sleep(0.15)

    print("\n=== RESULTADO ===", flush=True)
    print(f"NUNCA ENVIADOS (sai da coleta):  {len(nunca)}")
    print(f"Enviados/entregues (ficam):      {enviados}")
    if sem_ship:
        print(f"Sem shipping_id (nao checado):   {sem_ship}")

    if not aplicar:
        for c in nunca:
            print(f"  fora -> {c['order_id']} | {(c.get('titulo') or '')[:50]}")
        if nunca:
            print(f"\n(rode de novo com o campo 'pedido' = aplicar para mover esses {len(nunca)} pra fora da coleta)")
        return

    # APLICAR em LOTE (poucas chamadas, rapido)
    ids = [c["claim_id"] for c in nunca]
    movidos = 0
    for i in range(0, len(ids), 100):
        lote = ids[i:i + 100]
        try:
            sb.table("devolucoes").update({"etapa_interna": "cancelada"}) \
                .in_("claim_id", lote).execute()
            movidos += len(lote)
        except Exception as e:
            print("  falha no lote de update:", str(e)[:120])
        try:
            sb.table("devolucoes_historico").insert(
                [{"claim_id": cid, "campo": "etapa_interna", "de_valor": "coleta",
                  "para_valor": "cancelada", "por": "limpar_coleta"} for cid in lote]).execute()
        except Exception:
            pass
        print(f"   movidos {movidos}/{len(ids)}", flush=True)
    print(f"\nAPLICADO: {movidos} movidos de 'coleta' -> 'cancelada'.")


# ---------------- Checar coleta (relatorio pra achar o padrao) ----------------
def rodar_checar_coleta():
    """Relatorio compacto dos casos em 'coleta': para cada um traz
    status/substatus do envio, se foi entregue, se reembolsou e o 'flow' do
    reembolso. Nao grava nada -> so pra a gente achar o padrao."""
    casos = (sb.table("devolucoes").select("claim_id, order_id, seller_id, titulo, valor")
             .eq("etapa_interna", "coleta").order("data_abertura", desc=True).execute().data) or []
    print(f"Checando {len(casos)} casos em coleta...\n", flush=True)

    oids = [str(c["order_id"]) for c in casos if c.get("order_id")]
    ship = {}
    for i in range(0, len(oids), 150):
        rows = (sb.table("vendas").select("order_id, shipping_id")
                .in_("order_id", oids[i:i + 150]).execute().data) or []
        for r in rows:
            if r.get("shipping_id"):
                ship[str(r["order_id"])] = str(r["shipping_id"])

    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    acc = {}
    print(f"{'order_id':<16} {'R$':>7}  {'envio (status/substatus)':<28} {'entreg':<6} {'reemb':<5} {'flow':<16} titulo")
    print("-" * 130)
    for c in casos:
        sid = str(c.get("seller_id"))
        if sid not in acc:
            rt = toks.get(sid)
            if not rt:
                continue
            try:
                acc[sid] = obter_access(sb, sid, rt)[0]
            except Exception as e:
                print(f"[{sid}] token: {e}"); continue
        access = acc[sid]
        oid = str(c.get("order_id"))
        shp = ship.get(oid) or _shipping_do_pedido(oid, access)
        envio, entregue = "-", "-"
        if shp:
            sj = ml_get(f"/shipments/{shp}", access)
            if isinstance(sj, dict):
                envio = f"{sj.get('status')}/{sj.get('substatus')}"
                hist = sj.get("status_history") or {}
                entregue = "sim" if hist.get("date_delivered") else "nao"
        reemb, flow = "-", "-"
        o = ml_get(f"/orders/{oid}", access)
        pid = None
        if isinstance(o, dict):
            for p in (o.get("payments") or []):
                if p.get("id"):
                    pid = p.get("id")
                reemb = "sim" if p.get("status") == "refunded" else "nao"
        if pid:
            col = ml_get(f"/collections/{pid}", access)
            if isinstance(col, dict):
                for rf in (col.get("refunds") or []):
                    fl = ((rf.get("metadata") or {}).get("coverage") or {}).get("flow")
                    if fl:
                        flow = fl
        print(f"{oid:<16} {(c.get('valor') or 0):>7.0f}  {envio:<28} {entregue:<6} {reemb:<5} {flow:<16} {(c.get('titulo') or '')[:32]}", flush=True)
        time.sleep(0.2)
    print("\n(fim)")


# ---------------- Mediacoes: prazo de resposta + alerta ----------------
def _acao_do_respondente(det, seller_id):
    """Le players[respondent].available_actions e devolve
    (acao, obrigatoria, prazo_iso) — a acao com prazo mais proximo."""
    acao, obrig, prazo = None, False, None
    for p in (det.get("players") or []):
        # respondente = o vendedor (por papel; confere o user_id por seguranca)
        if p.get("role") != "respondent":
            continue
        for a in (p.get("available_actions") or []):
            if a.get("mandatory"):
                obrig = True
            due = a.get("due_date")
            if due and (prazo is None or due < prazo):
                prazo, acao = due, a.get("action")
            if acao is None:
                acao = a.get("action")
    return acao, obrig, prazo


def rodar_mediacoes():
    """Varre as reclamacoes ABERTAS, grava o prazo de resposta na devolucoes e
    ALERTA no Telegram quando ha acao obrigatoria com prazo (pra nao perder no
    automatico). Some sozinho quando nao precisa mais de acao.
    Tambem registra o desfecho de cada prazo (respondida x vencida)."""
    agora = datetime.now(timezone.utc)
    def _passou(prazo_iso):
        try:
            return datetime.fromisoformat((prazo_iso or "").replace("Z", "+00:00")) < agora
        except Exception:
            return False
    alertas = []
    for seller_id, refresh in lista_refresh_tokens():
        if SO_SELLER and str(seller_id) != SO_SELLER:
            continue
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"[{seller_id}] token: {e}"); continue
        claims = buscar_claims(access, sid)
        abertas = [c for c in claims if (c.get("status") or "").lower() != "closed"]
        print(f"[{sid}] {len(abertas)} reclamacoes abertas", flush=True)
        for c in abertas:
            cid = str(c.get("id"))
            det = ml_get(f"/post-purchase/v1/claims/{cid}", access)
            if not isinstance(det, dict):
                continue
            acao, obrig, prazo = _acao_do_respondente(det, sid)
            atual = (sb.table("devolucoes").select("acao_prazo, acao_notificada, titulo, valor")
                     .eq("claim_id", cid).limit(1).execute().data) or []
            prazo_antigo = atual[0].get("acao_prazo") if atual else None
            if obrig and prazo:
                # ainda PENDENTE (aparece na aba). Se veio um prazo NOVO (ex: mediacao
                # deu outra data apos voce responder), volta limpo -> nao fica em "Resolvidas".
                upd = {"acao_pendente": acao, "acao_obrigatoria": True, "acao_prazo": prazo,
                       "acao_resultado": None, "acao_resolvida_em": None}
                ja = atual[0].get("acao_notificada") if atual else None
                if ja != prazo:   # prazo novo (ou mudou) -> alerta uma vez
                    alertas.append({"order": c.get("resource_id"), "acao": acao, "prazo": prazo,
                                    "titulo": (atual[0].get("titulo") if atual else None),
                                    "valor": (atual[0].get("valor") if atual else None)})
                    upd["acao_notificada"] = prazo
            elif prazo_antigo:
                # tinha prazo e agora nao tem acao -> RESOLVIDO (voce respondeu, ou venceu)
                res = "vencida" if _passou(prazo_antigo) else "respondida"
                upd = {"acao_pendente": None, "acao_obrigatoria": False, "acao_prazo": None,
                       "acao_notificada": None, "acao_resultado": res,
                       "acao_resolvida_em": agora.isoformat()}
            else:
                upd = {"acao_pendente": acao, "acao_obrigatoria": obrig}
            try:
                sb.table("devolucoes").update(upd).eq("claim_id", cid).execute()
            except Exception as e:
                print("  falha update", cid, str(e)[:80])
            time.sleep(0.2)
        # reclamacoes que FECHARAM no ML e ainda tinham prazo -> registra desfecho e limpa
        try:
            fechadas = (sb.table("devolucoes").select("claim_id, acao_prazo")
                        .eq("seller_id", str(sid)).eq("status_ml", "closed")
                        .not_.is_("acao_prazo", "null").execute().data) or []
        except Exception:
            fechadas = []
        for f in fechadas:
            res = "vencida" if _passou(f.get("acao_prazo")) else "respondida"
            try:
                sb.table("devolucoes").update(
                    {"acao_prazo": None, "acao_pendente": None, "acao_obrigatoria": False,
                     "acao_notificada": None, "acao_resultado": res,
                     "acao_resolvida_em": agora.isoformat()}).eq("claim_id", f["claim_id"]).execute()
            except Exception:
                pass

    if NOTIFICAR and alertas:
        for a in alertas:
            pr = (a["prazo"] or "")[:16].replace("T", " ")
            tg_send("⏰ <b>RECLAMACAO A RESPONDER</b>\n"
                    f"Pedido <code>{a['order']}</code>\n"
                    f"{(a.get('titulo') or '')[:70]}\n"
                    f"Acao: {a['acao']}\n"
                    f"Prazo: {pr}\n"
                    "Responda/conteste no ML antes de vencer — senao perde no automatico.")
    print(f"Mediacoes: {len(alertas)} alertas de prazo enviados.", flush=True)


# ---------------- Diagnostico: reclamacoes ABERTAS + prazo ----------------
def rodar_claims_abertas():
    """Lista as reclamacoes/mediacoes ABERTAS (nao encerradas) e mostra os
    campos candidatos a PRAZO de resposta, pra montar o alerta de 'responder
    a tempo'. Faz dump completo das 2 primeiras pra achar o campo certo."""
    dumps = 0
    for seller_id, refresh in lista_refresh_tokens():
        if SO_SELLER and str(seller_id) != SO_SELLER:
            continue
        try:
            access, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"[{seller_id}] token: {e}"); continue
        claims = buscar_claims(access, sid)
        abertas = [c for c in claims if (c.get("status") or "").lower() != "closed"]
        print(f"\n[{sid}] {len(abertas)} reclamacoes ABERTAS de {len(claims)} no periodo", flush=True)
        for c in abertas:
            cid = c.get("id")
            det = ml_get(f"/post-purchase/v1/claims/{cid}", access)
            print(f"  claim {cid} | status={c.get('status')} stage={c.get('stage')} "
                  f"type={c.get('type')} order={c.get('resource_id')}", flush=True)
            if isinstance(det, dict):
                for k in ("due_date", "expected_resolution_date", "date_created",
                          "last_updated", "available_actions", "actions", "players", "resolution"):
                    if k in det and det[k] not in (None, [], {}):
                        print(f"      {k}: {json.dumps(det[k], ensure_ascii=False)[:280]}")
            if dumps < 2:
                _dump(f"CLAIM {cid} COMPLETO", det)
                dumps += 1
            time.sleep(0.2)
    print("\n(fim) — me manda esse log que eu acho o campo do prazo")


# ---------------- Mapear coleta (achar o sinal que define coleta) ----------------
def rodar_mapear_coleta():
    """Dump COMPLETO (devolucoes + claim + RETURNS + shipment) dos pedidos passados
    no campo 'pedido' (separados por virgula). Serve pra achar o sinal comum que
    define uma COLETA de verdade e virar regra."""
    ids = _lista_pedidos()
    if not ids:
        print("Passe os order_ids no campo 'pedido' (separados por virgula).")
        return
    rows = (sb.table("devolucoes")
            .select("order_id, seller_id, claim_id, etapa_interna, tem_retorno, aplicou_cobertura, status_ml")
            .in_("order_id", ids).execute().data) or []
    info = {str(r["order_id"]): r for r in rows}
    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    acc = {}
    for oid in ids:
        rec = info.get(oid)
        print(f"\n################ COLETA? {oid} ################", flush=True)
        sid = str(rec.get("seller_id")) if rec else None
        if not sid:
            v = (sb.table("vendas").select("seller_id").eq("order_id", oid).limit(1).execute().data) or []
            sid = str(v[0]["seller_id"]) if v else None
        if not sid:
            print("  sem seller_id, pulei"); continue
        if sid not in acc:
            try:
                acc[sid] = obter_access(sb, sid, toks.get(sid))[0]
            except Exception as e:
                print(f"  token: {e}"); continue
        access = acc[sid]
        if rec:
            print(f"  devolucoes: etapa={rec.get('etapa_interna')} status_ml={rec.get('status_ml')} "
                  f"tem_retorno={rec.get('tem_retorno')} aplicou_cobertura={rec.get('aplicou_cobertura')} "
                  f"claim={rec.get('claim_id')}", flush=True)
        cid = rec.get("claim_id") if rec else None
        if cid:
            _dump("CLAIM", ml_get(f"/post-purchase/v1/claims/{cid}", access))
            _dump("RETURNS (v2)", ml_get(f"/post-purchase/v2/claims/{cid}/returns", access))
        o = ml_get(f"/orders/{oid}", access)
        _dump("ORDER (completo)", o)
        shp = ((o.get("shipping") or {}).get("id")) if isinstance(o, dict) else None
        if shp:
            _dump("SHIPMENT (status/substatus/status_history)", ml_get(f"/shipments/{shp}", access))
            # tambem o /shipments/{id}/history (movimentacoes) ajuda a ver coleta
            _dump("SHIPMENT HISTORY", ml_get(f"/shipments/{shp}/history", access))
        time.sleep(0.3)
    print("\n(fim) — me manda esse log que eu acho o sinal comum das coletas")


# ---------------- Diagnostico da coleta (sinal logistico, sem financeiro) ----------------
def rodar_diag_coleta():
    """Varre as mediacoes, olha o RETORNO de cada uma e lista as que tem retorno
    NAO concluido (produto nao voltou) -> candidatas a COLETA. Marca se e volumoso.
    Nao usa nada financeiro. So relatorio."""
    rows, off, passo = [], 0, 1000
    while True:
        lote = (sb.table("devolucoes").select("order_id, seller_id, claim_id, status_ml, titulo")
                .eq("tipo", "mediations").range(off, off + passo - 1).execute().data) or []
        rows += lote
        if len(lote) < passo:
            break
        off += passo
    print(f"Mediacoes a checar: {len(rows)}", flush=True)

    oids = [str(r["order_id"]) for r in rows if r.get("order_id")]
    ship = {}
    for i in range(0, len(oids), 200):
        vr = (sb.table("vendas").select("order_id, shipping_id")
              .in_("order_id", oids[i:i + 200]).execute().data) or []
        for v in vr:
            if v.get("shipping_id"):
                ship[str(v["order_id"])] = str(v["shipping_id"])

    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    acc = {}
    achados = []
    print(f"\n{'order_id':<16} {'entregue':<8} {'envio (status/substatus)':<26} {'return':<13} {'money':<10} titulo")
    print("-" * 125)
    for idx, r in enumerate(rows):
        sid = str(r.get("seller_id"))
        if SO_SELLER and sid != SO_SELLER:
            continue
        if sid not in acc:
            rt = toks.get(sid)
            if not rt:
                continue
            try:
                acc[sid] = obter_access(sb, sid, rt)[0]
            except Exception as e:
                print(f"[{sid}] token: {e}"); continue
        access = acc[sid]
        oid = str(r.get("order_id"))
        shp = ship.get(oid)
        if not shp:
            continue
        sj = ml_get(f"/shipments/{shp}", access)
        time.sleep(0.12)
        if not isinstance(sj, dict):
            continue
        tm = sj.get("tracking_method") or ""
        itypes = " ".join(sj.get("items_types") or [])
        if not ("Voluminoso" in tm or "bulky" in itypes):
            continue   # SO os VOLUMOSOS
        hist = sj.get("status_history") or {}
        entregue = "sim" if hist.get("date_delivered") else "nao"
        envio = f"{sj.get('status')}/{sj.get('substatus')}"
        cid = r.get("claim_id")
        ret = ml_get(f"/post-purchase/v2/claims/{cid}/returns", access) if cid else None
        time.sleep(0.1)
        if isinstance(ret, dict) and not ret.get("error") and ret.get("_http") != 404:
            rstatus, money = (ret.get("status") or "-"), (ret.get("status_money") or "-")
        else:
            rstatus, money = "sem_retorno", "-"
        achados.append((oid, entregue, rstatus))
        print(f"{oid:<16} {entregue:<8} {envio:<26} {rstatus:<13} {money:<10} {(r.get('titulo') or '')[:34]}", flush=True)
        if (idx + 1) % 100 == 0:
            print(f"   ...{idx+1}/{len(rows)} varridos", flush=True)
    # resumo por estado do retorno (volumosos)
    from collections import Counter
    buck = Counter()
    for _oid, _entr, _rst in achados:
        if _rst in ("failed", "expired", "not_delivered"):
            buck["NAO VOLTOU (coleta/perda)"] += 1
        elif _rst == "delivered":
            buck["VOLTOU (reestocar)"] += 1
        elif _rst in ("shipped", "label_generated", "in_transit", "ready_to_ship"):
            buck["VOLTANDO (antes de expirar)"] += 1
        elif _rst == "cancelled":
            buck["cancelado"] += 1
        else:
            buck[_rst or "sem_retorno"] += 1
    print(f"\n=== {len(achados)} mediacoes VOLUMOSAS (candidatas a coleta) ===", flush=True)
    for k, v in buck.most_common():
        print(f"  {v:4d}  {k}")


# ---------------- Classificar coleta (grava retorno + reclassifica) ----------------
def rodar_classificar_coleta():
    """Varre as mediacoes, grava return_status/return_money/volumoso e RECLASSIFICA
    a etapa pela regra logistica:
      coleta   = FECHADO + retorno failed/expired + VOLUMOSO  (produto nao voltou)
      voltou   = retorno delivered   -> recebido_triagem (reestocar)
      voltando = shipped/label_generated -> em_transito
      resto    = sai da coleta -> encerrado
    So mexe em etapas 'aberto/em_transito/coleta' (nao toca trabalho manual)."""
    from collections import Counter
    rows, off, passo = [], 0, 1000
    while True:
        lote = (sb.table("devolucoes")
                .select("order_id, seller_id, claim_id, status_ml, etapa_interna, titulo")
                .eq("tipo", "mediations").range(off, off + passo - 1).execute().data) or []
        rows += lote
        if len(lote) < passo:
            break
        off += passo
    print(f"Mediacoes a classificar: {len(rows)}", flush=True)

    oids = [str(r["order_id"]) for r in rows if r.get("order_id")]
    ship = {}
    for i in range(0, len(oids), 200):
        vr = (sb.table("vendas").select("order_id, shipping_id")
              .in_("order_id", oids[i:i + 200]).execute().data) or []
        for v in vr:
            if v.get("shipping_id"):
                ship[str(v["order_id"])] = str(v["shipping_id"])

    toks = {str(sid): rt for sid, rt in lista_refresh_tokens()}
    acc = {}
    resumo = Counter()
    mudancas = 0
    for idx, r in enumerate(rows):
        sid = str(r.get("seller_id"))
        if SO_SELLER and sid != SO_SELLER:
            continue
        if sid not in acc:
            rt = toks.get(sid)
            if not rt:
                continue
            try:
                acc[sid] = obter_access(sb, sid, rt)[0]
            except Exception as e:
                print(f"[{sid}] token: {e}"); continue
        access = acc[sid]
        oid = str(r.get("order_id"))
        volumoso = None
        shp = ship.get(oid)
        if shp:
            sj = ml_get(f"/shipments/{shp}", access)
            time.sleep(0.1)
            if isinstance(sj, dict):
                tm = sj.get("tracking_method") or ""
                itypes = " ".join(sj.get("items_types") or [])
                volumoso = ("Voluminoso" in tm or "bulky" in itypes)
        rstatus = rmoney = None
        cid = r.get("claim_id")
        if cid:
            ret = ml_get(f"/post-purchase/v2/claims/{cid}/returns", access)
            time.sleep(0.1)
            if isinstance(ret, dict) and not ret.get("error") and ret.get("_http") != 404:
                rstatus, rmoney = ret.get("status"), ret.get("status_money")
        upd = {"return_status": rstatus, "return_money": rmoney, "volumoso": volumoso}

        et = r.get("etapa_interna") or "aberto"
        eh_coleta = (r.get("status_ml") == "closed") and (rstatus in ("failed", "expired")) and bool(volumoso)
        novo = None
        if eh_coleta and et in ("aberto", "em_transito", "coleta"):
            novo = "coleta"
        elif et == "coleta" and not eh_coleta:
            if rstatus == "delivered":
                novo = "recebido_triagem"
            elif rstatus in ("shipped", "label_generated"):
                novo = "em_transito"
            else:
                novo = "encerrado"
        if novo and novo != et:
            upd["etapa_interna"] = novo
            mudancas += 1
            try:
                sb.table("devolucoes_historico").insert({
                    "claim_id": cid, "campo": "etapa_interna",
                    "de_valor": et, "para_valor": novo, "por": "classificar_coleta"}).execute()
            except Exception:
                pass
        try:
            sb.table("devolucoes").update(upd).eq("claim_id", cid).execute()
        except Exception as e:
            print("  upd falha", cid, str(e)[:80])
        resumo["COLETA" if eh_coleta else (rstatus or "sem_retorno")] += 1
        if (idx + 1) % 100 == 0:
            print(f"   ...{idx+1}/{len(rows)}", flush=True)

    print(f"\n=== {mudancas} mudaram de etapa ===", flush=True)
    print("--- distribuicao (retorno) ---")
    for k, v in resumo.most_common():
        print(f"  {v:4d}  {k}")


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
    elif _modo == "scan":
        rodar_scan()
    elif _modo == "backfill":
        rodar_backfill()
    elif _modo == "monitor":
        rodar_monitor()
    elif _modo == "limpar_coleta":
        rodar_limpar_coleta()
    elif _modo == "checar_coleta":
        rodar_checar_coleta()
    elif _modo == "claims_abertas":
        rodar_claims_abertas()
    elif _modo == "mediacoes":
        rodar_mediacoes()
    elif _modo == "mapear_coleta":
        rodar_mapear_coleta()
    elif _modo == "diag_coleta":
        rodar_diag_coleta()
    elif _modo == "classificar_coleta":
        rodar_classificar_coleta()
    else:
        main()
