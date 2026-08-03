#!/usr/bin/env python3
"""
repricer_vendas.py - o puxador de vendas DO REPRICER.

=======================================================================
POR QUE ELE EXISTE

O repricer precisa saber o que vendeu nos ultimos dias. Ate hoje ele lia
a tabela 'vendas', que e a base do BI, do DRE e do App Vendas e que quem
enche e o puxador.py.

Isso amarrou dois sistemas que nao tem nada a ver um com o outro: ligar
uma conta de CLIENTE no repricer obrigava a puxar os pedidos dela para
dentro do financeiro da casa. Nao vazava nada pra fora - contaminava pra
dentro, somando o faturamento do cliente ao seu.

Este robo corta esse no. Ele enche a repricer_vendas, que e do repricer,
para TODAS as contas com token - inclusive as suas. O puxador.py continua
sendo do BI e passa a puxar so a org da casa.

=======================================================================
O QUE ELE PUXA, E O QUE NAO PUXA

So o que o repricer consome de verdade:

    item_id, sku, quantidade, status, data_aprovacao, rebate

Nada de comprador, valor, frete, comissao ou repasse. Nao e economia de
espaco: e que dado que ninguem usa vira dado que ninguem confere, e um
dia alguem o usa achando que esta certo.

=======================================================================
VARIAVEIS DE AMBIENTE (as mesmas do puxador.py)

    ML_CLIENT_ID, ML_CLIENT_SECRET   credenciais do aplicativo
    SUPABASE_URL, SUPABASE_KEY       service_role
    DIAS                             janela em dias (padrao 35)
    SO_SELLER                        se preenchido, roda so essa conta

A janela padrao de 35 dias cobre com folga as janelas que o repricer usa
hoje (o piloto olha 5 dias, o painel olha um corte curto). Cobrir com
folga custa pouco e evita o dia em que alguem aumenta a janela do piloto
e o dado nao esta la.
"""

import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

API = "https://api.mercadolibre.com"

CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DIAS = int(os.environ.get("DIAS", "35"))
SO_SELLER = os.environ.get("SO_SELLER", "").strip()
# o rebate custa uma chamada de API por pedido, entao ele e limitado em
# janela e em teto por rodada - ver enriquecer_rebate()
REBATE_DIAS = int(os.environ.get("REBATE_DIAS", "7"))
REBATE_MAX = int(os.environ.get("REBATE_MAX", "300"))

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# status que significam "nao vendeu de verdade". O piloto ja descarta
# esses na leitura; guardo mesmo assim, com o status junto, para quem ler
# a tabela poder decidir - e para dar para conferir depois por que um
# anuncio foi considerado parado.
CANCELADOS = ("cancelled", "invalid")


# ---------------------------------------------------------------- ML ---
def renovar_token(refresh_token):
    """Troca o refresh_token por um access_token.

    ATENCAO: o Mercado Livre devolve um refresh_token NOVO a cada
    renovacao, e invalida o anterior - e uso unico. Este robo NAO grava o
    novo de volta, de proposito: quem e dono da rotacao do token e o
    puxador do BI (via ml_auth). Dois robos gravando o mesmo campo em
    paralelo e receita para um derrubar o token do outro.

    Consequencia pratica: se o ML um dia passar a recusar o refresh_token
    antigo depois de rotacionado, este robo para de funcionar e o do BI
    continua. E o lado certo para falhar - o BI e o que nao pode parar.
    Se isso acontecer, a saida e este robo passar a pedir o access_token
    ao mesmo lugar que o outro, em vez de renovar por conta propria.
    """
    r = requests.post(API + "/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Falha ao renovar token: " + str(d)[:200])
    return d["access_token"]


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
        return {}


# ------------------------------------------------------------ contas ---
def contas():
    """Todas as contas com token - de qualquer org.

    Aqui e o contrario do puxador.py de proposito: o BI puxa so a casa, o
    repricer puxa todo mundo que ele precifica. Sao finalidades opostas e
    e por isso que sao dois robos.
    """
    try:
        res = sb.table("contas").select("seller_id, refresh_token, org, apelido").execute()
    except Exception:
        res = sb.table("contas").select("seller_id, refresh_token, apelido").execute()

    fora = []
    saida = []
    for c in (res.data or []):
        sid = str(c.get("seller_id") or "")
        if not sid or not c.get("refresh_token"):
            fora.append(f"{c.get('apelido') or sid}: sem token")
            continue
        if SO_SELLER and sid != SO_SELLER:
            continue
        saida.append((sid, c["refresh_token"], c.get("org"), c.get("apelido") or sid))

    for f in fora:
        print("  pulei -", f)
    return saida


# ----------------------------------------------------------- pedidos ---
def linhas_do_pedido(o, seller_id, org):
    """Uma linha por item do pedido, so com o que o repricer usa."""
    oid = str(o.get("id") or "")
    if not oid:
        return []

    # data_aprovacao: o mesmo campo que o piloto e o painel filtram.
    # date_closed e quando o pedido fechou; se faltar, caio no
    # date_created para a linha nao ficar sem data (linha sem data some
    # de todo filtro e vira invisivel, que e pior que estar errada por
    # algumas horas).
    data = o.get("date_closed") or o.get("date_created")

    status = str(o.get("status") or "")
    linhas = []
    for it in o.get("order_items", []) or []:
        item = it.get("item") or {}
        iid = item.get("id")
        if not iid:
            continue
        try:
            qtd = int(it.get("quantity") or 0)
        except (TypeError, ValueError):
            qtd = 0
        linhas.append({
            "seller_id": seller_id,
            "order_id": oid,
            "item_id": str(iid),
            "org": org,
            "sku": item.get("seller_sku") or item.get("seller_custom_field"),
            "quantidade": qtd,
            "status": status,
            "data_aprovacao": data,
            # rebate nao vem no pedido: e preenchido depois, por
            # enriquecer_rebate(), numa segunda chamada por pedido
            "rebate": None,
            "atualizado_em": datetime.now(timezone.utc).isoformat(),
        })
    return linhas


def rebate_do_pedido(oid, access):
    """O desconto que o Mercado Livre bancou no pedido.

    Este numero NAO vem no pedido. Vem de uma segunda chamada,
    /orders/{id}/discounts, somando (total - seller) dos descontos em que
    existe um 'supplier' - ou seja, os que alguem que nao voce bancou. E
    exatamente o que o puxador.py do BI ja faz; copiei a conta de la em
    vez de inventar outra, senao os dois numeros divergiriam e ninguem
    saberia qual esta certo.

    Minha primeira versao procurava um campo de rebate dentro do item do
    pedido. Nao existe: a conferencia veio com 0 de 1405 preenchidos, o
    que teria virado uma coluna morta no painel.

    Devolve None quando a chamada falha - None e 'nao sei', e o painel
    mostra vazio. Zero seria afirmar que o ML nao bancou nada, que e
    outra coisa.
    """
    try:
        disc = ml_get(f"/orders/{oid}/discounts", access)
    except Exception:
        return None
    if not isinstance(disc, dict):
        return None
    total = 0.0
    for det in (disc.get("details") or []):
        if not det.get("supplier"):
            continue
        for itx in (det.get("items") or []):
            amts = itx.get("amounts") or {}
            total += max((amts.get("total") or 0) - (amts.get("seller") or 0), 0)
    return round(total, 2)


def enriquecer_rebate(seller_id, access, apelido):
    """Preenche o rebate dos pedidos recentes que ainda nao tem.

    E uma chamada de API POR PEDIDO, entao ela e limitada de proposito em
    duas dimensoes:

      REBATE_DIAS  so os pedidos recentes - e o unico intervalo que o
                   painel mostra. Rebate de 30 dias atras nao muda
                   decisao nenhuma.
      REBATE_MAX   teto por rodada. Se sobrar, a proxima rodada pega o
                   resto, porque so busco quem esta com rebate nulo.

    Assim o custo por hora fica previsivel, e o robo se acerta sozinho
    depois de um dia parado, em vez de tentar mil chamadas de uma vez.
    """
    corte = (datetime.now(timezone.utc) - timedelta(days=REBATE_DIAS)).isoformat()
    try:
        pend = (sb.table("repricer_vendas")
                .select("order_id")
                .eq("seller_id", seller_id)
                .is_("rebate", "null")
                .gte("data_aprovacao", corte)
                .order("data_aprovacao", desc=True)
                .limit(REBATE_MAX).execute().data) or []
    except Exception as e:
        print(f"  {apelido}: nao consegui listar pedidos sem rebate - {str(e)[:120]}")
        return 0

    pedidos = sorted({str(p["order_id"]) for p in pend})
    if not pedidos:
        return 0

    feitos = 0
    for oid in pedidos:
        v = rebate_do_pedido(oid, access)
        if v is None:
            continue
        try:
            (sb.table("repricer_vendas").update({"rebate": v})
               .eq("seller_id", seller_id).eq("order_id", oid).execute())
            feitos += 1
        except Exception:
            pass
        time.sleep(0.15)

    print(f"  {apelido}: rebate preenchido em {feitos} de {len(pedidos)} pedido(s)"
          + (f" (teto de {REBATE_MAX} por rodada)" if len(pedidos) >= REBATE_MAX else ""))
    return feitos


def puxar(seller_id, access, org, apelido):
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    offset, total, linhas = 0, 1, []

    while offset < total and offset < 5000:
        path = ("/orders/search?seller=" + seller_id +
                "&order.date_created.from=" + urllib.parse.quote(desde) +
                "&sort=date_desc&limit=50&offset=" + str(offset))
        data = ml_get(path, access)
        total = (data.get("paging") or {}).get("total", 0)
        resultados = data.get("results", []) or []
        if not resultados:
            break
        for o in resultados:
            linhas.extend(linhas_do_pedido(o, seller_id, org))
        offset += 50
        time.sleep(0.3)

    if not linhas:
        print(f"  {apelido}: nenhum pedido na janela de {DIAS} dias")
        return 0

    # dedup dentro do proprio lote: o mesmo (pedido, item) pode voltar
    # duas vezes na paginacao se um pedido novo entrar no meio. Sem isso,
    # o upsert falha com 'ON CONFLICT afeta a linha duas vezes'.
    unicas = {}
    for l in linhas:
        unicas[(l["seller_id"], l["order_id"], l["item_id"])] = l
    lote = list(unicas.values())

    for i in range(0, len(lote), 200):
        sb.table("repricer_vendas").upsert(
            lote[i:i + 200], on_conflict="seller_id,order_id,item_id").execute()

    print(f"  {apelido}: {len(lote)} linha(s) de {total} pedido(s)")
    return len(lote)


def limpar_janela():
    """Apaga o que saiu da janela.

    Esta tabela e uma FOTO DOS ULTIMOS DIAS, nao um historico - o
    historico e a tabela vendas, do BI. Sem esta limpeza ela cresceria
    para sempre guardando coisa que ninguem le, e um dia alguem a
    confundiria com a fonte da verdade.
    """
    corte = (datetime.now(timezone.utc) - timedelta(days=DIAS + 5)).isoformat()
    try:
        sb.table("repricer_vendas").delete().lt("data_aprovacao", corte).execute()
        print(f"Limpeza: removi linhas anteriores a {corte[:10]}")
    except Exception as e:
        print("Aviso: falha na limpeza da janela:", str(e)[:150])


def main():
    print(f"repricer_vendas - janela de {DIAS} dias"
          + (f" - SO a conta {SO_SELLER}" if SO_SELLER else ""))

    lista = contas()
    if not lista:
        raise SystemExit("Nenhuma conta com token. Conecte pelo painel (Integracoes).")

    total = 0
    for seller_id, refresh, org, apelido in lista:
        try:
            access = renovar_token(refresh)
        except Exception as e:
            # uma conta com token vencido nao pode derrubar as outras
            print(f"  {apelido}: nao consegui renovar o token - {str(e)[:120]}")
            continue
        try:
            total += puxar(seller_id, access, org, apelido)
        except Exception as e:
            print(f"  {apelido}: falhou ao puxar - {str(e)[:150]}")
            continue
        try:
            enriquecer_rebate(seller_id, access, apelido)
        except Exception as e:
            # rebate e enfeite de tela: se falhar, as vendas ja estao
            # gravadas e o portao do piloto continua funcionando
            print(f"  {apelido}: falhou no rebate - {str(e)[:150]}")

    limpar_janela()
    print(f"Pronto: {total} linha(s) gravada(s).")


if __name__ == "__main__":
    main()
