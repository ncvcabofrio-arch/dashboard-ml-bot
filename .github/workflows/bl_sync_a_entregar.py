"""
VENDIDO A ENTREGAR -> Supabase (roda de hora em hora).

Le os pedidos da BaseLinker, soma por SKU as unidades que estao nos status
"ainda na minha prateleira" (BL_STATUS_A_ENTREGAR) e grava na tabela
base_a_entregar. E' de la' que a Edge Function 'base_estoque' tira o aviso
que aparece na tela do app de contagem.

NAO mexe em estoque, custo, nem em produtos. So' escreve nesta tabela.

Por que uma tabela separada e nao uma coluna em base_produtos
-------------------------------------------------------------
Porque a lista e' pequena e volatil: hoje 44 SKUs, amanha outros. Numa
coluna, todo SKU sem pendencia precisaria ser zerado a cada rodada — 1200
gravacoes para dizer "nada mudou". Aqui a regra e' simples: se o SKU esta
na tabela, tem venda a entregar; se nao esta, nao tem.

Variaveis
   BASELINKER_TOKEN        obrigatorio
   SUPABASE_URL            obrigatorio (https://xxx.supabase.co)
   SUPABASE_KEY            obrigatorio (service_role)
   BL_STATUS_A_ENTREGAR    ids de status, separados por virgula (padrao 373553)
   BL_DIAS                 dias de pedidos para tras (padrao 60)
   BL_INCLUIR_NAO_CONFIRMADOS  1 = traz tambem pedidos nao confirmados
   BL_DRY_RUN              1 = mostra o que faria e NAO grava
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.baselinker.com/connector.php"
TOKEN = (os.environ.get("BASELINKER_TOKEN") or "").strip()
DIAS = int(os.environ.get("BL_DIAS", "60") or 60)
NAO_CONFIRMADOS = os.environ.get("BL_INCLUIR_NAO_CONFIRMADOS", "0") == "1"
DRY_RUN = os.environ.get("BL_DRY_RUN", "0") == "1"
STATUS = [s.strip() for s in
          os.environ.get("BL_STATUS_A_ENTREGAR", "373553").split(",") if s.strip()]

TABELA = "base_a_entregar"


def _limpar_url(bruto):
    """O que voce digita costuma vir com espaco no fim, aspas ou ja com
    /rest/v1 colado — e o PostgREST responde PGRST125 sem explicar nada."""
    u = (bruto or "").strip().strip('"').strip("'").strip().rstrip("/")
    for sufixo in ("/rest/v1", "/rest"):
        if u.lower().endswith(sufixo):
            u = u[: -len(sufixo)].rstrip("/")
    return u


SUPABASE_URL = _limpar_url(os.environ.get("SUPABASE_URL", ""))
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY", "") or "").strip()

if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERRO] Faltam BASELINKER_TOKEN, SUPABASE_URL ou SUPABASE_KEY.")
    sys.exit(1)

if not STATUS:
    print("[ERRO] BL_STATUS_A_ENTREGAR vazio: eu nao teria como saber quais")
    print("       pedidos ainda estao com voce. Nao vou gravar nada.")
    sys.exit(1)

H = {"apikey": SUPABASE_KEY,
     "Authorization": "Bearer " + SUPABASE_KEY,
     "Content-Type": "application/json"}


# ----------------------------------------------------------------- BaseLinker

def chamar(metodo, parametros=None, tentativas=4):
    corpo = {"method": metodo,
             "parameters": json.dumps(parametros or {}, ensure_ascii=False)}
    for t in range(tentativas):
        try:
            r = requests.post(API, headers={"X-BLToken": TOKEN}, data=corpo, timeout=90)
        except requests.RequestException as e:
            if t == tentativas - 1:
                raise
            print(f"   rede falhou ({e}); tentando de novo...")
            time.sleep(2 * (t + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(3 * (t + 1))
            continue
        d = r.json()
        if d.get("status") == "ERROR":
            code = d.get("error_code", "")
            if code in ("ERROR_RATE_LIMIT", "ERROR_INTERNAL") and t < tentativas - 1:
                time.sleep(3 * (t + 1))
                continue
            raise RuntimeError(f"{metodo}: {code} — {d.get('error_message')}")
        return d
    raise RuntimeError(f"{metodo}: sem resposta")


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def limpo(n):
    n = float(n)
    return int(n) if n.is_integer() else round(n, 2)


def baixar_pedidos(desde_ts):
    """getOrders vem de 100 em 100; a paginacao e' pelo date_confirmed do
    ultimo + 1s. Se os 100 tiverem a MESMA data o cursor nao anda, entao o
    +1 e' forcado; e o controle de ja vistos evita contar duas vezes."""
    pedidos, vistos, cursor, paginas = [], set(), int(desde_ts), 0
    while True:
        params = {"date_confirmed_from": int(cursor)}
        if NAO_CONFIRMADOS:
            params["get_unconfirmed_orders"] = True
        lote = chamar("getOrders", params).get("orders", []) or []
        novos = [o for o in lote if str(o.get("order_id")) not in vistos]
        for o in novos:
            vistos.add(str(o.get("order_id")))
        pedidos.extend(novos)
        paginas += 1
        if len(lote) < 100:
            break
        cursor = max(max(int(num(o.get("date_confirmed"))) for o in lote) + 1,
                     cursor + 1)
        if paginas >= 300:
            print("  ⚠️ parei em 300 paginas por seguranca.")
            break
        time.sleep(0.65)
    print(f"  {len(pedidos)} pedidos lidos em {paginas} pagina(s).")
    return pedidos


# ------------------------------------------------------------------- Supabase

def sb(metodo, caminho, **kw):
    url = f"{SUPABASE_URL}/rest/v1/{caminho}"
    # quem chama pode acrescentar cabecalho (o Prefer do upsert, por exemplo)
    cabecalho = {**H, **(kw.pop("headers", None) or {})}
    for t in range(3):
        try:
            r = requests.request(metodo, url, headers=cabecalho, timeout=60, **kw)
        except requests.RequestException:
            if t == 2:
                raise
            time.sleep(2 * (t + 1))
            continue
        if r.status_code >= 400:
            print(f"[ERRO] Supabase {r.status_code}: {r.text[:300]}")
            print(f"   chamada: {metodo} {url[:150]}")
            if "PGRST205" in r.text:
                print(f"   -> a tabela '{TABELA}' nao existe. Rode o "
                      f"sql_a_entregar.sql no SQL Editor primeiro.")
            if "PGRST125" in r.text:
                print("   -> SUPABASE_URL malformada. Deve ser so'")
                print("      https://SEUPROJETO.supabase.co")
            sys.exit(1)
        return r
    raise RuntimeError("Supabase nao respondeu")


def skus_atuais():
    r = sb("GET", f"{TABELA}?select=sku")
    return {str(l["sku"]) for l in (r.json() or []) if l.get("sku")}


# ----------------------------------------------------------------------- main

def main():
    desde = datetime.now(timezone.utc) - timedelta(days=DIAS)
    print(f"Pedidos desde {desde:%d/%m/%Y} ({DIAS} dias) | "
          f"status a entregar: {', '.join(STATUS)}"
          + ("  [DRY RUN: nao grava nada]" if DRY_RUN else ""))

    pedidos = baixar_pedidos(desde.timestamp())

    alvo = set(STATUS)
    unidades, contagem_pedidos, prod_id = {}, {}, {}
    for o in pedidos:
        if str(o.get("order_status_id")) not in alvo:
            continue
        skus_do_pedido = set()
        for p in o.get("products", []) or []:
            sku = str(p.get("sku") or "").strip()
            if not sku:
                continue
            unidades[sku] = unidades.get(sku, 0.0) + num(p.get("quantity"))
            skus_do_pedido.add(sku)
            pid = str(p.get("product_id") or "").strip()
            if pid and pid not in ("0", "None"):
                prod_id.setdefault(sku, pid)
        for sku in skus_do_pedido:
            contagem_pedidos[sku] = contagem_pedidos.get(sku, 0) + 1

    agora = datetime.now(timezone.utc).isoformat()
    linhas = [{"sku": s,
               "product_id": prod_id.get(s),
               "unidades": limpo(q),
               "pedidos": contagem_pedidos.get(s, 0),
               "atualizado_em": agora}
              for s, q in unidades.items() if q > 0]
    linhas.sort(key=lambda l: -float(l["unidades"]))

    print(f"\n  {len(linhas)} SKU(s) com venda a entregar, "
          f"{limpo(sum(float(l['unidades']) for l in linhas))} unidades.")
    for l in linhas[:10]:
        print(f"     {l['sku']:<20} {l['unidades']:>4}  "
              f"({l['pedidos']} pedido(s))")

    # Trava: coleta vazia pode ser verdade (nenhuma pendencia) ou pode ser a
    # API tendo devolvido lixo. Se ontem havia dezenas e hoje ha zero, e' mais
    # provavel ser falha do que a operacao ter zerado — nao apago sem avisar.
    ja_tinha = skus_atuais()
    if not linhas and len(ja_tinha) > 5:
        print(f"\n⚠️ Nenhuma pendencia agora, mas a tabela tem {len(ja_tinha)} "
              f"SKU(s). Isso pode ser real (tudo despachado) ou falha de "
              f"leitura. NAO vou apagar. Rode com BL_DRY_RUN=0 e "
              f"BL_PERMITIR_ZERAR=1 se quiser mesmo limpar.")
        if os.environ.get("BL_PERMITIR_ZERAR", "0") != "1":
            return

    if DRY_RUN:
        print("\n[DRY RUN] nada gravado.")
        return

    if linhas:
        sb("POST", TABELA,
           headers={"Prefer": "resolution=merge-duplicates"},
           data=json.dumps(linhas, ensure_ascii=False).encode("utf-8"))
        print(f"\n✅ {len(linhas)} linha(s) gravada(s) em {TABELA}.")

    # SKU que saiu da pendencia (foi despachado) tem que sumir da tabela,
    # senao o app avisaria para sempre sobre venda que ja saiu.
    sair = ja_tinha - {l["sku"] for l in linhas}
    if sair:
        lista = ",".join('"' + s.replace('"', '') + '"' for s in sorted(sair))
        if len(lista) > 6000:
            print(f"   ⚠️ {len(sair)} SKU(s) para remover: lista grande demais "
                  f"para uma chamada so'. Removo os primeiros nesta rodada.")
            lista = lista[:6000].rsplit(",", 1)[0]
        sb("DELETE", f"{TABELA}?sku=in.({lista})")
        print(f"   {len(sair)} SKU(s) sem pendencia removido(s).")

    print(f"   carimbo atualizado_em = {agora}")


if __name__ == "__main__":
    main()
