"""
Puxador de DEVOLUCOES / RMA  Mercado Livre -> Supabase
(mesmo estilo do seu puxador de vendas: refresh token por conta,
 Supabase, Telegram, roda no GitHub Actions)

O que faz:
  - Para cada conta (tabela 'contas'), busca as reclamacoes/devolucoes
    dos ultimos DIAS dias na API do ML (post-purchase/v1/claims/search).
  - Enriquece com o pedido (/orders/{id}) para trazer produto/sku/valor.
  - Busca o retorno (frete reverso) em /post-purchase/v2/claims/{id}/returns.
  - Grava em 'devolucoes' e 'devolucoes_retornos' SEM sobrescrever os
    campos internos da sua esteira (etapa_interna, laudo, custos, desfecho...).
  - Registra mudanca de status do ML em 'devolucoes_historico'.
  - Avisa no Telegram quando entra devolucao nova.

Modos:
  (sem MODO)      -> rodada normal (ultimos DIAS dias, todas as contas)
  MODO=debug      -> NAO grava nada; imprime o JSON cru do 1o claim e do
                     1o return, pra calibrarmos o mapeamento de campos.
"""
import os
import time
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

# ---- Configuracao (vem dos secrets do GitHub, iguais aos do puxador de vendas) ----
CLIENT_ID     = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH  = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
DIAS          = int(os.environ.get("DIAS_DEVOLUCAO", os.environ.get("DIAS", "30")))
SO_SELLER     = os.environ.get("SO_SELLER", "").strip()

TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFICAR = os.environ.get("NOTIFICAR", "1") == "1"
DEBUG     = os.environ.get("MODO", "") == "debug"

API = "https://api.mercadolibre.com"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# etapa interna sugerida a partir do status do ML (so no INSERT; nao mexe depois)
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


# ---------------- Mercado Livre (igual ao puxador de vendas) ----------------
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
        return r.json()
    except Exception:
        return {"_status": r.status_code, "_text": r.text[:300]}


def lista_refresh_tokens():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    tokens = [(c["seller_id"], c["refresh_token"])
              for c in (res.data or []) if c.get("refresh_token")]
    if not tokens and SEED_REFRESH:
        tokens = [(None, SEED_REFRESH)]
    return tokens


# ---------------- Busca de claims/devolucoes ----------------
def buscar_claims(access, seller_id):
    """Lista as reclamacoes/devolucoes dos ultimos DIAS dias da conta."""
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)) \
        .strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    ate = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    rng = urllib.parse.quote(f"date_created:after:{desde},before:{ate}")

    claims, offset, total = [], 0, 1
    while offset < total and offset < 2000:
        path = ("/post-purchase/v1/claims/search"
                f"?range={rng}&sort=date_created:desc&limit=50&offset={offset}")
        data = ml_get(path, access)
        # a API pode devolver em 'data' ou 'results'
        lote = data.get("data") or data.get("results") or []
        pag = data.get("paging") or {}
        total = pag.get("total", len(lote))
        if not lote:
            break
        claims.extend(lote)
        offset += 50
        time.sleep(0.3)
    return claims


def detalhe_motivo(reason_id, access):
    """Traduz o codigo do motivo para texto (com cache)."""
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


def dados_do_pedido(order_id, access):
    """Reaproveita /orders/{id} pra trazer produto/sku/valor/comprador."""
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
    """Monta a linha de 'devolucoes' SO com colunas do ML (nao toca no interno)."""
    # resource e resource_id: relaciona ao pedido/envio
    resource = c.get("resource")
    resource_id = c.get("resource_id")
    order_id = str(resource_id) if resource == "order" and resource_id else None
    # alguns claims trazem related_entities/order dentro de estruturas variadas
    if not order_id:
        for ent in (c.get("related_entities") or []):
            if isinstance(ent, dict) and ent.get("type") == "order":
                order_id = str(ent.get("id"))
                break

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
        "data_abertura": c.get("date_created"),
        "ml_atualizado_em": c.get("last_updated"),
    }
    # enriquece com o pedido, se der
    if order_id:
        try:
            linha.update({k: v for k, v in dados_do_pedido(order_id, access).items()
                          if v is not None})
        except Exception as e:
            print("  aviso: falha ao enriquecer pedido", order_id, str(e)[:80])
    return linha


def retorno_do_claim(claim_id, access):
    """Busca o return (frete reverso) do claim. Retorna linha ou None."""
    d = ml_get(f"/post-purchase/v2/claims/{claim_id}/returns", access)
    if not isinstance(d, dict) or d.get("_status") in (403, 404):
        return None
    # a estrutura pode vir como objeto unico ou lista; tratamos defensivo
    ret = d
    if isinstance(d.get("data"), list) and d["data"]:
        ret = d["data"][0]
    shipping = ret.get("shipping") or {}
    review = ret.get("review") or {}
    if not (ret.get("status") or shipping or ret.get("subtype")):
        return None
    return {
        "claim_id": str(claim_id),
        "return_status": ret.get("status"),
        "subtype": ret.get("subtype"),
        "shipment_id": str(shipping.get("id")) if shipping.get("id") else None,
        "tracking": shipping.get("tracking_number"),
        "tracking_status": shipping.get("status"),
        "review_status": review.get("status"),
        "ml_atualizado_em": ret.get("last_updated") or ret.get("date_created"),
    }


# ---------------- Gravacao (preserva campos internos) ----------------
def status_atuais(claim_ids):
    """Le o status_ml atual das devolucoes ja gravadas (pra detectar mudanca)."""
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
    """Upsert SO das colunas do ML -> nao sobrescreve etapa_interna, laudo, etc."""
    if not linhas:
        return 0
    antes = status_atuais([l["claim_id"] for l in linhas])
    # historico de mudanca de status do ML
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
            tg_send("↩️ <b>Nova devolucao!</b>\n"
                    f"Conta: {conta}\n"
                    f"Produto: {titulo}\n"
                    f"Pedido: {d.get('order_id') or '-'}\n"
                    f"Motivo: {motivo}\n"
                    f"Status ML: {d.get('status_ml') or '-'}")
    ids = [d["claim_id"] for d in novas]
    for i in range(0, len(ids), 200):
        sb.table("devolucoes").update({"notificado": True}) \
            .in_("claim_id", ids[i:i + 200]).execute()


# ---------------- DEBUG: imprime JSON cru pra calibrar ----------------
def rodar_debug():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token em 'contas'.")
    seller_id, refresh = tokens[0]
    d = renovar_token(refresh)
    access = d["access_token"]
    sid = str(d.get("user_id") or seller_id)
    print(f"== DEBUG conta {sid} ==")
    claims = buscar_claims(access, sid)
    print(f"Claims encontrados nos ultimos {DIAS} dias:", len(claims))
    if not claims:
        print("Nenhum claim no periodo. Aumente DIAS_DEVOLUCAO e rode de novo.")
        return
    c0 = claims[0]
    print("\n--- JSON CRU DO 1o CLAIM (search) ---")
    print(json.dumps(c0, indent=2, ensure_ascii=False)[:3500])
    cid = c0.get("id")
    print("\n--- DETALHE /post-purchase/v1/claims/{id} ---")
    print(json.dumps(ml_get(f"/post-purchase/v1/claims/{cid}", access),
                     indent=2, ensure_ascii=False)[:3500])
    print("\n--- RETURN /post-purchase/v2/claims/{id}/returns ---")
    print(json.dumps(ml_get(f"/post-purchase/v2/claims/{cid}/returns", access),
                     indent=2, ensure_ascii=False)[:3500])
    print("\n--- LINHA que seria gravada em 'devolucoes' ---")
    print(json.dumps(claim_para_linha(c0, access, sid), indent=2, ensure_ascii=False))


# ---------------- Principal ----------------
def main():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token em 'contas'. Cadastre as contas primeiro.")
    total = 0
    for seller_id, refresh in tokens:
        if SO_SELLER and str(seller_id) != SO_SELLER:
            continue
        d = renovar_token(refresh)
        access = d["access_token"]
        sid = str(d.get("user_id") or seller_id)
        # mantem o refresh_token novo (rotativo), igual ao puxador de vendas
        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": d.get("refresh_token", refresh)},
            on_conflict="seller_id").execute()

        claims = buscar_claims(access, sid)
        if not claims:
            print(f"[{sid}] nenhuma devolucao nos ultimos {DIAS} dias.")
            continue

        linhas, retornos = [], []
        for c in claims:
            linha = claim_para_linha(c, access, sid)
            linhas.append(linha)
            try:
                retornos.append(retorno_do_claim(linha["claim_id"], access))
            except Exception as e:
                print("  aviso: falha no return", linha["claim_id"], str(e)[:80])
            time.sleep(0.2)

        n = gravar_devolucoes(linhas)
        gravar_retornos(retornos)
        total += n
        print(f"[{sid}] {n} devolucoes atualizadas em {datetime.now()}")

    if NOTIFICAR:
        try:
            notificar_novas()
        except Exception as e:
            print("Aviso: falha na notificacao:", e)
    print(f"Total: {total} devolucoes processadas.")


if __name__ == "__main__":
    if DEBUG:
        rodar_debug()
    else:
        main()
