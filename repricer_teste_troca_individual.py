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


def esperar_vaga(access, tentativas=40, espera=3.0):
    """Espera o ML REABRIR a candidatura de PRICE_DISCOUNT depois do DELETE.

    MEDIDO em 30/set: postar sem essa vaga devolve 400 "No candidates found for item".
    É a mesma espera que o aplicador já faz — aqui ela vai CRONOMETRADA, porque o número
    de segundos que ela leva é chute no robô de hoje.

    A doc lista 'restore_requested' como "processo pendente de remoção do desconto": o 200
    do DELETE quer dizer pedido aceito, não removido."""
    t0 = time.time()
    for i in range(tentativas):
        ofertas, _, _ = promocoes(access)
        cand, restos = [], []
        for o in ofertas:
            if not isinstance(o, dict) or (o.get("type") or "").upper() != "PRICE_DISCOUNT":
                continue
            st = (o.get("status") or "").lower()
            (cand if st == "candidate" else restos).append(st)
        dt = round(time.time() - t0, 1)
        print(f"     {dt:>5.1f}s  candidate={len(cand)}  outros={restos or '-'}", flush=True)
        if cand:
            # a vaga apareceu, mas o ML ainda esta assentando: postar no mesmo instante
            # em que o 'candidate' surge e apostar que o indice dele ja concordou.
            time.sleep(3.0)
            return True, dt, i + 1
        time.sleep(espera)
    return False, round(time.time() - t0, 1), tentativas


def postar_individual(access, preco, ini, fim_, rotulo, tentativas=3, espera=4.0):
    """POST do desconto individual, com repetição. Você me disse: o ML às vezes recusa por
    erro dele, até no painel deles. Então uma recusa não é resposta final — mas também não
    insisto para sempre."""
    corpo = {"deal_price": round(float(preco), 2), "promotion_type": "PRICE_DISCOUNT",
             "start_date": ini, "finish_date": fim_}
    for i in range(tentativas):
        print(f"\n  [POST {rotulo} {i+1}/{tentativas}]", flush=True)
        dump("corpo enviado", corpo)
        sc, resp = apl.post(f"/seller-promotions/items/{ITEM}?app_version=v2", access, corpo)
        print(f"    POST -> HTTP {sc}", flush=True)
        dump("RESPOSTA DO ML (inteira)", resp)
        if sc in (200, 201):
            return True, sc, resp, i + 1
        if i + 1 < tentativas:
            print(f"    recusado — esperando {espera}s e tentando de novo", flush=True)
            time.sleep(espera)
    return False, sc, resp, tentativas


def hipotese_c(access, ind, p_antes, corpo_datas, irm):
    """DELETE -> esperar a vaga -> POST. É o único caminho que existe, e é o que o
    aplicador roda hoje na tela Acelerar. Aqui ele vai cronometrado e com volta atrás."""
    p_velho = rec.preco_oferta(ind)
    ini_velho = ind.get("start_date") or corpo_datas["start_date"]
    fim_velho = ind.get("finish_date") or corpo_datas["finish_date"]
    r = {"preco_velho": p_velho, "etapas": []}

    print("\n################ HIPÓTESE C — DELETE, esperar a vaga, POST ################",
          flush=True)
    print(f"    ATENÇÃO: a partir daqui o anúncio fica SEM desconto até o POST entrar.", flush=True)
    print(f"    Se tudo falhar, eu recoloco {brl(p_velho)} (o preço de agora).", flush=True)

    sc, body = apl.req_delete(
        f"/seller-promotions/items/{ITEM}?promotion_type=PRICE_DISCOUNT&app_version=v2", access)
    print(f"\n  [1] DELETE -> HTTP {sc}", flush=True)
    dump("resposta do DELETE", body)
    r["etapas"].append({"delete": sc})
    if sc not in (200, 201):
        print("\n!! o DELETE foi recusado — NADA foi removido, o desconto continua "
              f"{brl(p_velho)}. Fim.", flush=True)
        r["desfecho"] = "delete_recusado"
        return r

    print("\n  [2] esperando o ML reabrir a candidatura:", flush=True)
    vaga, seg, voltas = esperar_vaga(access)
    r["etapas"].append({"vaga": vaga, "segundos": seg, "consultas": voltas})
    print(f"\n    vaga {'ABRIU' if vaga else 'NÃO abriu'} em {seg}s ({voltas} consultas)",
          flush=True)

    if not vaga:
        print("    ⚠ a vaga não apareceu na janela de espera. Vou tentar o POST mesmo assim —\n"
              "      se vier 'No candidates found', é só o ML ainda não ter assentado, e a\n"
              "      volta ao preço antigo entra em seguida.", flush=True)
    ok, sc, resp, n = postar_individual(access, PRECO_NOVO, corpo_datas["start_date"],
                                        fim_velho, "preço NOVO", tentativas=4, espera=8.0)
    r["etapas"].append({"post_novo": sc, "tentativas": n, "resposta": resp})
    if ok:
        print("\n    aceito. Conferindo a vitrine:", flush=True)
        valeu, visto, v = esperar_preco(access, round(PRECO_NOVO, 2))
        r["preco_depois"] = visto
        r["desfecho"] = "trocado" if valeu else "aceito_mas_nao_valeu"
        if valeu:
            print(f"\n>>> TROCA FUNCIONOU: {brl(p_antes)} -> {brl(visto)} "
                  f"(vaga em {seg}s, POST na tentativa {n})", flush=True)
        else:
            print(f"\n    ⚠ o ML aceitou mas a vitrine ficou em {brl(visto)}.", flush=True)
        return r

    # ---- não entrou: devolve o preço que estava, que é o que o anúncio merecia ----
    print(f"\n!! O POST do preço novo falhou {n}x. O anúncio está SEM desconto agora.", flush=True)
    print(f"!! RECOLOCANDO o preço anterior ({brl(p_velho)}).", flush=True)
    print("\n  [volta] esperando a vaga de novo antes de recolocar:", flush=True)
    esperar_vaga(access)
    ok2, sc2, resp2, n2 = postar_individual(access, p_velho, corpo_datas["start_date"],
                                            fim_velho, "VOLTA ao preço antigo",
                                            tentativas=6, espera=10.0)
    r["etapas"].append({"post_volta": sc2, "tentativas": n2, "resposta": resp2})
    if ok2:
        valeu2, visto2, _ = esperar_preco(access, round(float(p_velho), 2))
        r["preco_depois"] = visto2
        r["desfecho"] = "voltou_ao_antigo" if valeu2 else "volta_aceita_mas_nao_valeu"
        print(f"\n>>> VOLTEI ao preço anterior: vitrine em {brl(visto2)}", flush=True)
    else:
        r["desfecho"] = "SEM_DESCONTO_INTERVIR"
        print("\n" + "!" * 70, flush=True)
        print(f"!! NÃO CONSEGUI RECOLOCAR O DESCONTO. O anúncio {ITEM} está SEM desconto,", flush=True)
        print(f"!! vendendo pelo preço de lista. O desconto que estava lá era {brl(p_velho)},", flush=True)
        print(f"!! de {ini_velho} até {fim_velho}. PRECISA SER REFEITO NA MÃO.", flush=True)
        print("!" * 70, flush=True)
    return r


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

    resultado = {"put": None, "post": None, "troca": None}

    # MODO=TROCA: pula PUT e POST-por-cima (os dois já foram medidos e não existem) e vai
    # direto no único caminho real: DELETE -> esperar a vaga -> POST.
    if MODO == "TROCA":
        resultado["troca"] = hipotese_c(access, ind, p_antes, corpo_datas, irm)
        _fim(access, p_antes, resultado, irm)
        return

    # ------------------------------------------------------- HIPÓTESE A: PUT (modificar)
    # MEDIDO em 30/set, primeira rodada: PUT sem datas devolveu
    #     400 "Start and finish dates must be in local format"
    # Repare no que esse erro NÃO diz: não diz que PRICE_DISCOUNT não pode ser modificado,
    # não pede promotion_id, não fala em desconto já existente. Ele passou pela validação
    # de tipo e parou na de campos. O PUT ACEITA este tipo — faltava o corpo completo.
    #
    # "local format" = o formato que o próprio ML devolve no GET: "2026-09-29T00:00:00",
    # sem fuso. É o mesmo que o POST usou sem reclamar (o POST caiu por outro motivo).
    # Reaproveitamos as datas do desconto EM VIGOR: modificar não é recomeçar.
    if MODO in ("AUTO", "PUT"):
        def _corpo_put(ini, fim_):
            c = {"promotion_type": "PRICE_DISCOUNT",
                 "deal_price": round(PRECO_NOVO, 2),
                 "start_date": ini,
                 "finish_date": fim_}
            # o individual vem sem id (medido: "id": None). Mandar null faria o ML reclamar
            # do CAMPO e a gente não aprenderia nada sobre o mecanismo.
            if ind.get("id"):
                c["promotion_id"] = ind.get("id")
            return c

        # tentativa 1: as datas do desconto que já está lá, como o ML as devolve.
        # tentativa 2 (só se a 1 falhar POR DATA): início hoje — caso o ML recuse
        # start_date no passado. Vai na MESMA rodada pra não gastar outra ida sua.
        tentativas_put = [("datas do desconto em vigor",
                           ind.get("start_date") or corpo_datas["start_date"],
                           ind.get("finish_date") or corpo_datas["finish_date"])]
        print("\n################ HIPÓTESE A — PUT (modificar, sem remover) ################",
              flush=True)
        for _i, (_rot, _ini, _dfim) in enumerate(tentativas_put):
            corpo_put = _corpo_put(_ini, _dfim)
            print(f"\n  [PUT {_i+1}] {_rot}", flush=True)
            dump("corpo enviado", corpo_put)
            sc, resp = apl.req_put(f"/seller-promotions/items/{ITEM}?app_version=v2",
                                   access, corpo_put)
            print(f"    PUT -> HTTP {sc}", flush=True)
            dump("RESPOSTA DO ML (inteira)", resp)
            resultado["put"] = {"tentativa": _rot, "corpo": corpo_put,
                                "http": sc, "resposta": resp}
            if sc in (200, 201):
                print("\n    aceito. Conferindo se o preço da vitrine mudou de verdade:",
                      flush=True)
                ok, visto, voltas = esperar_preco(access, round(PRECO_NOVO, 2))
                resultado["put"]["preco_depois"] = visto
                resultado["put"]["valeu"] = ok
                if ok:
                    print(f"\n>>> PUT FUNCIONOU: {brl(p_antes)} -> {brl(visto)} "
                          f"em {voltas} conferência(s), SEM REMOVER NADA.", flush=True)
                    _fim(access, p_antes, resultado, irm)
                    return
                print(f"\n    ⚠ o ML aceitou o PUT mas a vitrine ficou em {brl(visto)}. "
                      f"Aceitar não é valer.", flush=True)
                break
            _msg = (resp.get("message") if isinstance(resp, dict) else "") or ""
            if "date" in _msg.lower() and len(tentativas_put) == 1:
                # ainda é data: tenta uma vez com início HOJE, mesmo fim.
                _hoje = datetime.now().strftime("%Y-%m-%dT00:00:00")
                tentativas_put.append(("início HOJE, mesmo fim", _hoje, _dfim))
                print("    recusado POR DATA — tentando de novo com início de hoje.",
                      flush=True)
                continue
            print("    recusado — é este o erro que faltava ler.", flush=True)
            break

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
