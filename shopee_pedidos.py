"""
Robo de pedidos da Shopee -> Supabase  (versao com DIAGNOSTICO).

Diferencas pra versao anterior (que retornava "0 pedidos" sem explicar):
  1) MOSTRA o erro da Shopee (error/message) em vez de engolir.
  2) Janela de 13 dias (a Shopee recusa quando chega em 15).
  3) Renova o token durante backfills longos (o access dura ~4h).
  4) Percorre TODAS as lojas de shopee_contas, nao so a primeira.
  5) No fim, imprime um resumo do que aconteceu em cada etapa.

ENV: SHOPEE_PARTNER_ID, SHOPEE_PARTNER_KEY, SUPABASE_URL, SUPABASE_KEY
     DIAS (padrao 15)  |  DEBUG=1 (imprime a resposta crua da 1a janela)
"""
import os
import sys
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# Log AO VIVO no GitHub Actions: sem isto o Python segura os prints em buffer
# e o log so aparece quando o robo termina.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

HOST = "https://partner.shopeemobile.com"
PID = int(os.environ.get("SHOPEE_PARTNER_ID") or "2039646")
PKEY = os.environ["SHOPEE_PARTNER_KEY"].encode()
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
DIAS = int(os.environ.get("DIAS", "15"))
DEBUG = os.environ.get("DEBUG", "0") == "1"

JANELA_DIAS = 13          # < 15, senao a Shopee devolve error_param
REFRESH_A_CADA = 45 * 60  # renova o token a cada 45 min de execucao

_ultima = [0.0]


def _freio():
    esp = 0.4 - (time.time() - _ultima[0])
    if esp > 0:
        time.sleep(esp)
    _ultima[0] = time.time()


def http(method, url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, json.dumps({"error": "rede", "message": str(e)})


def sb_get(path):
    st, raw = http("GET", f"{SB_URL}/rest/v1/{path}",
                   {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    if st >= 300:
        raise RuntimeError(f"Supabase GET {path} -> HTTP {st}: {raw[:200]}")
    return json.loads(raw) if raw else []


def sb_upsert(tabela, rows, conflito):
    for i in range(0, len(rows), 200):
        st, raw = http("POST", f"{SB_URL}/rest/v1/{tabela}?on_conflict={conflito}", {
            "apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }, json.dumps(rows[i:i + 200]).encode())
        if st >= 300:
            raise RuntimeError(f"Supabase {tabela} HTTP {st}: {raw[:300]}")


def sb_rpc(fn):
    """Chama uma funcao do banco (ex.: a que joga shopee_vendas -> vendas)."""
    st, raw = http("POST", f"{SB_URL}/rest/v1/rpc/{fn}", {
        "apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
        "Content-Type": "application/json",
    }, b"{}")
    return st, raw


def sb_patch(shop_id, fields):
    http("PATCH", f"{SB_URL}/rest/v1/shopee_contas?shop_id=eq.{shop_id}",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
          "Content-Type": "application/json", "Prefer": "return=minimal"},
         json.dumps(fields).encode())


def sign_public(path, ts):
    return hmac.new(PKEY, f"{PID}{path}{ts}".encode(), hashlib.sha256).hexdigest()


def sign_shop(path, ts, token, shop_id):
    return hmac.new(PKEY, f"{PID}{path}{ts}{token}{shop_id}".encode(), hashlib.sha256).hexdigest()


def refresh(shop_id, refresh_token):
    ts = int(time.time())
    path = "/api/v2/auth/access_token/get"
    url = f"{HOST}{path}?partner_id={PID}&timestamp={ts}&sign={sign_public(path, ts)}"
    st, raw = http("POST", url, {"Content-Type": "application/json"},
                   json.dumps({"refresh_token": refresh_token, "shop_id": shop_id,
                               "partner_id": PID}).encode())
    d = json.loads(raw) if raw else {}
    if not d.get("access_token"):
        raise RuntimeError(f"Falha no refresh (HTTP {st}): {d}")
    expira = (datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expire_in", 14400)))).isoformat()
    fields = {"access_token": d["access_token"], "access_expira_em": expira}
    if d.get("refresh_token"):
        fields["refresh_token"] = d["refresh_token"]
    sb_patch(shop_id, fields)
    return d["access_token"]


def shop_get(path, token, shop_id, extra):
    """Chamada autenticada. Devolve (dados, erro_legivel)."""
    _freio()
    ts = int(time.time())
    params = {"partner_id": PID, "timestamp": ts, "access_token": token,
              "shop_id": shop_id, "sign": sign_shop(path, ts, token, shop_id)}
    params.update(extra)
    st, raw = http("GET", f"{HOST}{path}?" + urllib.parse.urlencode(params),
                   {"Content-Type": "application/json"})
    try:
        d = json.loads(raw) if raw else {}
    except Exception:
        return {}, f"HTTP {st}: resposta nao-JSON: {raw[:200]}"
    err = d.get("error")
    if err:
        return d, f"{err} — {d.get('message', '')}"
    if st >= 300:
        return d, f"HTTP {st}: {raw[:200]}"
    return d, None


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None


def listar_order_sn(ctx):
    """{order_sn: status} do periodo. AGORA mostra o erro se a Shopee recusar."""
    agora = int(time.time())
    inicio = agora - DIAS * 24 * 3600
    achados, erros, primeira = {}, [], True
    janela = inicio
    while janela < agora:
        fim = min(janela + JANELA_DIAS * 24 * 3600, agora)
        cursor = ""
        while True:
            token = ctx_token(ctx)
            d, err = shop_get("/api/v2/order/get_order_list", token, ctx["shop_id"], {
                "time_range_field": "create_time", "time_from": janela, "time_to": fim,
                "page_size": 100, "cursor": cursor, "response_optional_fields": "order_status",
            })
            if DEBUG and primeira:
                print("  [debug] resposta crua da 1a janela:", json.dumps(d)[:600])
                primeira = False
            if err:
                per = f"{datetime.fromtimestamp(janela).date()} a {datetime.fromtimestamp(fim).date()}"
                print(f"  ERRO da Shopee em {per}: {err}")
                erros.append(err)
                break
            resp = d.get("response") or {}
            lote = resp.get("order_list", []) or []
            for o in lote:
                if o.get("order_sn"):
                    achados[o["order_sn"]] = o.get("order_status")
            if resp.get("more") and resp.get("next_cursor"):
                cursor = resp["next_cursor"]
            else:
                break
        janela = fim
    return achados, erros


def ctx_token(ctx):
    """Token valido: renova sozinho em execucoes longas."""
    if time.time() - ctx["t0"] > REFRESH_A_CADA:
        ctx["token"] = refresh(ctx["shop_id"], ctx["refresh_token"])
        ctx["t0"] = time.time()
        print("  (token renovado no meio da execucao)")
    return ctx["token"]


def processar_loja(c):
    shop_id = c["shop_id"]
    print(f"\n=== Loja {shop_id} ===")
    token = refresh(shop_id, c["refresh_token"])
    ctx = {"shop_id": shop_id, "refresh_token": c["refresh_token"], "token": token, "t0": time.time()}
    print(f"token OK. Puxando {DIAS} dias (janelas de {JANELA_DIAS})...")

    sns, erros = listar_order_sn(ctx)
    print(f"{len(sns)} pedido(s) encontrado(s).")
    if not sns:
        if erros:
            print("  >> A Shopee RECUSOU as chamadas. Causas comuns:")
            print("     error_auth / error_permission -> app sem permissao de Pedidos,")
            print("       ou a loja precisa reautorizar o app (refresh_token velho).")
            print("     error_param -> janela de datas invalida.")
            print("     error_sign  -> partner_id/partner_key errados.")
        else:
            print("  >> Sem erro da Shopee: nao existem pedidos nesse periodo.")
            print("     Tente aumentar DIAS (ex.: DIAS=90) ou confira se e a loja certa.")
        return 0, 0

    lista = list(sns.keys())
    vendas, repasses = [], []
    NAO_PAGO = {"CANCELLED", "UNPAID", "INVOICE_PENDING"}

    falhas_det = 0
    for i in range(0, len(lista), 50):
        lote = lista[i:i + 50]
        d, err = shop_get("/api/v2/order/get_order_detail", ctx_token(ctx), shop_id, {
            "order_sn_list": ",".join(lote),
            "response_optional_fields": "item_list,total_amount,order_status,create_time,buyer_username,payment_method,recipient_address",
        })
        if err:
            falhas_det += 1
            print(f"  ERRO no detalhe (lote {i//50+1}): {err}")
            continue
        for o in (d.get("response") or {}).get("order_list", []):
            osn = o.get("order_sn")
            data = iso(o.get("create_time"))
            end = o.get("recipient_address") or {}
            for it in o.get("item_list", []):
                # foto do produto (a Shopee manda em image_info.image_url)
                img = ((it.get("image_info") or {}).get("image_url")) or None
                vendas.append({
                    "order_sn": osn, "item_id": it.get("item_id"),
                    "model_id": it.get("model_id") or 0, "shop_id": shop_id,
                    "status": o.get("order_status"), "data": data,
                    "sku": it.get("model_sku") or it.get("item_sku") or "",
                    "titulo": it.get("item_name"),
                    "quantidade": it.get("model_quantity_purchased"),
                    "valor_unitario": it.get("model_discounted_price"),
                    "forma_pagamento": o.get("payment_method"),
                    "comprador": o.get("buyer_username"),
                    "total_pedido": o.get("total_amount"),
                    # --- novos: foto e endereco, pro detalhe do pedido no app ---
                    "thumbnail": img,
                    "variacao": it.get("model_name") or None,
                    "receptor_nome": end.get("name"),
                    "receptor_telefone": end.get("phone"),
                    "bairro": end.get("district"),
                    "cep": end.get("zipcode"),
                    "cidade": end.get("city"),
                    "estado": end.get("state"),
                })
    if vendas:
        sb_upsert("shopee_vendas", vendas, "order_sn,item_id,model_id")

    pagos = [sn for sn, stt in sns.items() if stt not in NAO_PAGO]
    falhas_esc = 0
    for sn in pagos:
        d, err = shop_get("/api/v2/payment/get_escrow_detail", ctx_token(ctx), shop_id, {"order_sn": sn})
        if err:
            falhas_esc += 1
            if falhas_esc <= 3:
                print(f"  ERRO no escrow de {sn}: {err}")
            continue
        resp = d.get("response") or {}
        oi = resp.get("order_income") or {}
        if not oi:
            continue
        repasses.append({
            "order_sn": sn, "shop_id": shop_id, "status": sns.get(sn),
            "data": iso(resp.get("create_time")),
            "buyer_total": oi.get("buyer_total_amount"),
            "comissao": oi.get("commission_fee"),
            "service_fee": oi.get("net_service_fee"),
            "frete": oi.get("final_shipping_fee") or oi.get("actual_shipping_fee"),
            "repasse": oi.get("escrow_amount_after_adjustment") or oi.get("escrow_amount"),
        })
    if repasses:
        sb_upsert("shopee_repasses", repasses, "order_sn")

    tot = sum((r["repasse"] or 0) for r in repasses)
    print(f"OK: {len(vendas)} itens de venda, {len(repasses)} repasses. Liquido: R$ {tot:,.2f}")
    if falhas_det:
        print(f"  atencao: {falhas_det} lote(s) de detalhe falharam")
    if falhas_esc:
        print(f"  atencao: {falhas_esc} escrow(s) falharam (comum em pedidos muito recentes)")
    return len(vendas), len(repasses)


def main():
    contas = sb_get("shopee_contas?select=shop_id,refresh_token")
    if not contas:
        raise SystemExit("Nenhuma loja em shopee_contas.")
    print(f"{len(contas)} loja(s) cadastrada(s).")
    tv = tr = 0
    for c in contas:                     # todas as lojas, nao so a primeira
        try:
            v, r = processar_loja(c)
            tv += v
            tr += r
        except Exception as e:
            print(f"ERRO na loja {c.get('shop_id')}: {e}")
    print(f"\nTOTAL: {tv} itens de venda, {tr} repasses.")

    # ESSENCIAL: joga o que caiu em shopee_vendas/shopee_repasses para a tabela
    # que o painel le. Sem isto os pedidos ficam parados e nao aparecem no BI.
    # (o webhook ja faz isso a cada venda; aqui e pro lote/backfill)
    st, raw = sb_rpc("shopee_atualizar_vendas")
    if st < 300:
        print("Painel atualizado (shopee_atualizar_vendas OK).")
    else:
        print(f"ATENCAO: shopee_atualizar_vendas falhou (HTTP {st}): {raw[:300]}")
        print("  -> por isso os pedidos podem nao aparecer no painel.")


if __name__ == "__main__":
    main()
