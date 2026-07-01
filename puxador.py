"""
Puxador Mercado Livre -> Supabase (automatico / GitHub Actions)
+ Notificacoes Telegram
+ Comissao (sale_fee), Frete (envio) e calculo de repasse/margem.
"""

import os
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

# ---- Configuracao (vem dos secrets do GitHub) ----
CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DIAS = int(os.environ.get("DIAS", "7"))

# Telegram (opcional)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFICAR = os.environ.get("NOTIFICAR", "1") == "1"

API = "https://api.mercadolibre.com"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


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


def estoque_atual_de(sku):
    """Consulta o estoque atual de um SKU na view estoque_atual."""
    if not sku:
        return None
    try:
        r = (sb.table("estoque_atual").select("estoque_atual")
             .eq("sku", sku).limit(1).execute().data)
        if r and r[0].get("estoque_atual") is not None:
            return r[0]["estoque_atual"]
    except Exception:
        return None
    return None


def notificar_vendas_novas():
    if not TG_TOKEN or not TG_CHAT:
        return
    novas = (sb.table("vendas").select("*")
             .eq("notificado", False).eq("status", "paid").execute().data) or []
    if not novas:
        return
    contas = {c["seller_id"]: (c.get("apelido") or c["seller_id"])
              for c in (sb.table("contas").select("seller_id, apelido").execute().data or [])}
    pedidos = defaultdict(list)
    for r in novas:
        pedidos[r["order_id"]].append(r)
    if len(pedidos) > 20:
        total = sum((its[0].get("total") or 0) for its in pedidos.values())
        tg_send(f"🛒 <b>{len(pedidos)} vendas novas!</b>\nTotal: R$ {total:,.2f}")
    else:
        for oid, itens in pedidos.items():
            it0 = itens[0]
            conta = contas.get(it0["seller_id"], it0["seller_id"])
            total = it0.get("total") or 0
            titulo = it0.get("titulo") or "(produto)"
            extra = f" (+{len(itens)-1} item)" if len(itens) > 1 else ""

            # margem do pedido = receita - custo - comissao - frete
            receita = sum((x.get("valor_unitario") or 0) * (x.get("quantidade") or 1) for x in itens)
            comissao = sum((x.get("comissao") or 0) for x in itens)
            frete = sum((x.get("frete") or 0) for x in itens)
            tem_custo = all(x.get("custo_unitario") is not None for x in itens)
            alerta = False
            if tem_custo and receita > 0:
                custo = sum((x.get("custo_unitario") or 0) * (x.get("quantidade") or 1) for x in itens)
                margem = receita - custo - comissao - frete
                pct = margem / receita * 100
                alerta = pct < 17                      # margem abaixo de 17%
                linha_margem = f"💰 Margem: R$ {margem:,.2f} ({pct:.1f}%)"
                if alerta:
                    linha_margem += " 🚨"
            else:
                linha_margem = "💰 Margem: aguardando custo do produto"

            # estoque atual do produto principal
            est = estoque_atual_de(it0.get("sku"))
            linha_estoque = f"\n📦 Estoque atual: {float(est):g}" if est is not None else ""

            cabecalho = ("🚨 <b>Nova venda — MARGEM BAIXA!</b>" if alerta
                         else "🛒 <b>Nova venda!</b>")
            tg_send(f"{cabecalho}\nConta: {conta}\n"
                    f"Valor: R$ {total:,.2f}\nProduto: {titulo}{extra}\n"
                    f"{linha_margem}{linha_estoque}")
    for oid in pedidos:
        sb.table("vendas").update({"notificado": True}).eq("order_id", oid).execute()


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


def tipo_anuncio(lt):
    """gold_pro = Premium, gold_special = Clássico (mantém o resto como veio)."""
    return {"gold_pro": "Premium", "gold_special": "Clássico",
            "gold_premium": "Premium", "gold": "Clássico"}.get(lt, lt)


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
            ship = (o.get("shipping") or {}).get("id")
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
                    "tipo_pagamento": pay.get("payment_type"),
                    "payment_id": str(pay.get("id")) if pay.get("id") else None,
                    "comissao": it.get("sale_fee"),
                    "tipo_anuncio": tipo_anuncio(it.get("listing_type_id")),
                    "shipping_id": str(ship) if ship else None,
                    "item_id": item.get("id"),
                    "sku": item.get("seller_sku") or item.get("seller_custom_field"),
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

    skus = sorted({l["sku"] for l in linhas if l.get("sku")})
    if skus:
        try:
            sb.table("produtos").upsert(
                [{"sku": s} for s in skus],
                on_conflict="sku", ignore_duplicates=True).execute()
        except Exception as e:
            print("Aviso: falha ao cadastrar SKUs novos:", e)

    return len(linhas)


def _select_all(tabela, cols, filtros=None, is_null=None):
    """Lê TODAS as linhas (o PostgREST devolve no máx. 1000 por vez, então
    paginamos com .range até acabar)."""
    out, passo, ini = [], 1000, 0
    while True:
        q = sb.table(tabela).select(cols)
        for k, v in (filtros or {}).items():
            q = q.eq(k, v)
        if is_null:
            q = q.is_(is_null, "null")
        linhas = (q.range(ini, ini + passo - 1).execute().data) or []
        out += linhas
        if len(linhas) < passo:
            break
        ini += passo
    return out


def enriquecer_frete(access, seller_id):
    """Busca o custo do envio e RATEIA entre todos os itens que dividem o mesmo
    envio (inclusive quando são pedidos diferentes de um 'carrinho'/pack).
    Assim o frete nunca é duplicado. Só preenche o que está vazio."""
    pend = (sb.table("vendas").select("shipping_id")
            .eq("seller_id", seller_id).eq("status", "paid")
            .is_("frete", "null").limit(1000).execute().data) or []

    # envios distintos que ainda têm item sem frete
    envios, visto = [], set()
    for r in pend:
        s = r.get("shipping_id")
        if s and s not in visto:
            visto.add(s)
            envios.append(s)
    if not envios:
        return 0

    feitos = 0
    for ship_id in envios:
        if feitos >= 80:        # limita por execucao (respeita rate limit)
            break
        # TODOS os itens desse envio (pode ser de vários pedidos = carrinho)
        itens = (sb.table("vendas")
                 .select("id, valor_unitario, quantidade")
                 .eq("seller_id", seller_id).eq("shipping_id", ship_id)
                 .eq("status", "paid").execute().data) or []
        if not itens:
            continue
        try:
            c = ml_get(f"/shipments/{ship_id}/costs", access)
        except Exception:
            continue
        senders = c.get("senders") or []
        frete_env = 0
        if senders:
            match = [s for s in senders if str(s.get("user_id")) == str(seller_id)]
            frete_env = (match[0] if match else senders[0]).get("cost") or 0

        total_val = sum((it["valor_unitario"] or 0) * (it["quantidade"] or 1)
                        for it in itens) or 1
        for it in itens:
            val = (it["valor_unitario"] or 0) * (it["quantidade"] or 1)
            frete_item = round(frete_env * (val / total_val), 2)
            sb.table("vendas").update({"frete": frete_item}).eq("id", it["id"]).execute()
        feitos += 1
        time.sleep(0.4)
    print(f"Frete: {feitos} envios rateados.")
    return feitos


def enriquecer_local(access, seller_id, limite=80):
    """Preenche UF/estado/cidade do comprador a partir do envio
    (/shipments/{id} -> receiver_address). Só mexe no que está sem UF.
    Resumível: cada envio preenchido sai da fila."""
    pend = _select_all("vendas", "shipping_id",
                       {"seller_id": seller_id, "status": "paid"}, is_null="uf")
    envios, visto = [], set()
    for r in pend:
        s = r.get("shipping_id")
        if s and s not in visto:
            visto.add(s)
            envios.append(s)
    if not envios:
        return 0

    feitos = 0
    for ship_id in envios:
        if feitos >= limite:
            break
        try:
            sj = ml_get(f"/shipments/{ship_id}", access)
        except Exception:
            continue
        ra = (sj or {}).get("receiver_address") or {}
        st = ra.get("state") or {}
        ci = ra.get("city") or {}
        uf = (st.get("id") or "").replace("BR-", "").strip() or "ND"  # 'ND' evita reprocessar sem fim
        dados = {"uf": uf, "estado": st.get("name"), "cidade": ci.get("name")}
        sb.table("vendas").update(dados).eq("seller_id", seller_id) \
            .eq("shipping_id", ship_id).eq("status", "paid").execute()
        feitos += 1
        time.sleep(0.3)
    print(f"Local: {feitos} envios localizados.")
    return feitos


def enriquecer_repasse(access, seller_id, limite=80):
    """Para pedidos sem repasse registrado, busca o valor líquido em
    /collections/{payment_id} e grava na tabela 'repasses'.
    'limite' = quantos pedidos NOVOS processar por execução (no mutirão usamos
    um número alto pra terminar de uma vez)."""
    # IMPORTANTE: paginar (o PostgREST corta em 1000 linhas por consulta).
    vds = _select_all("vendas", "order_id, payment_id",
                      {"seller_id": seller_id, "status": "paid"})
    ja = set(r["order_id"] for r in
             _select_all("repasses", "order_id", {"seller_id": seller_id}))
    pares = {}
    for v in vds:
        oid, pid = v.get("order_id"), v.get("payment_id")
        if oid and pid and oid not in ja:
            pares[oid] = pid

    feitos = 0
    for oid, pid in pares.items():
        if feitos >= limite:
            break
        try:
            c = ml_get(f"/collections/{pid}", access)
        except Exception:
            continue
        if not isinstance(c, dict) or c.get("net_received_amount") is None:
            continue
        mrd = c.get("money_release_date")
        dap = c.get("date_approved") or c.get("date_created")

        # rebate = parte do desconto de campanha financiada pelo ML (total - seller)
        rebate = 0
        try:
            disc = ml_get(f"/orders/{oid}/discounts", access)
            for det in (disc.get("details") or []):
                if det.get("supplier"):
                    for itx in (det.get("items") or []):
                        amts = itx.get("amounts") or {}
                        rebate += max((amts.get("total") or 0) - (amts.get("seller") or 0), 0)
        except Exception:
            pass

        sb.table("repasses").upsert({
            "order_id": str(oid),
            "payment_id": str(pid),
            "seller_id": seller_id,
            "transaction_amount": c.get("transaction_amount"),
            "net_received_amount": c.get("net_received_amount"),
            "money_release_date": mrd[:10] if mrd else None,
            "data_pagamento": dap[:10] if dap else None,
            "released": c.get("released"),
            "amount_refunded": c.get("amount_refunded"),
            "status": c.get("status"),
            "rebate": round(rebate, 2),
        }, on_conflict="order_id").execute()
        feitos += 1
        time.sleep(0.3)
    print(f"Repasse: {feitos} pedidos registrados.")
    return feitos


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
        try:
            enriquecer_frete(access, sid)
        except Exception as e:
            print("Aviso: falha no frete:", e)
        try:
            enriquecer_repasse(access, sid)
        except Exception as e:
            print("Aviso: falha no repasse:", e)
        try:
            enriquecer_local(access, sid)
        except Exception as e:
            print("Aviso: falha na localizacao:", e)
        print(f"[{sid}] {n} vendas atualizadas em {datetime.now()}")

    if NOTIFICAR:
        try:
            notificar_vendas_novas()
        except Exception as e:
            print("Aviso: falha na notificacao:", e)


if __name__ == "__main__":
    main()
