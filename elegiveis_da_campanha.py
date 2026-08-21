# -*- coding: utf-8 -*-
"""
QUEM ESTA ELEGIVEL NUMA CAMPANHA — somente leitura, nao altera NADA no ML.

PARA QUE SERVE
  A conta tem 38 campanhas. O painel mostra as campanhas POR ANUNCIO (voce abre
  o anuncio e ve onde ele cabe). Este script faz a pergunta ao contrario: dada
  UMA campanha, quais anuncios o ML esta convidando?

  Nasceu da "Pre Acordo Essencial Ago" [PRE_NEGOTIATED/P-MLB17883094].

DE ONDE VEM O DADO (doc do ML, nao suposicao)
  GET /seller-promotions/promotions/{PID}/items?promotion_type={TIPO}&app_version=v2
  A doc diz, sobre o status do item: "candidate: O item e elegivel e pode
  participar da promocao". Entao ELEGIVEL = status 'candidate'. Nao e leitura
  minha, e a definicao deles.

  Outros status: 'pending' (voce aceitou, ainda nao comecou), 'started' (ativo
  na campanha), 'finished' (removido).

DOIS DETALHES DA DOC QUE MUDAM O RESULTADO
  1. Paginacao e por search_after (nao offset). O limit maximo e 50, o
     search_after vem em TODA pagina menos a ultima, tem TTL de 5 minutos e nao
     da pra voltar. Sem paginar, voce ve 50 e acha que sao todos.
  2. Sem o parametro status_item, "a consulta, por padrao, retorna apenas os
     itens ativos no Mercado Livre". Pausado nao aparece — o que aqui e bom, mas
     e bom saber que e um filtro implicito e nao a lista completa.

MARGEM
  Usa a MESMA conta do resto do sistema (rec.avaliar): comissao cheia sobre o
  preco da promocao menos a reducao que o ML banca, menos frete, menos custo.
  Sem custo cadastrado nao invento nada — o anuncio sai marcado 'sem custo'.

USO (inputs do workflow / env)
  SELLER_ID   conta (obrigatorio se voce tem mais de uma)
  PROMO_ID    ex.: P-MLB17883094   -> vazio LISTA as campanhas e para
  PROMO_TIPO  ex.: PRE_NEGOTIATED  -> vazio: descobre pelo id na lista da conta
  SO_ELEGIVEIS  1 (padrao) = so os 'candidate'; 0 = todos os status
"""
import os
import repricer_sugestoes as rec
from ml_auth import obter_access

sb = rec.sb
SELLER = (os.environ.get("SELLER_ID") or "").strip()
PROMO_ID = (os.environ.get("PROMO_ID") or "").strip()
PROMO_TIPO = (os.environ.get("PROMO_TIPO") or "").strip().upper()
SO_ELEGIVEIS = (os.environ.get("SO_ELEGIVEIS", "1").strip() != "0")
TETO_PAGINAS = int(os.environ.get("TETO_PAGINAS", "200"))


def brl(v):
    try:
        return "R$ " + format(float(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)


def _search_after(d):
    """O ML devolve o cursor ora como 'search_after', ora como 'searchAfter', e as
    vezes dentro de 'paging'. A doc renomeou o parametro e diz que o antigo ainda
    e aceito por um tempo — entao procuro nos quatro lugares em vez de apostar."""
    if not isinstance(d, dict):
        return None
    for k in ("search_after", "searchAfter"):
        v = d.get(k)
        if v:
            return str(v)
    pg = d.get("paging")
    if isinstance(pg, dict):
        for k in ("search_after", "searchAfter"):
            v = pg.get(k)
            if v:
                return str(v)
    return None


def itens_da_campanha(pid, tipo, access, status=None):
    """TODOS os itens da campanha, paginando ate o fim.
    O teto de paginas existe so como freio de emergencia: se ele for atingido, a
    lista esta INCOMPLETA e o script diz isso na cara, em vez de entregar um
    numero menor como se fosse o total."""
    out, cursor, paginas, truncou = [], None, 0, False
    while True:
        p = (f"/seller-promotions/promotions/{pid}/items"
             f"?promotion_type={tipo}&app_version=v2&limit=50")
        if status:
            p += f"&status={status}"
        if cursor:
            p += f"&search_after={cursor}"
        st, d = rec.get(p, access)
        if not isinstance(d, dict):
            print(f"  ! o ML respondeu {st} em vez de uma lista — parei aqui", flush=True)
            truncou = True
            break
        res = d.get("results") or []
        out.extend([x for x in res if isinstance(x, dict)])
        cursor = _search_after(d)
        paginas += 1
        # sem cursor = ultima pagina (a doc: "o search_after sera retornado em
        # todas as paginas, exceto na ultima")
        if not cursor or not res:
            break
        if paginas >= TETO_PAGINAS:
            truncou = True
            break
    return out, truncou


def main():
    contas = rec.contas()
    for seller_id, refresh in contas:
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        if SELLER and str(sid) != SELLER:
            continue
        print(f"\n===== CONTA {sid} =====", flush=True)
        campanhas = rec.promocoes_do_vendedor(sid, access)

        # SEM PROMO_ID: so mostra o cardapio e para. Assim voce escolhe olhando
        # id, tipo e status, em vez de adivinhar o nome.
        if not PROMO_ID:
            print(f"campanhas da conta: {len(campanhas)}", flush=True)
            for c in sorted(campanhas, key=lambda x: ((x.get("type") or ""), (x.get("name") or ""))):
                print(f"  {c.get('id'):<18} {(c.get('type') or ''):<22} "
                      f"{(c.get('status') or ''):<9} {c.get('name') or '(sem nome)'}", flush=True)
            print("\nRode de novo com PROMO_ID=<o id acima> pra ver os anúncios elegíveis.",
                  flush=True)
            continue

        alvo = next((c for c in campanhas if str(c.get("id")) == PROMO_ID), None)
        tipo = PROMO_TIPO or ((alvo or {}).get("type") or "").upper()
        if not tipo:
            print(f"não achei {PROMO_ID} nas campanhas desta conta e você não passou "
                  f"PROMO_TIPO — sem o tipo o ML recusa a consulta.", flush=True)
            continue
        nome = (alvo or {}).get("name") or "(nome não encontrado na lista da conta)"
        print(f"campanha: {nome} [{tipo}/{(alvo or {}).get('status')}/{PROMO_ID}]", flush=True)

        itens, truncou = itens_da_campanha(PROMO_ID, tipo,
                                           access, "candidate" if SO_ELEGIVEIS else None)
        if truncou:
            print("  ⚠️ LISTA INCOMPLETA — bati o teto de páginas ou o ML cortou. "
                  "O número abaixo é MENOR que o real.", flush=True)
        porstatus = {}
        for x in itens:
            k = (x.get("status") or "?").lower()
            porstatus[k] = porstatus.get(k, 0) + 1
        print(f"itens devolvidos: {len(itens)}  ({porstatus})", flush=True)
        print("  (sem status_item, o ML devolve só os anúncios ATIVOS — pausado não entra)",
              flush=True)
        if not itens:
            continue

        rec.preload(sid)
        ids = [str(x.get("id")) for x in itens if x.get("id")]
        detalhes = rec.detalhes_itens(ids, access)

        linhas, sem_custo = [], []
        for x in itens:
            iid = str(x.get("id") or "")
            it = detalhes.get(iid) or {}
            sku = rec.sku_do_item(it) if it else None
            custo = rec.custo_efetivo(iid, sku)
            if custo is None:
                sem_custo.append((iid, sku, it.get("title")))
                continue
            frete, _o = rec.frete_de(sku, iid, access)
            piso, grupo = rec.margem_minima_do(sku)
            ev = rec.avaliar(x, it.get("category_id"), it.get("listing_type_id"),
                             access, frete, custo)
            if not ev:
                # sem preco ou sem preco original: nao da pra calcular margem.
                # Aparece assim mesmo — campanha que existe e some da tela e pior.
                linhas.append((None, iid, sku, it.get("title"), x, None, piso, grupo))
                continue
            linhas.append((ev["margem"], iid, sku, it.get("title"), x, ev, piso, grupo))

        # maior margem primeiro: e a ordem em que voce decidiria
        linhas.sort(key=lambda t: (t[0] is None, -(t[0] or 0)))
        print("\n--- ELEGÍVEIS (margem calculada no preço da campanha) ---", flush=True)
        acima = 0
        for m, iid, sku, tit, x, ev, piso, grupo in linhas:
            if ev is None:
                print(f"  {iid} {str(sku or '—'):<16} sem preço na oferta — não dá pra calcular "
                      f"margem | {str(tit)[:40]}", flush=True)
                continue
            ok = "✅" if m >= piso else "⚠️"
            if m >= piso:
                acima += 1
            print(f"  {ok} {iid} {str(sku or '—'):<16} {brl(x.get('original_price'))} → "
                  f"{brl(ev['pb'])} | margem {m:6.2f}% (piso {piso:.0f}% · {grupo}) | "
                  f"ML banca {ev['mp']:.1f}% | {str(tit)[:34]}", flush=True)
        print(f"\nresumo: {len(linhas)} elegíveis com custo · {acima} acima do piso · "
              f"{len(linhas) - acima} abaixo · {len(sem_custo)} sem custo cadastrado", flush=True)
        for iid, sku, tit in sem_custo[:20]:
            print(f"  sem custo: {iid} {sku or '—'} {str(tit)[:40]}", flush=True)
    print("\n################ FIM — nada foi alterado (só leitura) ################", flush=True)


if __name__ == "__main__":
    main()
