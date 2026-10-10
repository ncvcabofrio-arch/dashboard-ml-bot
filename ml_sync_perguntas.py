"""
PERGUNTAS DO MERCADO LIVRE -> Supabase (rede de seguranca do webhook).

Varre as 3 contas e grava na tabela 'perguntas':
  - TODAS as perguntas em aberto (status UNANSWERED), sem limite de data
  - as respondidas dos ultimos BL_DIAS dias, para o historico do cliente
    e para pegar o que foi respondido direto pelo site do ML

O webhook (ml_webhook, topico 'questions') traz a pergunta em segundos.
Este robo existe para o dia em que o webhook falhar — e para a primeira
carga, quando a tabela ainda esta vazia.

REGRAS QUE ELE NAO QUEBRA
  1. Nunca mexe no que o app escreveu: sugestao da IA, quem respondeu,
     ignorada. So' grava o que veio do Mercado Livre.
  2. Nunca "desresponde": se o app ja marcou como respondida e o ML ainda
     nao refletiu, mantem respondida.
  3. Pergunta antiga encontrada pela primeira vez entra como ja notificada
     — a primeira varredura nao dispara duzentos avisos no celular.
  4. Renova token pelo ml_auth.obter_access, o mesmo dos outros robos, e
     roda no grupo de concorrencia 'ml-puxador': refresh token do ML e' de
     uso unico, e dois robos renovando juntos derrubam a conta.

Variaveis
   ML_CLIENT_ID, ML_CLIENT_SECRET   (usadas pelo ml_auth)
   SUPABASE_URL, SUPABASE_KEY       obrigatorios
   ML_SELLERS     contas a varrer, separadas por virgula (padrao: as 3)
   BL_DIAS        respondidas de quantos dias para tras (padrao 30)
   BL_DRY_RUN     1 = le tudo e NAO grava
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

from ml_auth import obter_access

API = "https://api.mercadolibre.com"
DIAS = int(os.environ.get("BL_DIAS", "30") or 30)
DRY_RUN = os.environ.get("BL_DRY_RUN", "0") == "1"
SELLERS = [s.strip() for s in os.environ.get(
    "ML_SELLERS", "177795203,471489691,3244206480").split(",") if s.strip()]

# pergunta mais velha que isso, vista pela primeira vez, nao apita
JANELA_AVISO = timedelta(hours=2)

TABELA = "perguntas"


def _limpar_url(bruto):
    u = (bruto or "").strip().strip('"').strip("'").strip().rstrip("/")
    for sufixo in ("/rest/v1", "/rest"):
        if u.lower().endswith(sufixo):
            u = u[: -len(sufixo)].rstrip("/")
    return u


SUPABASE_URL = _limpar_url(os.environ.get("SUPABASE_URL", ""))
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY", "") or "").strip()
if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERRO] Faltam SUPABASE_URL ou SUPABASE_KEY.")
    sys.exit(1)

H = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
     "Content-Type": "application/json"}


# ---------------------------------------------------------------- Supabase

def sb_req(metodo, caminho, **kw):
    url = f"{SUPABASE_URL}/rest/v1/{caminho}"
    cab = {**H, **(kw.pop("headers", None) or {})}
    for t in range(3):
        try:
            r = requests.request(metodo, url, headers=cab, timeout=60, **kw)
        except requests.RequestException:
            if t == 2:
                raise
            time.sleep(2 * (t + 1))
            continue
        if r.status_code >= 400:
            print(f"[ERRO] Supabase {r.status_code}: {r.text[:300]}")
            if "PGRST205" in r.text or "does not exist" in r.text:
                print(f"   -> rode o sql_perguntas.sql e o sql_perguntas_v2.sql primeiro.")
            sys.exit(1)
        return r
    raise RuntimeError("Supabase nao respondeu")


def estado_atual(ids):
    """{id: status} das perguntas que ja estao no banco."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 150):
        bloco = ",".join(str(x) for x in ids[i:i + 150])
        r = sb_req("GET", f"{TABELA}?select=id,status&id=in.({bloco})")
        for l in r.json() or []:
            out[int(l["id"])] = l.get("status")
    return out


CAMPOS_ITEM = ("item_titulo", "item_thumb", "item_preco", "sku")


def itens_conhecidos(item_ids):
    """{item_id: dados do anuncio} copiados de outra pergunta ja gravada.
    Assim pergunta nova num anuncio conhecido nasce com titulo e foto, sem
    chamar o Mercado Livre de novo para um anuncio que ja lemos."""
    out = {}
    ids = list(item_ids)
    for i in range(0, len(ids), 100):
        bloco = ",".join(f'"{x}"' for x in ids[i:i + 100])
        r = sb_req("GET", f"{TABELA}?select=item_id,{','.join(CAMPOS_ITEM)}"
                          f"&item_titulo=not.is.null&item_id=in.({bloco})"
                          f"&order=recebido_em.desc")
        for l in r.json() or []:
            out.setdefault(l["item_id"], {k: l.get(k) for k in CAMPOS_ITEM})
    return out


def upsert(linhas, rotulo):
    """O PostgREST exige que todos os objetos do lote tenham as mesmas chaves.
    Por isso quem chama separa novas e existentes antes."""
    if not linhas:
        return
    for i in range(0, len(linhas), 200):
        sb_req("POST", TABELA,
               headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
               data=json.dumps(linhas[i:i + 200], ensure_ascii=False).encode("utf-8"))
    print(f"   {rotulo}: {len(linhas)} gravada(s)")


# -------------------------------------------------------------- Mercado Livre

def ml_get(caminho, token, params=None, tentativas=4):
    for t in range(tentativas):
        try:
            r = requests.get(f"{API}{caminho}", params=params,
                             headers={"Authorization": f"Bearer {token}"}, timeout=40)
        except requests.RequestException as e:
            if t == tentativas - 1:
                raise
            print(f"   rede falhou ({e}); tentando de novo...")
            time.sleep(2 * (t + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(3 * (t + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"ML {r.status_code} em {caminho}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"ML nao respondeu em {caminho}")


def buscar_perguntas(token, seller_id, status, ate=None):
    """Pagina /questions/search. 'ate' corta por data (para as respondidas,
    que sao muitas e so' interessam as recentes)."""
    achadas, offset, passo = [], 0, 50
    while True:
        d = ml_get("/questions/search", token, {
            "seller_id": seller_id, "status": status, "api_version": 4,
            "sort_fields": "date_created", "sort_types": "DESC",
            "limit": passo, "offset": offset,
        })
        lote = d.get("questions") or []
        if not lote:
            break
        parar = False
        for q in lote:
            if ate and _data(q.get("date_created")) < ate:
                parar = True
                break
            achadas.append(q)
        total = int(d.get("total") or 0)
        offset += passo
        if parar or offset >= total or offset >= 2000:
            break
        time.sleep(0.25)
    return achadas


def _data(s):
    if not s:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def dados_dos_itens(token, item_ids):
    """Titulo, foto, preco e SKU, de 20 em 20 (limite do multiget do ML)."""
    out = {}
    ids = list(item_ids)
    for i in range(0, len(ids), 20):
        bloco = ids[i:i + 20]
        try:
            resp = ml_get("/items", token, {
                "ids": ",".join(bloco),
                "attributes": "id,title,thumbnail,price,seller_custom_field,attributes",
            })
        except Exception as e:
            print(f"   aviso: nao li {len(bloco)} anuncio(s) ({e})")
            continue
        for r in resp or []:
            b = r.get("body") or {}
            if not b.get("id"):
                continue
            sku = b.get("seller_custom_field")
            if not sku:
                for a in b.get("attributes") or []:
                    if a.get("id") == "SELLER_SKU":
                        sku = a.get("value_name")
                        break
            out[b["id"]] = {
                "item_titulo": b.get("title"),
                "item_thumb": (b.get("thumbnail") or "").replace("http://", "https://") or None,
                "item_preco": b.get("price"),
                "sku": sku or None,
            }
        time.sleep(0.25)
    return out


# -------------------------------------------------------------- traducao

def status_local(q):
    """Status do ML -> status do app."""
    if q.get("deleted_from_listing"):
        return "excluida"
    s = str(q.get("status") or "").upper()
    if s == "ANSWERED":
        return "respondida"
    if s == "CLOSED_UNANSWERED":
        return "ignorada"
    return "pendente"          # UNANSWERED, UNDER_REVIEW


def linha(q, item):
    a = q.get("answer") or {}
    base = {
        "id": int(q["id"]),
        "seller_id": str(q.get("seller_id") or ""),
        "item_id": str(q.get("item_id") or ""),
        "pergunta": q.get("text") or "",
        "perguntado_em": q.get("date_created"),
        "comprador_id": str((q.get("from") or {}).get("id") or "") or None,
        "status": status_local(q),
        "resposta": a.get("text") or None,
        "respondida_em": a.get("date_created") or None,
    }
    base.update(item or {
        "item_titulo": None, "item_thumb": None, "item_preco": None, "sku": None})
    return base


# ------------------------------------------------------------------- main

def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    agora = datetime.now(timezone.utc)
    corte_respondidas = agora - timedelta(days=DIAS)

    print(f"Contas: {', '.join(SELLERS)} | respondidas dos ultimos {DIAS} dias"
          + ("  [DRY RUN: nao grava nada]" if DRY_RUN else ""))

    contas = sb.table("contas").select("seller_id,refresh_token,apelido") \
               .in_("seller_id", SELLERS).execute().data or []
    achadas_ids = {str(c["seller_id"]) for c in contas}
    faltam = [s for s in SELLERS if s not in achadas_ids]
    if faltam:
        print(f"⚠️ Nao achei na tabela 'contas': {', '.join(faltam)}")

    todas = []
    for c in contas:
        sid, apelido = str(c["seller_id"]), c.get("apelido") or c["seller_id"]
        try:
            token, _, _ = obter_access(sb, sid, c.get("refresh_token"))
        except Exception as e:
            print(f"\n❌ {apelido}: nao consegui token ({e}). Pulo esta conta.")
            continue

        abertas = buscar_perguntas(token, sid, "UNANSWERED")
        respondidas = buscar_perguntas(token, sid, "ANSWERED", ate=corte_respondidas)
        print(f"\n{apelido} ({sid}): {len(abertas)} em aberto, "
              f"{len(respondidas)} respondidas nos ultimos {DIAS} dias")

        perguntas = abertas + respondidas
        todos_itens = {str(q.get("item_id")) for q in perguntas}
        conhecidos = itens_conhecidos(todos_itens)
        novos_itens = todos_itens - set(conhecidos)
        itens = dict(conhecidos)
        if novos_itens:
            lidos = dados_dos_itens(token, novos_itens)
            itens.update(lidos)
            print(f"   {len(lidos)} anuncio(s) novo(s) lido(s) para titulo e foto")

        for q in perguntas:
            todas.append((q, itens.get(str(q.get("item_id")))))

    if not todas:
        print("\nNenhuma pergunta encontrada.")
        return

    # ---- separa o que e' novo do que ja existe
    existente = estado_atual(int(q["id"]) for q, _ in todas)
    novas, atualizar, protegidas = [], [], 0

    for q, item in todas:
        l = linha(q, item)
        st_banco = existente.get(l["id"])

        # regra 2: o app ja respondeu ou ignorou, o ML ainda nao refletiu
        if st_banco in ("respondida", "ignorada") and l["status"] == "pendente":
            l["status"] = st_banco
            protegidas += 1

        # sem dado do anuncio (leitura falhou): nao manda vazio por cima do bom
        if item is None:
            for k in CAMPOS_ITEM:
                l.pop(k, None)

        if st_banco is None:
            # regra 3: pergunta antiga vista pela 1a vez nao apita
            antiga = _data(l["perguntado_em"]) < agora - JANELA_AVISO
            l["notificada"] = antiga or l["status"] != "pendente"
            novas.append(l)
        else:
            atualizar.append(l)

    # o PostgREST quer chaves iguais no lote: agrupa pelo formato
    def por_formato(linhas):
        grupos = {}
        for l in linhas:
            grupos.setdefault(tuple(sorted(l)), []).append(l)
        return grupos.values()

    pend_novas = [l for l in novas if l["status"] == "pendente"]
    vao_apitar = [l for l in pend_novas if not l["notificada"]]

    print("\n" + "=" * 60)
    print("RESULTADO")
    print("=" * 60)
    print(f"  perguntas lidas ............ {len(todas)}")
    print(f"  novas no banco ............. {len(novas)} "
          f"({len(pend_novas)} em aberto)")
    print(f"  ja existiam ................ {len(atualizar)}")
    print(f"  vao gerar aviso ............ {len(vao_apitar)}")
    if protegidas:
        print(f"  protegidas (app ja tinha respondido) .. {protegidas}")
    for l in vao_apitar[:10]:
        print(f"     • {l['seller_id']} | {l['pergunta'][:70]}")

    if DRY_RUN:
        print("\n[DRY RUN] nada gravado.")
        return

    for g in por_formato(novas):
        upsert(g, "novas")
    for g in por_formato(atualizar):
        upsert(g, "atualizadas")

    # Chama SEMPRE, nao so' quando achou pergunta nova: se o push do webhook
    # falhou e devolveu alguma pergunta para a fila, e' aqui que ela apita.
    # A funcao marca e avisa numa operacao so', entao chamar a mais nao duplica.
    avisar()
    print(f"\n✅ concluido em {datetime.now(timezone.utc):%H:%M:%S} UTC")


def avisar():
    try:
        r = requests.post(f"{SUPABASE_URL}/functions/v1/perguntas_push",
                          headers={"Authorization": f"Bearer {SUPABASE_KEY}",
                                   "Content-Type": "application/json"},
                          data="{}", timeout=30)
        if r.status_code == 404:
            print("   aviso: a funcao perguntas_push ainda nao foi publicada")
        elif r.status_code in (401, 403):
            print(f"   aviso: perguntas_push recusou a chave (HTTP {r.status_code}) — "
                  f"o SUPABASE_KEY do GitHub precisa ser a service_role")
        else:
            print(f"   aviso: {r.text[:160]}")
    except Exception as e:
        print(f"   aviso: nao consegui chamar perguntas_push ({e})")


if __name__ == "__main__":
    main()
