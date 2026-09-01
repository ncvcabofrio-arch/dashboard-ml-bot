# -*- coding: utf-8 -*-
"""
DE ONDE SAEM OS 5,00 PONTOS — somente leitura. Nao escreve no ML nem no banco.

O QUE ELE RESPONDE
  Na rodada de 122 quatro anuncios foram barrados pela trava da margem com a
  diferenca EXATA de 5,00 pontos (16->11, 17->12, 18->13, 16->11), e na de 103
  um quinto (10->5). Cinco casos, sempre 5,00. Isso e regra, nao arredondamento.

  Este script diz QUAL TERMO da conta esta diferente, item a item, decompondo a
  margem nas suas partes e mostrando as duas contas lado a lado.

A CONTA, ESCRITA POR INTEIRO
  O robo calcula assim (repricer_sugestoes.avaliar):

      tarifa  = comissao_cheia(pb) - (meli% / 100 * p0)
      recebe  = pb - tarifa - frete
      margem  = (recebe - custo) / pb

  Reagrupando em (parte que nao depende do preco) menos (parte que depende):

      margem = A - B/pb      com   A = 1 - comissao%/100 + rebate_efetivo
                                   B = frete + custo
                             e     rebate_efetivo = (meli%/100 * p0) / pb

  O painel NAO tem a comissao: ele deduz A e B de DOIS PONTOS da faixa que o ML
  mandou junto com a sugestao (preco_min/margem_min e preco_max/margem_max), o
  que determina A e B exatamente. Depois inverte:  p = B / (A - alvo).

  Entao a divergencia so pode estar em A ou em B, e o script mede as duas.

O QUE EU JA SEI E O QUE EU AINDA NAO SEI
  SEI: um desvio CONSTANTE em pontos, identico em precos muito diferentes
  (R$891 a R$2.628), nao pode vir de B — a contribuicao de B e B/pb, que muda
  com o preco. Tem que estar em A.

  NAO SEI qual das duas parcelas de A. Eu disse na conversa que A e "1 menos a
  comissao"; isso estava incompleto. A tem DUAS parcelas, e as duas produzem
  desvio constante:

    1. COMISSAO  — se as duas contas usarem listing_type_id (ou categoria)
       diferentes, a % vem de outra tabela do ML. Classico e premium costumam
       ficar 5 pontos separados, o que casaria com o numero.

    2. REBATE DO ML (meli_percentage) — quando pb ~ p0, o rebate entra na
       margem quase ponto a ponto. Uma cofinanciada com 5% de rebate vira
       exatamente 5,00 pontos que EXISTEM na oferta que o painel usou para
       desenhar a curva e NAO existem no desconto individual, que e bancado
       100% pelo vendedor. Esta e a hipotese que eu acho mais provavel, porque
       5 e um numero que aparece escrito no dado do ML, e nao uma coincidencia
       entre tabelas de categoria.

  O script separa as duas. Nao decide no meu chute: decide no numero.

USO
  SELLER_ID=177795203                 (obrigatorio: e a conta das rodadas)
  MLBS='MLB1,MLB2'                    (vazio = pega sozinho os barrados da fila)
  LIMITE=40                           (teto de itens, so pra nao virar rodada longa)
"""
import json
import os
import re

import repricer_sugestoes as rec
from ml_auth import obter_access

SELLER = (os.environ.get("SELLER_ID") or "177795203").strip()
LIMITE = int(os.environ.get("LIMITE") or "40")
_env = (os.environ.get("MLBS") or "").strip()
ALVOS_ENV = [x.strip().upper() for x in _env.split(",") if x.strip()]


def brl(v):
    try:
        return "R$" + format(float(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)


def pp(v, casas=2):
    try:
        return f"{float(v):.{casas}f}"
    except (TypeError, ValueError):
        return "?"


def num(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- fila (o pedido da tela)
def fila_do_item(iid):
    """A linha MAIS NOVA da fila para este item. select('*') de proposito: eu ja
    inventei nome de coluna nesta sessao mais de uma vez, entao aqui eu leio tudo
    e pergunto ao dado quais colunas existem, em vez de assumir."""
    try:
        rows = (rec.sb.table("repricer_promo_fila").select("*")
                .eq("seller_id", str(SELLER)).eq("item_id", iid)
                .order("id", desc=True).limit(1).execute().data) or []
        return rows[0] if rows else {}
    except Exception as e:
        print(f"  aviso: nao li a fila de {iid}: {e}", flush=True)
        return {}


# A trava da margem NAO grava o codigo 'margem_menor_que_pedida' na fila — esse codigo
# e o valor de RETORNO da funcao, que so alimenta o resumo do log. Na tabela ela grava
# status='erro' e um texto explicativo. Foi assim que a primeira versao deste script
# achou zero item: eu filtrei pelo codigo do log em vez do texto do banco.
#
# O texto e (repricer_promo_aplicar.py, trava da margem):
#   "NÃO APLIQUEI. Você pediu 16.00% e o preço que o ML aceita (R$2628.63) entrega
#    11.00% — margem MENOR que a sua. ..."
#
# Bonus: os DOIS numeros estao escritos ali. Entao a fila me da a margem pedida mesmo
# que a coluna margem_alvo_manual esteja vazia — e me da tambem a margem que o robo
# mediu, que e o alvo que este script tem que reproduzir.
ASSINATURA_TRAVA = "margem menor que a sua"
_RE_TRAVA = re.compile(r"pediu\s+([\d.,]+)%.*?\(R\$\s*([\d.,]+)\)\s*entrega\s+([\d.,]+)%",
                       re.IGNORECASE | re.DOTALL)


def _n(txt):
    """Numero no formato do texto do robo (usa ponto decimal) -> float."""
    try:
        return float(str(txt).replace(".", "").replace(",", ".")) if "," in str(txt) \
            else float(txt)
    except (TypeError, ValueError):
        return None


def le_fila(limite_linhas=1500):
    try:
        return (rec.sb.table("repricer_promo_fila").select("*")
                .eq("seller_id", str(SELLER))
                .order("id", desc=True).limit(limite_linhas).execute().data) or []
    except Exception as e:
        print(f"nao consegui ler a fila: {e}")
        return []


def barrados_da_fila():
    """Pega sozinho quem a trava da margem barrou, pelo TEXTO que ela grava.
    Se nao achar nada, mostra o que a fila realmente tem — em vez de dizer
    'nada para conferir' e deixar voce sem saber se e ausencia ou filtro errado."""
    rows = le_fila()
    if not rows:
        print("a fila desta conta esta vazia.")
        return [], {}
    if "resultado" not in rows[0]:
        print(f"a fila nao tem coluna 'resultado'. Colunas: {sorted(rows[0].keys())}")
        return [], {}
    vistos, out, pedidos = set(), [], {}
    for r in rows:
        txt = str(r.get("resultado") or "")
        if ASSINATURA_TRAVA not in txt.lower():
            continue
        iid = r.get("item_id")
        if not iid or iid in vistos:
            continue
        vistos.add(iid)
        out.append(iid)
        m = _RE_TRAVA.search(txt)
        if m:
            pedidos[iid] = {"pedida": _n(m.group(1)), "preco": _n(m.group(2)),
                            "real": _n(m.group(3))}
    if not out:
        print("nenhum item barrado pela trava da margem nesta fila. O que ela tem:")
        cont = {}
        for r in rows:
            chave = str(r.get("resultado") or "(vazio)")[:60]
            cont[chave] = cont.get(chave, 0) + 1
        for chave, n in sorted(cont.items(), key=lambda x: -x[1])[:12]:
            print(f"   {n:5d}x  {chave}")
    return out[:LIMITE], pedidos


# ---------------------------------------------------------------- sugestao (a curva do painel)
def ofertas_guardadas(iid):
    try:
        rows = (rec.sb.table("repricer_sugestoes").select("*")
                .eq("seller_id", str(SELLER)).eq("item_id", iid)
                .order("criado_em", desc=True).limit(1).execute().data) or []
    except Exception as e:
        print(f"  aviso: nao li a sugestao de {iid}: {e}", flush=True)
        return {}, []
    if not rows:
        return {}, []
    row = rows[0]
    ofs = row.get("ofertas")
    if isinstance(ofs, str):
        try:
            ofs = json.loads(ofs)
        except ValueError:
            ofs = []
    return row, (ofs if isinstance(ofs, list) else [])


def curva_do_painel(ofs):
    """Refaz, em Python, EXATAMENTE o caminho 2 do acelEstPreco do painel:
    dois pontos da faixa do desconto individual determinam A e B.
    Devolve (A, B, oferta_usada) ou (None, None, oferta_ou_None)."""
    fx = None
    for o in (ofs or []):
        if isinstance(o, dict) and o.get("individual"):
            fx = o
            break
    if not fx:
        return None, None, None
    pts = []
    for pk, mk in (("preco_min", "margem_min"), ("preco_sug", "margem_sug"),
                   ("preco_max", "margem_max")):
        p, m = num(fx.get(pk)), num(fx.get(mk))
        if p is not None and m is not None and p > 0:
            pts.append((p, m / 100.0))
    if len(pts) < 2:
        return None, None, fx
    p1, m1 = pts[0]
    p2, m2 = pts[-1]
    if abs(p1 - p2) < 0.01:
        return None, None, fx
    B = (m1 - m2) / ((1 / p2) - (1 / p1))
    A = m1 + B / p1
    return A, B, fx


# ---------------------------------------------------------------- a conta do robo, aberta
def conta_do_robo(pb, p0, mp, cat, ltid, frete, custo, access):
    """Reproduz avaliar() mostrando cada parcela, em vez de so o total."""
    com = rec.comissao(round(pb, 2), cat, ltid, access) or 0.0
    reducao = float(mp or 0) / 100.0 * float(p0 or 0)
    tarifa = max(com - reducao, 0.0)
    recebe = pb - tarifa - frete
    margem = ((recebe - custo) / pb * 100) if pb else None
    return {
        "comissao": com, "com_pct": (com / pb * 100 if pb else None),
        "mp": float(mp or 0), "reducao": reducao,
        "rebate_pp": (reducao / pb * 100 if pb else None),
        "tarifa": tarifa, "frete": frete, "custo": custo,
        "A": (1 - com / pb + reducao / pb) if pb else None,
        "B": frete + custo, "margem": margem,
    }


def token_da_conta():
    """Resolve o access token da conta escolhida — MESMA mecânica do
    repricer_sugestoes.main() e do aplicador:  obter_access(sb, seller_id, refresh)
    devolve (access, sid_real, refresh).  O seller_id que vem da tabela 'contas'
    pode ser None (conta semeada por ML_REFRESH_TOKEN), por isso a conferência é
    feita no sid que VOLTA da chamada, não no que foi enviado."""
    for seller_id, refresh in rec.contas():
        try:
            access, sid, refresh = obter_access(rec.sb, seller_id, refresh)
        except Exception as e:
            print(f"  !! não consegui token de {seller_id}: {e}", flush=True)
            continue
        if str(sid) == SELLER:
            return access, str(sid)
    return None, None


def main():
    access, sid = token_da_conta()
    if not access:
        print(f"sem token para a conta {SELLER} — as contas com token são: "
              f"{[c[0] for c in rec.contas()]}")
        return
    rec.preload(SELLER)

    pedidos = {}
    if ALVOS_ENV:
        alvos = ALVOS_ENV
    else:
        alvos, pedidos = barrados_da_fila()
    if not alvos:
        # o motivo ja foi impresso por barrados_da_fila (fila vazia, coluna faltando,
        # ou a lista do que a fila realmente tem). Aqui so digo o caminho manual.
        print("\nnada para conferir automaticamente. Rode com MLBS='MLB...,MLB...' "
              "para conferir itens escolhidos por voce.")
        return
    print(f"conta {SELLER} | {len(alvos)} item(ns)\n")

    det = rec.detalhes_itens(alvos, access)
    resumo = []

    for iid in alvos:
        it = det.get(iid) or {}
        cat = it.get("category_id")
        ltid = it.get("listing_type_id")
        p_lista = num(it.get("price"))
        sku = rec.sku_do_item(it) if it else None
        custo, orig_custo = rec.custo_efetivo(iid, sku)
        frete = rec.frete_de(sku, iid, access) or 0.0

        print("=" * 78)
        print(f"{iid}  {str(it.get('title') or '')[:44]}")
        print(f"  categoria {cat} | tipo de anuncio {ltid} | preco de lista {brl(p_lista)}")
        print(f"  sku {sku} | custo {brl(custo)} ({orig_custo}) | frete {brl(frete)}")

        # ---- % da comissao: com o tipo do anuncio, e sem ele
        pct_com = rec._percentual(cat, ltid, access)
        pct_sem = rec._percentual(cat, None, access)
        print(f"  comissao do ML: com listing_type_id={ltid} -> {pp(pct_com)}% | "
              f"SEM listing_type_id -> {pp(pct_sem)}%")
        if pct_com is not None and pct_sem is not None:
            d = abs(float(pct_com) - float(pct_sem))
            if d >= 0.5:
                print(f"     ^ as duas respostas diferem em {pp(d)} pontos — se algum caminho "
                      f"esquecer o tipo do anuncio, e por aqui que entra")

        # ---- o pedido da tela
        fila = fila_do_item(iid)
        mg_ped = num(fila.get("margem_alvo_manual"))
        de_onde = "margem_alvo_manual (voce digitou)"
        if mg_ped is None:
            mg_ped = num(fila.get("margem_prevista"))
            de_onde = "margem_prevista (a tela calculou)"
        if mg_ped is None and iid in pedidos:
            mg_ped = pedidos[iid].get("pedida")
            de_onde = "texto que a trava gravou na fila"
        if mg_ped is None:
            print(f"  a fila nao trouxe margem pedida. Colunas: {sorted(fila.keys())}")
        alvo_medido = (pedidos.get(iid) or {}).get("real")
        if alvo_medido is not None:
            print(f"  na rodada o robo mediu {pp(alvo_medido)}% "
                  f"a {brl((pedidos.get(iid) or {}).get('preco'))} — e esse numero que eu "
                  f"tenho que reproduzir aqui")

        # ---- a curva que o painel usou
        row_sug, ofs = ofertas_guardadas(iid)
        A, B, fx = curva_do_painel(ofs)
        if fx is None:
            print("  faixa do desconto individual: NAO existe na sugestao guardada "
                  "(o painel caiu no 'ponto unico' — curva deduzida da oferta ativa)")
        elif A is None:
            print("  faixa do desconto individual existe, mas sem dois pontos utilizaveis")
        else:
            print(f"  curva do PAINEL (2 pontos da faixa): A = {pp(A, 4)}  "
                  f"(= comissao implicita {pp((1 - A) * 100)}%)  |  B = {brl(B)}")
            print(f"     rebate guardado na faixa (meli%): {fx.get('rebate')}")

        # ---- as promocoes que o item tinha quando a sugestao rodou
        for o in (ofs or []):
            if not isinstance(o, dict):
                continue
            mp_o = o.get("meli_percentage", o.get("rebate"))
            print(f"     · {str(o.get('nome') or o.get('tipo'))[:38]:38s} "
                  f"[{o.get('tipo')}] meli%={mp_o} vendedor%={o.get('seller_percentage')} "
                  f"{'ATIVA' if o.get('ativa') else ''}")

        # ---- reproduzir a divergencia
        if A is not None and B is not None and mg_ped is not None:
            den = A - float(mg_ped) / 100.0
            preco = (B / den) if den > 0 else None
            if preco:
                mp_ind = num(fx.get("rebate")) or 0.0
                c = conta_do_robo(preco, p_lista or preco, mp_ind, cat, ltid,
                                  frete, custo, access)
                gap = float(mg_ped) - float(c["margem"])
                contrib_A = (A - c["A"]) * 100
                contrib_B = (c["B"] - B) / preco * 100
                print(f"  PEDIDO: {pp(mg_ped)}% ({de_onde})")
                print(f"  preco que a tela calcula para essa margem: {brl(preco)}")
                print(f"  margem que o robo mede nesse preco: {pp(c['margem'])}%  "
                      f"-> DIFERENCA {pp(gap)} pontos")
                print(f"     de onde vem a diferenca:")
                print(f"       A (nao depende do preco) .... {pp(contrib_A)} pontos   "
                      f"[painel {pp(A, 4)} vs robo {pp(c['A'], 4)}]")
                print(f"       B (custo+frete) ............. {pp(contrib_B)} pontos   "
                      f"[painel {brl(B)} vs robo {brl(c['B'])}]")
                print(f"     dentro de A:")
                print(f"       comissao ....... {pp(c['com_pct'])} pontos do preco "
                      f"({brl(c['comissao'])})")
                print(f"       rebate do ML ... {pp(c['rebate_pp'])} pontos "
                      f"(meli% = {pp(c['mp'])})")
                resumo.append((iid, gap, contrib_A, contrib_B, c["rebate_pp"], c["com_pct"]))
        print()

    # ---------------------------------------------------------------- veredito
    if resumo:
        print("=" * 78)
        print("RESUMO — onde mora a diferenca")
        print(f"{'anuncio':>16} {'gap':>8} {'por A':>8} {'por B':>8} {'rebate':>8} {'comis%':>8}")
        for iid, gap, ca, cb, reb, com in resumo:
            print(f"{iid:>16} {pp(gap):>8} {pp(ca):>8} {pp(cb):>8} {pp(reb):>8} {pp(com):>8}")
        print()
        porA = sum(1 for r in resumo if abs(r[2]) > abs(r[3]))
        print(f"  {porA} de {len(resumo)} tem a diferenca concentrada em A "
              f"(parte que NAO depende do preco).")
        reb = [r[4] for r in resumo if r[4] is not None]
        if reb and max(reb) - min(reb) < 0.01 and abs(reb[0]) > 0.01:
            print(f"  o rebate do ML e o MESMO em todos ({pp(reb[0])} pontos) — "
                  f"se ele bater com o gap, a causa e o rebate.")
        print("\n  LEITURA: gap ~= 'por A' e 'por B' ~ 0 confirma que a divergencia nao vem")
        print("  de custo/frete. Dentro de A, compare o gap com a coluna 'rebate': se forem")
        print("  o mesmo numero, o painel desenhou a curva com um rebate que o desconto")
        print("  individual nao tem. Se o gap bater com a diferenca de comissao com/sem")
        print("  listing_type_id impressa acima, a causa e o tipo do anuncio.")


if __name__ == "__main__":
    main()
