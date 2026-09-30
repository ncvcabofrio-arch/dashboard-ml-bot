# -*- coding: utf-8 -*-
"""
TESTE CONTROLADO — TROCAR O PREÇO DE UM DESCONTO INDIVIDUAL SEM REMOVER O ATUAL.

POR QUE ESTE TESTE EXISTE
-------------------------
O aplicador, quando acha um desconto individual em vigor, recusa a troca. A mensagem
dele diz por quê:

    "o ML não abre candidatura para um segundo — medido em 17 de 17 casos"

E, no mesmo arquivo, sobre o caminho alternativo (remover antes de criar):

    "A causa do 'No candidates found' segue DESCONHECIDA — remover o individual em
     vigor NÃO abre a candidatura (medição de 19/ago)"

Duas medições de agosto que se contradizem, e em NENHUMA delas o corpo da resposta do
ML foi guardado. Sem o erro, as duas viraram lenda: uma virou trava, a outra virou
desligamento (TROCAR_INDIVIDUAL=0). Este script existe para ler o erro.

AS DUAS HIPÓTESES
-----------------
A) PUT  /seller-promotions/items/{id}   {promotion_id, promotion_type, deal_price}
   É o verbo de MODIFICAR. Já está em produção neste repositório para SELLER_CAMPAIGN,
   com este comentário: "É o caminho seguro: não sai de nada, não abre janela sem
   desconto, não gasta candidatura." Ninguém tentou com PRICE_DISCOUNT.

B) POST /seller-promotions/items/{id}   {deal_price, start_date, finish_date, ...}
   É o verbo de OFERECER, o único documentado para o desconto individual. A doc NÃO
   exige remover antes, e a lista de erros dela não tem nenhum "já existe desconto
   neste item" — o que sugere que o POST simplesmente substitui.

Tenta A; se o ML recusar, tenta B. Imprime o corpo INTEIRO das duas respostas.

O QUE ELE NÃO FAZ
-----------------
Não remove nada antes. Não mexe em campanha nenhuma. Não grava no Supabase. Não sobe
preço. Sem CONFIRMAR=SIM ele só lê e para.

DESFAZER
--------
Rodar de novo com DESFAZER=SIM remove o desconto individual do anúncio (DELETE) e
mostra o preço antes e depois. É a saída de emergência pela mesma tela.

Inputs (env): ITEM_ID, SELLER_ID, PRECO_NOVO, CONFIRMAR, DESFAZER, MODO (AUTO|PUT|POST)
"""
import os
import json
import time
from datetime import datetime, timedelta

import repricer_sugestoes as rec
import repricer_promo_aplicar as apl
from ml_auth import obter_access

sb = rec.sb

ITEM = (os.environ.get("ITEM_ID") or "").strip().upper()
SELLER = (os.environ.get("SELLER_ID") or "").strip()
CONFIRMAR = (os.environ.get("CONFIRMAR") or "").strip().upper() == "SIM"
DESFAZER = (os.environ.get("DESFAZER") or "").strip().upper() == "SIM"
MODO = (os.environ.get("MODO") or "AUTO").strip().upper()
PERMITIR_TOP = (os.environ.get("PERMITIR_TOP") or "").strip().upper() == "SIM"

try:
    PRECO_NOVO = float((os.environ.get("PRECO_NOVO") or "0").replace(",", ".").strip())
except ValueError:
    PRECO_NOVO = 0.0

DIAS = 12          # duração do desconto quando não dá pra reaproveitar a data atual
TOL = 0.02         # centavos de tolerância ao comparar preços


def brl(v):
    try:
        return "R$ " + format(float(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)


def dump(titulo, obj):
    print(f"\n--- {titulo} " + "-" * max(4, 66 - len(titulo)), flush=True)
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:6000], flush=True)
    except (TypeError, ValueError):
        print(repr(obj)[:6000], flush=True)


def sale_price(access):
    """O preço que o COMPRADOR paga, medido. É o árbitro deste teste: não interessa o
    que o ML respondeu no POST/PUT, interessa se o preço da vitrine mudou."""
    st, d = rec.get(f"/items/{ITEM}/sale_price?context=channel_marketplace", access)
    amt = d.get("amount") if isinstance(d, dict) else None
    try:
        return (round(float(amt), 2) if amt is not None else None), st, d
    except (TypeError, ValueError):
        return None, st, d


def promocoes(access):
    st, d = rec.get(f"/seller-promotions/items/{ITEM}?app_version=v2", access)
    return (d if isinstance(d, list) else []), st, d


def individual_ativo(ofertas):
    for o in ofertas:
        if not isinstance(o, dict):
            continue
        if (o.get("type") or "").upper() != "PRICE_DISCOUNT":
            continue
        if rec.eh_ativa(o):
            return o
    return None


def esperar_preco(access, esperado, tentativas=8, espera=3.0):
    """O ML é assíncrono: o 200 diz 'pedido aceito', não 'preço trocado'. Espera o
    sale_price virar o preço pedido, e devolve o que encontrou."""
    visto = None
    for i in range(tentativas):
        time.sleep(espera)
        visto, _, _ = sale_price(access)
        print(f"     tentativa {i+1}/{tentativas}: sale_price = {brl(visto)}", flush=True)
        if visto is not None and abs(visto - esperado) <= TOL:
            return True, visto, i + 1
    return False, visto, tentativas


def irmaos(access, it):
    """Anúncio irmão sincronizado muda de preço junto e estraga a leitura do teste."""
    achados = {}
    sid = it.get("seller_id")
    upid = it.get("user_product_id")
    alvos = []
    if upid:
        alvos.append((f"/users/{sid}/items/search?user_product_id={upid}&limit=100", "user_product_id"))
    sku = it.get("seller_custom_field")
    if sku:
        alvos.append((f"/users/{sid}/items/search?seller_sku={sku}&limit=100", "seller_sku"))
    for path, via in alvos:
        st, d = rec.get(path, access)
        for x in ((d or {}).get("results") or []) if isinstance(d, dict) else []:
            if str(x) != ITEM:
                achados.setdefault(str(x), via)
    return achados


def main():
    if not ITEM:
        print("!! defina ITEM_ID", flush=True)
        return

    access, SID = None, None
    for seller_id, refresh in rec.contas():
        try:
            a, sid, refresh = obter_access(sb, seller_id, refresh)
        except Exception as e:
            print(f"  token de {seller_id} falhou: {e}", flush=True)
            continue
        if not SELLER or str(sid) == SELLER:
            access, SID = a, str(sid)
            if SELLER:
                break
    if not access:
        print("!! não consegui token", flush=True)
        return

    print(f"################ TESTE TROCA DE INDIVIDUAL — {ITEM} — conta {SID} ################",
          flush=True)

    # ---------------------------------------------------------------- 1) estado de hoje
    st_it, it = rec.get(f"/items/{ITEM}", access)
    if not isinstance(it, dict):
        print(f"!! não li o anúncio (HTTP {st_it})", flush=True)
        return
    lista = it.get("price")
    print(f"\n[1] ANÚNCIO   {it.get('title')}", flush=True)
    print(f"    preço de lista .... {brl(lista)}", flush=True)
    print(f"    status ............ {it.get('status')} | condição {it.get('condition')}", flush=True)
    print(f"    tipo .............. {it.get('listing_type_id')}", flush=True)
    print(f"    estoque ........... {it.get('available_quantity')}", flush=True)

    ofertas, st_of, bruto_of = promocoes(access)
    dump("[2] TODAS AS PROMOÇÕES DO ANÚNCIO (bruto do ML)", bruto_of)

    p_antes, st_sp, bruto_sp = sale_price(access)
    print(f"\n[3] SALE_PRICE AGORA = {brl(p_antes)}   (HTTP {st_sp})", flush=True)

    ind = individual_ativo(ofertas)
    if ind is None:
        print("\n!! este anúncio NÃO tem desconto individual ativo. O teste é justamente "
              "trocar por cima de um ativo — escolha outro anúncio.", flush=True)
        return
    p_ind = rec.preco_oferta(ind)
    print(f"\n[4] DESCONTO INDIVIDUAL EM VIGOR = {brl(p_ind)}", flush=True)
    print(f"    status {ind.get('status')!r} | id {ind.get('id')!r} | "
          f"ref_id {ind.get('ref_id')!r}", flush=True)
    print(f"    início {ind.get('start_date')} | fim {ind.get('finish_date')}", flush=True)

    # irmão sincronizado: avisa, não impede — mas muda a leitura do resultado
    irm = irmaos(access, it)
    if irm:
        print(f"\n[5] ⚠ IRMÃOS deste produto: {irm}", flush=True)
        print("    Se o preço deles mudar junto, a sincronia do ML entrou no meio do teste.",
              flush=True)
    else:
        print("\n[5] nenhum anúncio irmão encontrado — leitura limpa", flush=True)

    # ------------------------------------------------------------------- DESFAZER
    if DESFAZER:
        print("\n################ DESFAZER — removendo o desconto individual ################",
              flush=True)
        sc, body = apl.req_delete(
            f"/seller-promotions/items/{ITEM}?promotion_type=PRICE_DISCOUNT&app_version=v2", access)
        print(f"    DELETE -> HTTP {sc}", flush=True)
        dump("resposta do DELETE", body)
        time.sleep(5)
        p_dep, _, _ = sale_price(access)
        print(f"\n    sale_price: {brl(p_antes)} -> {brl(p_dep)}", flush=True)
        return

    # ------------------------------------------------------------------ pré-voo
    print("\n################ PRÉ-VOO ################", flush=True)
    paradas = []
    if PRECO_NOVO <= 0:
        paradas.append("PRECO_NOVO não foi informado")
    if p_antes is not None and PRECO_NOVO >= p_antes:
        paradas.append(f"o preço pedido ({brl(PRECO_NOVO)}) NÃO é menor que o de hoje "
                       f"({brl(p_antes)}). Este teste só abaixa preço.")
    try:
        pct = (1 - PRECO_NOVO / float(lista)) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        pct = None
    if pct is None:
        paradas.append("não consegui calcular o % de desconto sobre o preço de lista")
    elif not (5.0 <= pct <= 80.0):
        paradas.append(f"desconto de {pct:.1f}% está fora da faixa da doc (5% a 80%) — "
                       f"o ML recusaria de qualquer jeito")
    _top = {k: v for k, v in ind.items() if "top" in k.lower() and v not in (None, "", 0)}
    if _top and not PERMITIR_TOP:
        paradas.append(f"o desconto atual tem preço para compradores fiéis ({_top}). "
                       f"Trocar sem informar esse campo pode APAGAR essa faixa. "
                       f"Rode com PERMITIR_TOP=SIM se aceitar perder.")
    if pct is not None:
        print(f"    preço pedido ...... {brl(PRECO_NOVO)}  ({pct:.1f}% de desconto sobre "
              f"{brl(lista)})", flush=True)
    print(f"    hoje na vitrine ... {brl(p_antes)}", flush=True)
    if paradas:
        print("\n!! NÃO VOU ESCREVER NADA. Motivo(s):", flush=True)
        for m in paradas:
            print(f"   - {m}", flush=True)
        return
    if not CONFIRMAR:
        print("\n== SÓ LEITURA. Nada foi alterado. Para executar de verdade, rode com "
              "CONFIRMAR=SIM. ==", flush=True)
        return

    # datas: a doc só considera o DIA. Reaproveita o fim atual quando cabe nos 14 dias.
    hoje = datetime.now()
    fim = None
    try:
        _f = str(ind.get("finish_date") or "")[:10]
        if _f:
            _fdt = datetime.strptime(_f, "%Y-%m-%d")
            if hoje.date() < _fdt.date() <= (hoje + timedelta(days=14)).date():
                fim = _fdt
    except ValueError:
        fim = None
    if fim is None:
        fim = hoje + timedelta(days=DIAS)
    corpo_datas = {"start_date": hoje.strftime("%Y-%m-%dT00:00:00"),
                   "finish_date": fim.strftime("%Y-%m-%dT00:00:00")}

    resultado = {"put": None, "post": None}

    # ------------------------------------------------------- HIPÓTESE A: PUT (modificar)
    if MODO in ("AUTO", "PUT"):
        # O desconto individual vem SEM id (medido: "id": None, "ref_id": None). Mandar
        # "promotion_id": null faria o ML reclamar do CAMPO, e a gente aprenderia nada sobre
        # o mecanismo. O DELETE do individual funciona só com promotion_type, sem id — o PUT
        # segue a mesma forma: sem id, o campo não vai.
        corpo_put = {"promotion_type": "PRICE_DISCOUNT",
                     "deal_price": round(PRECO_NOVO, 2)}
        if ind.get("id"):
            corpo_put["promotion_id"] = ind.get("id")
        print("\n################ HIPÓTESE A — PUT (modificar, sem remover) ################",
              flush=True)
        dump("corpo enviado", corpo_put)
        sc, resp = apl.req_put(f"/seller-promotions/items/{ITEM}?app_version=v2", access, corpo_put)
        print(f"    PUT -> HTTP {sc}", flush=True)
        dump("RESPOSTA DO ML (inteira)", resp)
        resultado["put"] = {"http": sc, "resposta": resp}
        if sc in (200, 201):
            print("\n    aceito. Conferindo se o preço da vitrine mudou de verdade:", flush=True)
            ok, visto, voltas = esperar_preco(access, round(PRECO_NOVO, 2))
            resultado["put"]["preco_depois"] = visto
            resultado["put"]["valeu"] = ok
            if ok:
                print(f"\n>>> PUT FUNCIONOU: {brl(p_antes)} -> {brl(visto)} "
                      f"em {voltas} conferência(s), sem remover nada.", flush=True)
                _fim(access, p_antes, resultado, irm)
                return
            print(f"\n    ⚠ o ML aceitou o PUT mas a vitrine ficou em {brl(visto)}. "
                  f"Aceitar não é valer.", flush=True)
        else:
            print("    recusado — é este o erro que faltava ler.", flush=True)

    # ----------------------------------------------- HIPÓTESE B: POST por cima do ativo
    if MODO in ("AUTO", "POST"):
        corpo_post = {"deal_price": round(PRECO_NOVO, 2),
                      "promotion_type": "PRICE_DISCOUNT"}
        corpo_post.update(corpo_datas)
        print("\n################ HIPÓTESE B — POST por cima do ativo ################",
              flush=True)
        dump("corpo enviado", corpo_post)
        sc, resp = apl.post(f"/seller-promotions/items/{ITEM}?app_version=v2", access, corpo_post)
        print(f"    POST -> HTTP {sc}", flush=True)
        dump("RESPOSTA DO ML (inteira)", resp)
        resultado["post"] = {"http": sc, "resposta": resp}
        if sc in (200, 201):
            print("\n    aceito. Conferindo se o preço da vitrine mudou de verdade:", flush=True)
            ok, visto, voltas = esperar_preco(access, round(PRECO_NOVO, 2))
            resultado["post"]["preco_depois"] = visto
            resultado["post"]["valeu"] = ok
            if ok:
                print(f"\n>>> POST FUNCIONOU: {brl(p_antes)} -> {brl(visto)} "
                      f"em {voltas} conferência(s), sem remover nada.", flush=True)
            else:
                print(f"\n    ⚠ o ML aceitou o POST mas a vitrine ficou em {brl(visto)}. "
                      f"Isto seria dormência de verdade — o primeiro caso medido.", flush=True)
        else:
            print("    recusado — é este o erro que faltava ler.", flush=True)

    _fim(access, p_antes, resultado, irm)


def _fim(access, p_antes, resultado, irm=None):
    print("\n################ ESTADO FINAL ################", flush=True)
    ofertas, _, bruto = promocoes(access)
    dump("promoções do anúncio DEPOIS", bruto)
    p_dep, _, _ = sale_price(access)
    print(f"\n    sale_price: {brl(p_antes)} -> {brl(p_dep)}", flush=True)
    # IRMÃOS: cada um tem o SEU desconto individual (medido no MLB5320075152). Então a
    # pergunta "o desconto se propaga pelo bloco sincronizado?" é respondida aqui de graça,
    # olhando o preço deles DEPOIS da escrita — sem tocar em nenhum.
    for _irmao in (irm or {}):
        st_i, d_i = rec.get(f"/seller-promotions/items/{_irmao}?app_version=v2", access)
        _ind_i = None
        for _o in (d_i if isinstance(d_i, list) else []):
            if isinstance(_o, dict) and (_o.get("type") or "").upper() == "PRICE_DISCOUNT" \
                    and rec.eh_ativa(_o):
                _ind_i = _o
                break
        st_s, d_s = rec.get(f"/items/{_irmao}/sale_price?context=channel_marketplace", access)
        _amt = d_s.get("amount") if isinstance(d_s, dict) else None
        print(f"\n    IRMÃO {_irmao}: sale_price {brl(_amt)} | desconto individual "
              f"{brl(rec.preco_oferta(_ind_i)) if _ind_i else 'nenhum ativo'}", flush=True)
    dump("RESUMO", resultado)
    print("\n    Para desfazer: rode este mesmo workflow com DESFAZER=SIM.", flush=True)
    print("    (Isso REMOVE o desconto individual — o anúncio volta ao preço de lista ou "
          "à campanha que estiver ativa.)", flush=True)


if __name__ == "__main__":
    main()
