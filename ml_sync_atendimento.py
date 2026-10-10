"""
ATENDIMENTO DO MERCADO LIVRE -> Supabase (mensagens pos-venda e reclamacoes)

Grava nas tabelas atend_conversas / atend_mensagens (sql_atendimento.sql).

DE ONDE VEM CADA CONVERSA
  1. a FILA (atend_fila): o ml_webhook anota cada aviso 'messages' e
     'post_purchase' do ML e acorda este robo — e' o caminho rapido;
  2. as conversas NAO LIDAS do ML (rede de seguranca);
  3. as reclamacoes ABERTAS (busca completa a cada rodada — sao poucas);
  4. uma vez por hora, os pedidos do ultimo dia (pega conversa que o cliente
     abriu e que alguem ja' leu em outro sistema, tipo o Responso);
  5. as conversas abertas do banco, para atualizar.

REGRAS QUE ELE NAO QUEBRA
  - NUNCA marca mensagem como lida no ML (mark_as_read=false).
  - Nunca envia nada: so' le.
  - Renova token pelo ml_auth.obter_access, no grupo 'ml-puxador'.
  - Mensagem antiga vista pela primeira vez nao apita no celular.

Variaveis
   ML_CLIENT_ID, ML_CLIENT_SECRET   (ml_auth)
   SUPABASE_URL, SUPABASE_KEY       obrigatorios
   PERGUNTAS_PUSH_SECRET            para o aviso no celular
   ML_SELLERS     contas (padrao: as 3)
   BL_DIAS        dias de pedidos para varrer (padrao: 1 por hora; 0 = nao varre)
   BL_DRY_RUN     1 = le tudo e NAO grava
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

from ml_auth import obter_access

API = "https://api.mercadolibre.com"
DRY_RUN = os.environ.get("BL_DRY_RUN", "0") == "1"
SELLERS = [s.strip() for s in os.environ.get(
    "ML_SELLERS", "177795203,471489691,3244206480").split(",") if s.strip()]
_dias = (os.environ.get("BL_DIAS") or "").strip()
AGORA = datetime.now(timezone.utc)
# sem valor: 1 dia, so' na primeira rodada de cada hora (as outras sao leves)
DIAS_PEDIDOS = int(_dias) if _dias else (1 if AGORA.minute < 15 else 0)
AVISO_JANELA = timedelta(hours=3)        # mensagem mais velha que isso nao apita
PRAZO_MSG = timedelta(hours=24)          # prazo de referencia das mensagens
INICIO = time.time()
# a carga grande (varrer pedidos de muitos dias) para antes de estourar o tempo do
# GitHub; o que faltou entra na proxima rodada. Fila, nao lidas e reclamacoes
# sempre sao lidas (sao poucas e sao as que importam).
TEMPO_MAX = 18 * 60
MAX_REFRESH = 60                         # conversas do banco atualizadas por rodada

ACOES_PT = {
    "send_message_to_complainant": "Responder ao comprador",
    "send_message_to_mediator": "Responder ao mediador",
    "refund": "Devolver o dinheiro",
    "allow_partial_refund": "Oferecer reembolso parcial",
    "open_dispute": "Pedir mediação",
    "send_potential_shipping": "Informar envio",
    "add_shipping_evidence": "Enviar comprovante de envio",
    "send_attachments": "Enviar anexos",
    "send_tracking_number": "Informar rastreio",
    "allow_return": "Aceitar devolução",
    "allow_return_label": "Gerar etiqueta de devolução",
    "recontact": "Recontato",
}


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
                print("   -> rode o sql_atendimento.sql primeiro.")
            sys.exit(1)
        return r
    raise RuntimeError("Supabase nao respondeu")


def sb_get(caminho):
    return sb_req("GET", caminho).json() or []


def upsert(tabela, linhas):
    if not linhas or DRY_RUN:
        return
    grupos = {}
    for l in linhas:
        grupos.setdefault(tuple(sorted(l)), []).append(l)
    for g in grupos.values():
        for i in range(0, len(g), 200):
            sb_req("POST", tabela, headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                   data=json.dumps(g[i:i + 200], ensure_ascii=False, default=str).encode("utf-8"))


# -------------------------------------------------------------- Mercado Livre

class MLErro(Exception):
    def __init__(self, status, texto):
        super().__init__(f"ML {status}: {texto[:160]}")
        self.status = status


def ml_get(caminho, token, params=None, headers=None, tentativas=4):
    for t in range(tentativas):
        try:
            r = requests.get(f"{API}{caminho}", params=params, timeout=40,
                             headers={"Authorization": f"Bearer {token}", **(headers or {})})
        except requests.RequestException:
            if t == tentativas - 1:
                raise
            time.sleep(2 * (t + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(3 * (t + 1))
            continue
        if r.status_code >= 400:
            raise MLErro(r.status_code, r.text)
        return r.json()
    raise MLErro(0, f"sem resposta em {caminho}")


def iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- pedidos

_pedidos = {}


def dados_do_pedido(token, pack_ou_pedido):
    """Titulo, SKU, valor, comprador. Aceita pack_id ou order_id."""
    if pack_ou_pedido in _pedidos:
        return _pedidos[pack_ou_pedido]
    o = None
    try:
        o = ml_get(f"/orders/{pack_ou_pedido}", token)
    except MLErro as e:
        if e.status in (400, 403, 404):
            try:
                p = ml_get(f"/packs/{pack_ou_pedido}", token)
                oid = ((p.get("orders") or [{}])[0] or {}).get("id")
                if oid:
                    o = ml_get(f"/orders/{oid}", token)
            except MLErro:
                o = None
    out = {}
    if o:
        it = ((o.get("order_items") or [{}])[0] or {})
        item = it.get("item") or {}
        out = {
            "order_id": str(o.get("id") or ""),
            "pack_id": str(o.get("pack_id") or o.get("id") or ""),
            "comprador_id": str((o.get("buyer") or {}).get("id") or "") or None,
            "comprador_nick": (o.get("buyer") or {}).get("nickname"),
            "item_id": item.get("id"),
            "item_titulo": item.get("title"),
            "sku": item.get("seller_sku") or item.get("seller_custom_field"),
            "valor": o.get("total_amount"),
            "pedido_em": o.get("date_created"),
        }
    _pedidos[pack_ou_pedido] = out
    return out


def fotos(token, item_ids):
    out = {}
    ids = [i for i in item_ids if i]
    for i in range(0, len(ids), 20):
        try:
            r = ml_get("/items", token, {"ids": ",".join(ids[i:i + 20]), "attributes": "id,thumbnail"})
        except Exception:
            continue
        for x in r or []:
            b = x.get("body") or {}
            if b.get("id"):
                out[b["id"]] = (b.get("thumbnail") or "").replace("http://", "https://") or None
    return out


# ---------------------------------------------------------------- estado

def calcular_status(msgs, anterior, bloqueada=False, encerrada=False, acao_mensagem=False):
    """msgs: lista de dicts (de, criada_em) em ordem. Devolve campos da conversa."""
    if not msgs:
        ult = None
    else:
        ult = msgs[-1]
    sem = 0
    primeira_sem_resposta = None
    for m in reversed(msgs):
        if m["de"] == "loja":
            break
        if m["de"] in ("cliente", "mediador"):
            sem += 1
            primeira_sem_resposta = m["criada_em"]
    if encerrada:
        st = "fechada"
    elif bloqueada:
        st = "bloqueada"
    elif anterior and anterior.get("status") == "fechada":
        fechada_em = iso(anterior.get("fechada_em"))
        novo = any(m["de"] in ("cliente", "mediador") and m["criada_em"] and fechada_em and m["criada_em"] > fechada_em
                   for m in msgs)
        st = "aberta" if novo else "fechada"
    elif sem > 0 or acao_mensagem:
        st = "aberta"
    else:
        st = "respondida"
    return {
        "status": st,
        "ultima_msg_em": ult["criada_em"].isoformat() if ult and ult["criada_em"] else None,
        "ultima_msg_de": ult["de"] if ult else None,
        "ultima_msg_texto": (ult.get("texto") or "")[:300] if ult else None,
        "sem_resposta": sem,
        "_primeira_sem_resposta": primeira_sem_resposta,
    }


# ---------------------------------------------------------------- mensagens

def msgs_do_pack(token, sid, pack):
    """Le a conversa SEM marcar como lida."""
    d = ml_get(f"/messages/packs/{pack}/sellers/{sid}", token,
               {"tag": "post_sale", "mark_as_read": "false", "limit": 100})
    conv = d.get("conversation_status") or {}
    out = []
    for m in d.get("messages") or []:
        de = "loja" if str((m.get("from") or {}).get("user_id")) == str(sid) else "cliente"
        data = iso(((m.get("message_date") or {}).get("created")) or m.get("date_created"))
        anexos = [{"nome": a.get("original_filename") or a.get("filename"), "arquivo": a.get("filename"),
                   "tipo": a.get("type"), "tamanho": a.get("size")} for a in (m.get("message_attachments") or [])]
        mod = (m.get("message_moderation") or {}).get("status")
        out.append({"id": str(m.get("id")), "de": de, "texto": m.get("text") if isinstance(m.get("text"), str)
                    else (m.get("text") or {}).get("plain"), "anexos": anexos, "criada_em": data,
                    "moderacao": None if mod in (None, "clean", "non_moderated") else mod})
    out.sort(key=lambda x: x["criada_em"] or datetime(1970, 1, 1, tzinfo=timezone.utc))
    return out, conv


def pack_da_mensagem(token, msg_id):
    try:
        m = ml_get(f"/messages/{msg_id}", token, headers={"X-Pack-Format": "true"})
    except MLErro:
        return None
    for r in m.get("message_resources") or []:
        if r.get("name") in ("packs", "orders") and r.get("id"):
            return str(r["id"])
    return None


def packs_nao_lidos(token):
    try:
        d = ml_get("/messages/unread", token, {"role": "seller", "tag": "post_sale"})
    except MLErro as e:
        print(f"   aviso: nao li as nao lidas ({e})")
        return set()
    out = set()
    for r in d.get("results") or []:
        mm = re.search(r"/(?:packs|orders)/(\d+)", str(r.get("resource") or ""))
        if mm:
            out.add(mm.group(1))
    return out


def packs_dos_pedidos(token, sid, dias):
    if dias <= 0:
        return set()
    desde = (AGORA - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    out, offset = set(), 0
    while True:
        try:
            d = ml_get("/orders/search", token, {"seller": sid, "order.date_created.from": desde,
                                                  "sort": "date_desc", "limit": 50, "offset": offset})
        except MLErro as e:
            print(f"   aviso: parei de ler pedidos em {offset} ({e})")
            break
        res = d.get("results") or []
        for o in res:
            out.add(str(o.get("pack_id") or o.get("id")))
        offset += 50
        if not res or offset >= int((d.get("paging") or {}).get("total") or 0) or offset >= 5000:
            break
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- reclamacoes

_motivos = {}


def nome_motivo(token, reason_id):
    if not reason_id:
        return None
    if reason_id not in _motivos:
        try:
            r = ml_get(f"/post-purchase/v1/claims/reasons/{reason_id}", token)
            _motivos[reason_id] = r.get("detail") or r.get("name") or reason_id
        except Exception:
            _motivos[reason_id] = reason_id
    return _motivos[reason_id]


def reclamacoes_abertas(token, sid):
    out, offset = [], 0
    while True:
        try:
            d = ml_get("/post-purchase/v1/claims/search", token, {
                "players.role": "respondent", "players.user_id": sid, "status": "opened",
                "sort": "last_updated:desc", "limit": 30, "offset": offset})
        except MLErro as e:
            print(f"   aviso: parei de ler reclamacoes em {offset} ({e})")
            break
        lote = d.get("data") or []
        out.extend(lote)
        offset += 30
        if not lote or offset >= int((d.get("paging") or {}).get("total") or 0) or offset >= 600:
            break
    return out


def acoes_do_vendedor(c, sid):
    acoes = []
    for p in c.get("players") or []:
        if p.get("role") == "respondent" or str(p.get("user_id")) == str(sid):
            for a in p.get("available_actions") or []:
                acoes.append({"acao": a.get("action"), "nome": ACOES_PT.get(a.get("action"), a.get("action")),
                              "prazo": a.get("due_date"), "obrigatoria": bool(a.get("mandatory"))})
    acoes.sort(key=lambda a: (not a["obrigatoria"], a["prazo"] or "9999"))
    return acoes


def msgs_da_reclamacao(token, claim_id):
    try:
        d = ml_get(f"/post-purchase/v1/claims/{claim_id}/messages", token)
    except MLErro as e:
        print(f"   aviso: nao li mensagens da reclamacao {claim_id} ({e})")
        return []
    lista = d if isinstance(d, list) else (d.get("data") or d.get("messages") or [])
    out = []
    papel = {"complainant": "cliente", "respondent": "loja", "mediator": "mediador"}
    for m in lista:
        data = iso(m.get("date_created"))
        de = papel.get(m.get("sender_role"), "ml")
        texto = m.get("message") or ""
        mid = m.get("id") or hashlib.sha1(f"{claim_id}|{m.get('date_created')}|{de}|{texto[:60]}".encode()).hexdigest()[:20]
        out.append({"id": f"rec:{claim_id}:{mid}", "de": de, "texto": texto, "criada_em": data,
                    "anexos": [{"nome": a.get("original_filename") or a.get("filename"), "arquivo": a.get("filename"),
                                "tipo": a.get("type")} for a in (m.get("attachments") or [])],
                    "moderacao": (m.get("message_moderation") or {}).get("status") if isinstance(m.get("message_moderation"), dict) else None})
    out.sort(key=lambda x: x["criada_em"] or datetime(1970, 1, 1, tzinfo=timezone.utc))
    return out


# ---------------------------------------------------------------- gravacao

def existentes(conv_ids):
    out = {}
    ids = list(conv_ids)
    for i in range(0, len(ids), 80):
        bloco = ",".join(f'"{x}"' for x in ids[i:i + 80])
        for l in sb_get(f"atend_conversas?select=id,status,fechada_em,item_titulo,motivo_nome&id=in.({bloco})"):
            out[l["id"]] = l
    return out


def msgs_existentes(conv_ids):
    out = {}
    ids = list(conv_ids)
    for i in range(0, len(ids), 40):
        bloco = ",".join(f'"{x}"' for x in ids[i:i + 40])
        for l in sb_get(f"atend_mensagens?select=id,conversa_id,de,texto&conversa_id=in.({bloco})"):
            out.setdefault(l["conversa_id"], []).append(l)
    return out


def normal(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    print(f"Contas: {', '.join(SELLERS)} | pedidos dos ultimos {DIAS_PEDIDOS} dia(s)"
          + ("  [DRY RUN]" if DRY_RUN else ""))

    contas = sb.table("contas").select("seller_id,refresh_token,apelido").in_("seller_id", SELLERS).execute().data or []
    fila = sb_get("atend_fila?select=id,topic,resource,seller_id&processado=eq.false&order=recebido_em&limit=500")
    print(f"Fila do webhook: {len(fila)} aviso(s)")

    conversas, mensagens, avisar = [], [], []
    fila_ok = []
    tot = {"conversas": 0, "abertas": 0, "mensagens": 0}

    for c in contas:
        sid, apelido = str(c["seller_id"]), c.get("apelido") or c["seller_id"]
        try:
            token, _, _ = obter_access(sb, sid, c.get("refresh_token"))
        except Exception as e:
            print(f"\n❌ {apelido}: nao consegui token ({e}). Pulo esta conta.")
            continue
        print(f"\n{apelido} ({sid})")

        minha_fila = [f for f in fila if str(f.get("seller_id") or "") == sid]
        # ------------------------------------------------ MENSAGENS
        packs = set()
        for f in minha_fila:
            if f["topic"] == "messages":
                p = pack_da_mensagem(token, str(f["resource"]).strip("/").split("/")[-1])
                if p:
                    packs.add(p)
        n_fila = len(packs)
        packs |= packs_nao_lidos(token)
        n_nl = len(packs) - n_fila
        abertas_banco = sb_get(
            "atend_conversas?select=pack_id&tipo=eq.mensagem&status=in.(aberta,respondida)"
            f"&seller_id=eq.{sid}&order=atualizado_em.asc&limit={MAX_REFRESH}")
        packs |= {str(x["pack_id"]) for x in abertas_banco if x.get("pack_id")}
        prioridade = set(packs)
        dos_pedidos = packs_dos_pedidos(token, sid, DIAS_PEDIDOS) - prioridade
        packs |= dos_pedidos
        print(f"   mensagens: {len(packs)} conversa(s) para ler (fila {n_fila}, nao lidas {n_nl}, "
              f"pedidos {len(dos_pedidos)})")
        # tempo desta conta para varrer pedidos (divide o que sobra entre as contas que faltam)
        faltam = max(1, len(contas) - contas.index(c))
        limite_conta = time.time() + max(60, (TEMPO_MAX - (time.time() - INICIO)) / faltam)

        ids_conv = [f"msg:{p}" for p in packs]
        antes = existentes(ids_conv)
        ja_msgs = msgs_existentes(ids_conv)
        novas_conv_msg = []
        ordem = sorted(prioridade) + sorted(dos_pedidos, reverse=True)  # mais novos primeiro
        lidos = 0
        for p in ordem:
            if p in dos_pedidos and time.time() > limite_conta:
                print(f"   ⏸ tempo desta conta acabou: li {lidos} de {len(ordem)}; o resto entra nas proximas rodadas")
                break
            lidos += 1
            if lidos % 200 == 0:
                print(f"   ... {lidos}/{len(ordem)} ({(time.time() - INICIO) / 60:.0f} min)")
            try:
                msgs, conv = msgs_do_pack(token, sid, p)
            except MLErro as e:
                print(f"   aviso: pack {p}: {e}")
                continue
            if not msgs:
                continue
            cid = f"msg:{p}"
            ant = antes.get(cid)
            bloq = str(conv.get("status") or "") == "blocked"
            linha = {"id": cid, "tipo": "mensagem", "seller_id": sid, "pack_id": p,
                     "status_ml": (conv.get("status") or "") + (f":{conv.get('substatus')}" if conv.get("substatus") else "")}
            est = calcular_status(msgs, ant, bloqueada=bloq)
            prim = est.pop("_primeira_sem_resposta")
            linha.update(est)
            linha["prazo_em"] = (prim + PRAZO_MSG).isoformat() if est["status"] == "aberta" and prim else None
            linha["prazo_tipo"] = "referencia" if linha["prazo_em"] else None
            if not ant or not ant.get("item_titulo"):
                linha.update(dados_do_pedido(token, p))
                linha["pack_id"] = p
                novas_conv_msg.append(linha)
            conversas.append(linha)
            existentes_txt = {normal(x["texto"]) for x in ja_msgs.get(cid, []) if x["de"] == "loja"}
            ids_ja = {x["id"] for x in ja_msgs.get(cid, [])}
            for m in msgs:
                if m["de"] == "loja" and m["id"] not in ids_ja and normal(m["texto"]) in existentes_txt:
                    continue  # ja' esta' gravada (enviada pelo app)
                mensagens.append({"id": m["id"], "conversa_id": cid, "de": m["de"], "texto": m["texto"],
                                  "anexos": m["anexos"], "criada_em": m["criada_em"].isoformat() if m["criada_em"] else None,
                                  "moderacao": m["moderacao"]})
                if (m["id"] not in ids_ja and m["de"] == "cliente" and m["criada_em"]
                        and m["criada_em"] > AGORA - AVISO_JANELA):
                    avisar.append({"conversa": cid, "tipo": "mensagem", "seller_id": sid, "texto": m["texto"] or "(anexo)"})
            time.sleep(0.15)

        # ------------------------------------------------ RECLAMACOES
        rec = reclamacoes_abertas(token, sid)
        ids_abertas = {str(x.get("id")) for x in rec}
        # reclamacoes que estavam abertas no banco e sairam da lista: conferir
        no_banco = sb_get(f"atend_conversas?select=claim_id&tipo=eq.reclamacao&seller_id=eq.{sid}&status=neq.fechada&limit=200")
        sumiram = [str(x["claim_id"]) for x in no_banco if str(x["claim_id"]) not in ids_abertas]
        for f in minha_fila:
            if f["topic"] == "post_purchase":
                mm = re.search(r"claims/(\d+)", str(f["resource"]))
                if mm and mm.group(1) not in ids_abertas:
                    sumiram.append(mm.group(1))
        for cid_ml in list(dict.fromkeys(sumiram))[:40]:
            try:
                rec.append(ml_get(f"/post-purchase/v1/claims/{cid_ml}", token))
            except MLErro as e:
                print(f"   aviso: reclamacao {cid_ml}: {e}")
        print(f"   reclamacoes: {len(ids_abertas)} aberta(s), {len(sumiram)} para conferir")

        ids_rec = [f"rec:{x.get('id')}" for x in rec]
        antes_r = existentes(ids_rec)
        ja_r = msgs_existentes(ids_rec)
        for cl in rec:
            claim = str(cl.get("id"))
            cid = f"rec:{claim}"
            ant = antes_r.get(cid)
            msgs = msgs_da_reclamacao(token, claim)
            acoes = acoes_do_vendedor(cl, sid)
            com_prazo = [a for a in acoes if a["prazo"]]
            obrig = [a for a in com_prazo if a["obrigatoria"]] or com_prazo
            responder = any(a["acao"] in ("send_message_to_complainant", "send_message_to_mediator") and a["obrigatoria"] for a in acoes)
            encerrada = str(cl.get("status")) == "closed"
            linha = {"id": cid, "tipo": "reclamacao", "seller_id": sid, "claim_id": claim,
                     "order_id": str(cl.get("resource_id") or ""), "status_ml": cl.get("status"),
                     "etapa": cl.get("stage"), "motivo": cl.get("reason_id"), "acoes": acoes,
                     "prazo_em": min(a["prazo"] for a in obrig) if obrig and not encerrada else None,
                     "prazo_tipo": "ml" if obrig and not encerrada else None}
            est = calcular_status(msgs, ant, encerrada=encerrada, acao_mensagem=responder)
            est.pop("_primeira_sem_resposta")
            linha.update(est)
            if not ant or not ant.get("motivo_nome"):
                linha["motivo_nome"] = nome_motivo(token, cl.get("reason_id"))
            if not ant or not ant.get("item_titulo"):
                linha.update({k: v for k, v in dados_do_pedido(token, str(cl.get("resource_id") or "")).items() if k != "pack_id"})
                novas_conv_msg.append(linha)
            conversas.append(linha)
            ids_ja = {x["id"] for x in ja_r.get(cid, [])}
            txt_loja = {normal(x["texto"]) for x in ja_r.get(cid, []) if x["de"] == "loja"}
            for m in msgs:
                if m["de"] == "loja" and m["id"] not in ids_ja and normal(m["texto"]) in txt_loja:
                    continue
                mensagens.append({"id": m["id"], "conversa_id": cid, "de": m["de"], "texto": m["texto"],
                                  "anexos": m["anexos"], "criada_em": m["criada_em"].isoformat() if m["criada_em"] else None,
                                  "moderacao": m["moderacao"]})
                if (m["id"] not in ids_ja and m["de"] in ("cliente", "mediador") and m["criada_em"]
                        and m["criada_em"] > AGORA - AVISO_JANELA):
                    avisar.append({"conversa": cid, "tipo": "reclamacao", "seller_id": sid, "texto": m["texto"] or "(anexo)"})
            criada = iso(cl.get("date_created"))
            if not ant and not encerrada and not msgs and criada and criada > AGORA - AVISO_JANELA:
                avisar.append({"conversa": cid, "tipo": "reclamacao", "seller_id": sid, "texto": "Reclamação nova: " + (linha.get("motivo_nome") or "")})
            time.sleep(0.15)

        # fotos dos produtos das conversas novas
        th = fotos(token, {l.get("item_id") for l in novas_conv_msg if l.get("item_id")})
        for l in novas_conv_msg:
            if l.get("item_id") in th:
                l["item_thumb"] = th[l["item_id"]]
        fila_ok += [f["id"] for f in minha_fila]
        # grava conta por conta: se o GitHub cortar no meio, o que ja' foi lido fica salvo
        tot["conversas"] += len(conversas)
        tot["abertas"] += sum(1 for x in conversas if x["status"] == "aberta")
        tot["mensagens"] += len(mensagens)
        if not DRY_RUN:
            gravar(conversas, mensagens)
            print(f"   gravado: {len(conversas)} conversa(s), {len(mensagens)} mensagem(ns)")
        conversas, mensagens = [], []

    # avisos de fila sem conta conhecida tambem saem da fila
    fila_ok += [f["id"] for f in fila if str(f.get("seller_id") or "") not in SELLERS]

    print("\n" + "=" * 60)
    print(f"  conversas lidas ....... {tot['conversas']}")
    print(f"     abertas ............ {tot['abertas']}")
    print(f"  mensagens ............. {tot['mensagens']}")
    print(f"  tempo ................. {(time.time() - INICIO) / 60:.1f} min")
    print(f"  vao gerar aviso ....... {len(avisar)}")
    if DRY_RUN:
        print("\n[DRY RUN] nada gravado.")
        return
    for i in range(0, len(fila_ok), 150):
        ids = ",".join(str(x) for x in fila_ok[i:i + 150])
        sb_req("PATCH", f"atend_fila?id=in.({ids})", data=json.dumps({"processado": True}))
    avisar_celular(avisar)
    print(f"\n✅ concluido em {datetime.now(timezone.utc):%H:%M:%S} UTC")


def gravar(conversas, mensagens):
    # conversas primeiro (as mensagens apontam para elas)
    upsert("atend_conversas", [{k: v for k, v in c.items() if not k.startswith("_")} for c in conversas])
    upsert("atend_mensagens", mensagens)


def avisar_celular(itens):
    if not itens:
        return
    seg = (os.environ.get("PERGUNTAS_PUSH_SECRET") or "").strip()
    try:
        r = requests.post(f"{SUPABASE_URL}/functions/v1/perguntas_push",
                          headers={"Authorization": f"Bearer {SUPABASE_KEY}", "x-app-secret": seg,
                                   "Content-Type": "application/json"},
                          data=json.dumps({"tipo": "atend", "itens": itens[:20]}), timeout=30)
        print(f"   aviso no celular: {r.text[:160]}")
    except Exception as e:
        print(f"   aviso no celular falhou ({e})")


if __name__ == "__main__":
    main()
