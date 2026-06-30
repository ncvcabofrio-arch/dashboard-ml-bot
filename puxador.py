"""
Puxador Mercado Livre -> Supabase (versao automatica / GitHub Actions)
+ Notificacoes no Telegram para vendas novas.

- Le o refresh_token (do banco; na 1a vez, do secret ML_REFRESH_TOKEN)
- Renova o acesso no Mercado Livre (e guarda o novo refresh_token no banco)
- Baixa vendas e reputacao e grava no Supabase
- Avisa no Telegram cada venda nova (se os secrets do Telegram existirem)
"""

import os
import json
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

# imprime 1 pedido completo no log (pra inspecionar comissao/frete/repasse)
_DEBUG_PEDIDO = os.environ.get("DEBUG_PEDIDO", "1") == "1"
_debug_feito = False

# ---- Configuracao (vem dos secrets do GitHub) ----
CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DIAS = int(os.environ.get("DIAS", "7"))

# Telegram (opcional: se vazio, nao notifica)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

API = "https://api.mercadolibre.com"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------- Telegram ----------------
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=30)
    except Exception as e:
        print("Aviso: falha ao enviar Telegram:", e)


def notificar_vendas_novas():
    if not TG_TOKEN or not TG_CHAT:
        return
    novas = (sb.table("vendas").select("*")
             .eq("notificado", False).eq("status", "paid").execute().data) or []
    if not novas:
        return

    # apelidos das contas
    contas = {c["seller_id"]: (c.get("apelido") or c["seller_id"])
              for c in (sb.table("contas").select("seller_id, apelido")
                        .execute().data or [])}

    # agrupa por pedido
    pedidos = defaultdict(list)
    for r in novas:
        pedidos[r["order_id"]].append(r)

    # se vier muita coisa de uma vez, manda um resumo em vez de spam
    if len(pedidos) > 20:
        total = sum((its[0].get("total") or 0) for its in pedidos.values())
        tg_send(f"🛒 <b>{len(pedidos)} vendas novas!</b>\n"
                f"Total: R$ {total:,.2f}")
    else:
        for oid, itens in pedidos.items():
            it0 = itens[0]
            conta = contas.get(it0["seller_id"], it0["seller_id"])
            total = it0.get("total") or 0
            titulo = it0.get("titulo") or "(produto)"
            extra = f" (+{len(itens)-1} item)" if len(itens) > 1 else ""
            tg_send(f"🛒 <b>Nova venda!</b>\n"
                    f"Conta: {conta}\n"
                    f"Valor: R$ {total:,.2f}\n"
                    f"Produto: {titulo}{extra}")

    # marca como avisado
    for oid in pedidos:
        sb.table("vendas").update({"notificado": True}) \
            .eq("order_id", oid).execute()


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


def ml_get(path, access):
    return requests.get(API + path,
                        headers={"Authorization": "Bearer " + access},
                        timeout=30).json()


def lista_refresh_tokens():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    tokens = [(c["seller_id"], c["refresh_token"])
              for c in (res.data or []) if c.get("refresh_token")]
    if not tokens and SEED_REFRESH:
        tokens = [(None, SEED_REFRESH)]
    return tokens


def puxar_conta(access, seller_id):
    u = ml_get("/users/" + seller_id, access)
    sb.table("contas").upsert(
        {"seller_id": seller_id, "apelido": u.get("nickname")},
        on_conflict="seller_id").execute()

    rep = u.get("seller_reputation") or {}
    tr = rep.get("transactions") or {}
    rt = tr.get("ratings") or {}
    met = rep.get("metrics") or {}

    def taxa(k):
        return (met.get(k) or {}).get("rate")

    sb.table("reputacao").upsert({
        "seller_id": seller_id,
        "nivel": rep.get("level_id"),
        "transacoes_total": tr.get("total"),
        "positivas": rt.get("positive"),
        "neutras": rt.get("neutral"),
        "negativas": rt.get("negative"),
        "taxa_reclamacoes": taxa("claims"),
        "taxa_cancelamentos": taxa("cancellations"),
        "taxa_atrasos": taxa("delayed_handling_time"),
    }, on_conflict="seller_id,data").execute()

    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)) \
        .strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    linhas, offset, total = [], 0, 1
    while offset < total and offset < 2000:
        path = ("/orders/search?seller=" + seller_id +
                "&order.date_created.from=" + urllib.parse.quote(desde) +
                "&sort=date_desc&limit=50&offset=" + str(offset))
        data = ml_get(path, access)
        total = (data.get("paging") or {}).get("total", 0)
        for o in data.get("results", []):
            global _debug_feito
            if _DEBUG_PEDIDO and not _debug_feito:
                print("===== ENVIO/FRETE (DEBUG) =====")
                ship_id = (o.get("shipping") or {}).get("id")
                print("shipping_id:", ship_id, " | total:", o.get("total_amount"))
                if ship_id:
                    for ep in [f"/shipments/{ship_id}/costs", f"/shipments/{ship_id}"]:
                        try:
                            d2 = ml_get(ep, access)
                            print(f"----- GET {ep} -----")
                            print(json.dumps(d2, ensure_ascii=False, indent=2)[:3000])
                        except Exception as e:
                            print(ep, "erro:", e)
                print("===== FIM DEBUG =====")
                _debug_feito = True
            pay = (o.get("payments") or [{}])[0]
            for it in o.get("order_items", []):
                item = it.get("item") or {}
                linhas.append({
                    "seller_id": seller_id,
                    "order_id": str(o.get("id")),
                    "status": o.get("status"),
                    "data_aprovacao": o.get("date_created"),
                    "total": o.get("total_amount"),
                    "forma_pagamento": pay.get("payment_method_id"),
                    "comissao": it.get("sale_fee"),
                    "item_id": item.get("id"),
                    "sku": item.get("seller_sku"),
                    "titulo": item.get("title"),
                    "categoria_id": item.get("category_id"),
                    "quantidade": it.get("quantity"),
                    "valor_unitario": it.get("unit_price"),
                })
        offset += 50
        time.sleep(0.3)

    for i in range(0, len(linhas), 200):
        sb.table("vendas").upsert(
            linhas[i:i + 200],
            on_conflict="order_id,item_id,seller_id").execute()

    # cadastra automaticamente SKUs novos na tabela de produtos
    # (custo fica em branco pra voce preencher; nao mexe nos custos ja existentes)
    skus = sorted({l["sku"] for l in linhas if l.get("sku")})
    if skus:
        try:
            sb.table("produtos").upsert(
                [{"sku": s} for s in skus],
                on_conflict="sku", ignore_duplicates=True).execute()
        except Exception as e:
            print("Aviso: falha ao cadastrar SKUs novos:", e)

    return len(linhas)


def main():
    tokens = lista_refresh_tokens()
    if not tokens:
        raise SystemExit("Nenhum refresh_token. Configure o secret ML_REFRESH_TOKEN.")

    for seller_id, refresh in tokens:
        d = renovar_token(refresh)
        access = d["access_token"]
        novo_refresh = d.get("refresh_token", refresh)
        sid = str(d.get("user_id") or seller_id)

        sb.table("contas").upsert(
            {"seller_id": sid, "refresh_token": novo_refresh},
            on_conflict="seller_id").execute()

        n = puxar_conta(access, sid)
        print(f"[{sid}] {n} vendas atualizadas em {datetime.now()}")

    # depois de atualizar todas as contas, avisa as vendas novas
    notificar_vendas_novas()


if __name__ == "__main__":
    main()
