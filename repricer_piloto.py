"""
PILOTO — repricer completo em UMA conta (padrão MG 3244206480), com trilhos e reconciliador.
Lê a sonda + as vendas recentes + o controle por anúncio, e ACERTA o estado de cada item:
  perdendo + PARADO      -> cria desconto (Central de Promoções) no alvo competitivo (piso/PMA)
  perdendo + VENDENDO    -> não desconta; se já tiver desconto nosso, REMOVE (protege margem)
  ganhando / barato demais -> sem desconto; remove o nosso se houver
Nunca mexe no preço do anúncio. Padrão = SIMULAÇÃO. Só escreve com CONFIRMA=SIM e ATIVO=SIM.

Trilhos: conta travada · teto de CRIAÇÕES/rodada · anti-salto · piso do grupo (+PMA) · gate de
vendas (2 dias) · auto-exclusão das suas contas · botão de pânico · log de auditoria · Telegram.
As REMOÇÕES (que protegem margem) não contam no teto.
"""
import os
import time
from datetime import date, timedelta
import requests
import repricer_sugestoes as rec
import repricer_competitivo as sonda
from ml_auth import obter_access

# --- API da Central de Promoções (autossuficiente: NÃO depende do repricer_aplicar) ---
STATUS_ATIVA = {"started", "active", "in_progress", "ongoing", "pending"}


def req(method, path, access, body=None, tent=2):
    h = {"Authorization": "Bearer " + access, "Content-Type": "application/json"}
    r = None
    for i in range(tent):
        r = requests.request(method, rec.API + path, headers=h, json=body, timeout=25)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1)); continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text if r is not None else None)


def eh_ativa(o):
    if (o.get("status") or "").lower() in STATUS_ATIVA:
        return True
    return str(o.get("ref_id") or "").upper().startswith("OFFER-")


def promos_do_item(item_id, access):
    st, d = req("GET", f"/seller-promotions/items/{item_id}?app_version=v2", access)
    return d if isinstance(d, list) else []

SELLER_ID = (os.environ.get("SELLER_ID") or "3244206480").strip()   # MG por padrão
MAX_ALTERACOES = int(os.environ.get("MAX_ALTERACOES", "3"))          # teto de CRIAÇÕES por rodada
MAX_DROP_PCT = float(os.environ.get("MAX_DROP_PCT", "20"))
DIAS = max(1, min(int(os.environ.get("DIAS", "14")), 14))            # duração do desconto (rede de segurança)
VENDAS_DIAS = int(os.environ.get("VENDAS_DIAS", "2"))               # janela do gate de vendas
VENDAS_MIN = int(os.environ.get("VENDAS_MIN", "1"))                 # vendeu >= isso -> não desconta
MAX_ITENS = int(os.environ.get("MAX_ITENS", "0"))                    # 0 = todos
CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
ATIVO = (os.environ.get("ATIVO") or "SIM").strip().upper() == "SIM"  # botão de pânico

ACOES_DESCONTO = {"descontar", "descontar_ean", "descontar_piso"}
REMOVER_OK = {"subir_margem", "ja_competitivo", "manter_ganhando"}   # confiante que não precisa desconto
_CANCEL = ("cancel", "estorn", "refund", "devolv")                   # status que NÃO conta como venda


def telegram(msg):
    tok = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=15)
    except Exception:
        pass


def logar(row):
    try:
        rec.sb.table("repricer_log").insert(row).execute()
    except Exception as e:
        print(f"   (log falhou: {e})", flush=True)


def base_log(sid, a):
    return {"seller_id": str(sid), "item_id": a.get("item_id"), "sku": a.get("sku"),
            "titulo": a.get("titulo"), "preco_cheio": a.get("preco_cheio"),
            "margem_alvo": a.get("margem_alvo"), "margem_cheio": a.get("margem_cheio"),
            "conc_min": a.get("conc_min") or a.get("segundo"), "price_to_win": a.get("price_to_win"),
            "piso": a.get("piso"), "grupo": a.get("grupo"), "motivo": a.get("detalhe")}


def _datas():
    hoje = date.today()
    fim = hoje + timedelta(days=DIAS - 1)
    return f"{hoje.isoformat()}T00:00:00", f"{fim.isoformat()}T00:00:00"


def carregar_vendas(sid, dias):
    """Unidades vendidas (NÃO canceladas) nos últimos `dias`, por item_id e por sku."""
    corte = (date.today() - timedelta(days=dias)).isoformat()
    por_item, por_sku, ini = {}, {}, 0
    while True:
        try:
            lote = (rec.sb.table("vendas")
                    .select("item_id,sku,quantidade,status,data_aprovacao")
                    .eq("seller_id", str(sid)).gte("data_aprovacao", corte)
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: não li vendas: {e})", flush=True)
            return por_item, por_sku
        for r in lote:
            if any(x in str(r.get("status") or "").lower() for x in _CANCEL):
                continue
            try:
                q = int(r.get("quantidade") or 0)
            except (TypeError, ValueError):
                q = 0
            if r.get("item_id"):
                por_item[r["item_id"]] = por_item.get(r["item_id"], 0) + q
            if r.get("sku"):
                por_sku[r["sku"]] = por_sku.get(r["sku"], 0) + q
        if len(lote) < 1000:
            break
        ini += 1000
    return por_item, por_sku


def unidades(a, por_item, por_sku):
    u = por_item.get(a.get("item_id"), 0)
    if a.get("sku"):
        u = max(u, por_sku.get(a["sku"], 0))
    return u


def estado_promo(item_id, access):
    """(tem_pd, tem_deal): tem PRICE_DISCOUNT nosso ativo? está num DEAL?"""
    tem_pd = tem_deal = False
    for o in promos_do_item(item_id, access):
        if isinstance(o, dict) and eh_ativa(o):
            t = (o.get("type") or "")
            if t == "PRICE_DISCOUNT":
                tem_pd = True
            elif t == "DEAL":
                tem_deal = True
    return tem_pd, tem_deal


def criar_desconto(item_id, deal_price, access):
    start, finish = _datas()
    body = {"deal_price": round(float(deal_price), 2), "start_date": start,
            "finish_date": finish, "promotion_type": "PRICE_DISCOUNT"}
    return req("POST", f"/seller-promotions/items/{item_id}?app_version=v2", access, body=body)


def remover_desconto(item_id, access):
    return req("DELETE",
               f"/seller-promotions/items/{item_id}?promotion_type=PRICE_DISCOUNT&app_version=v2", access)


def main():
    if not ATIVO:
        print("⛔ ATIVO=NAO (botão de pânico) — nada será escrito. Saindo.", flush=True)
        telegram("⛔ Piloto: botão de pânico ligado. Nada foi feito.")
        return

    rec.preload()
    sonda.carregar_match()
    sonda.carregar_controle()

    access = sid = None
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if str(s) == str(SELLER_ID):
            access, sid = a, s
            break
    if not access:
        print(f"não autentiquei a conta {SELLER_ID}.", flush=True)
        return

    modo = "AO VIVO" if CONFIRMA else "SIMULAÇÃO"
    print(f"===== PILOTO | conta {sid} | {modo} | teto {MAX_ALTERACOES} criações/rodada | "
          f"anti-salto {MAX_DROP_PCT:.0f}% | gate vendas {VENDAS_MIN}u/{VENDAS_DIAS}d | "
          f"UNDERCUT R${sonda.UNDERCUT:.0f} =====", flush=True)

    todos, _ = rec.todos_ativos(sid, access)
    if MAX_ITENS:
        todos = todos[:MAX_ITENS]

    por_item, por_sku = carregar_vendas(sid, VENDAS_DIAS)

    analises = []
    for iid in todos:
        try:
            a = sonda.analisar(iid, access, sid)
            if a:
                analises.append(a)
        except Exception as e:
            print(f"   (analisar {iid} falhou: {e})", flush=True)

    cont = {"criado": 0, "removido": 0, "vende": 0, "barato": 0}
    criados = 0
    for a in analises:
        acao = a.get("acao")
        if acao == "desligado":
            continue
        iid = a.get("item_id")
        tit = str(a.get("titulo"))[:26]
        u = unidades(a, por_item, por_sku)

        quer_desconto = acao in ACOES_DESCONTO
        remover_motivo = None

        if quer_desconto and u >= VENDAS_MIN:            # gate de vendas: gira -> não desconta
            quer_desconto = False
            remover_motivo = f"vende {u}u/{VENDAS_DIAS}d"
            cont["vende"] += 1
            print(f"🛒 VENDE {iid} {tit} ({u}u/{VENDAS_DIAS}d) -> não desconta", flush=True)
            logar({**base_log(sid, a), "acao": "vende_nao_mexe", "aplicado": False, "modo": "insight"})
        elif acao in REMOVER_OK:
            remover_motivo = acao
            if acao in ("subir_margem", "ja_competitivo"):
                cont["barato"] += 1
                print(f"💰 BARATO DEMAIS {iid} {tit} -> {a.get('detalhe')}", flush=True)
                logar({**base_log(sid, a), "acao": acao, "aplicado": False, "modo": "insight",
                       "deal_price": a.get("alvo_subir")})

        if quer_desconto:
            alvo, p0 = a.get("alvo"), a.get("preco_cheio")
            if not alvo or not p0 or alvo <= 0:
                continue
            desc = (1 - alvo / p0) * 100
            row = {**base_log(sid, a), "acao": acao, "deal_price": alvo, "desconto_pct": round(desc, 1)}
            tem_pd, tem_deal = estado_promo(iid, access)
            if tem_pd:
                continue                                  # já descontado; ajuste fino fica pra depois
            if tem_deal:
                logar({**row, "acao": "pulado_deal", "aplicado": False, "modo": modo.lower()}); continue
            if desc > MAX_DROP_PCT:
                print(f"⏭️  SALTO {desc:.1f}%>{MAX_DROP_PCT:.0f}% {iid} {tit} — pulado", flush=True)
                logar({**row, "acao": "pulado_salto", "aplicado": False, "modo": modo.lower()}); continue
            if desc < 5:
                logar({**row, "acao": "pulado_menor5", "aplicado": False, "modo": modo.lower()}); continue
            if criados >= MAX_ALTERACOES:
                logar({**row, "acao": "fila_teto", "aplicado": False, "modo": modo.lower()}); continue
            if not CONFIRMA:
                print(f"• SIMULA cria {iid} {tit} -> R${alvo:.2f} ({desc:.1f}% off, "
                      f"margem {a.get('margem_alvo')}%)", flush=True)
                logar({**row, "aplicado": False, "modo": "simulacao"}); criados += 1; cont["criado"] += 1; continue
            st, resp = criar_desconto(iid, alvo, access)
            ok = 200 <= st < 300
            print(f"{'✅' if ok else '⛔'} CRIA {iid} {tit} -> R${alvo:.2f} ({desc:.1f}% off) HTTP {st}", flush=True)
            logar({**row, "aplicado": ok, "modo": "live", "http_status": st})
            if ok:
                criados += 1; cont["criado"] += 1
            time.sleep(0.4)

        elif remover_motivo:
            tem_pd, _ = estado_promo(iid, access)
            if not tem_pd:
                continue
            row = {**base_log(sid, a), "acao": "remover_desconto", "motivo": remover_motivo}
            if not CONFIRMA:
                print(f"• SIMULA remove desconto {iid} {tit} ({remover_motivo})", flush=True)
                logar({**row, "aplicado": False, "modo": "simulacao"}); cont["removido"] += 1; continue
            st, resp = remover_desconto(iid, access)
            ok = 200 <= st < 300
            print(f"{'✅' if ok else '⛔'} REMOVE desconto {iid} {tit} ({remover_motivo}) HTTP {st}", flush=True)
            logar({**row, "aplicado": ok, "modo": "live", "http_status": st})
            if ok:
                cont["removido"] += 1
            time.sleep(0.3)

    resumo = (f"Piloto {modo} · MG {sid}: {cont['criado']} desconto(s) "
              f"{'criados' if CONFIRMA else 'a criar'}, {cont['removido']} remoção(ões), "
              f"{cont['vende']} segurados por venda, {cont['barato']} barato-demais.")
    print(f"\n=== {resumo} ===", flush=True)
    telegram("🤖 " + resumo)
    if not CONFIRMA:
        print("SIMULAÇÃO: nada escrito. CONFIRMA=SIM (e ATIVO=SIM) pra valer.", flush=True)


if __name__ == "__main__":
    main()
