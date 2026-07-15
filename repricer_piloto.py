"""
PILOTO — roda o repricer completo em UMA conta (padrão: MG 3244206480), com trilhos.
Lê a análise da sonda e, nos casos competitivos, cria desconto na Central de Promoções.
Nunca mexe no preço do anúncio. Padrão = SIMULAÇÃO. Só escreve com CONFIRMA=SIM e ATIVO=SIM.

Trilhos de segurança:
  - conta travada (SELLER_ID, padrão MG)          - piso do grupo (18% padrão) já vem da sonda
  - teto de alterações por rodada (MAX_ALTERACOES) - auto-exclusão das suas contas (na sonda)
  - guarda anti-salto (MAX_DROP_PCT)               - botão de pânico (ATIVO=NAO)
  - pula item que já tem PRICE_DISCOUNT/DEAL        - log de auditoria (repricer_log) + Telegram

Ações:
  BAIXAR (perdendo/não é o mais barato) -> cria desconto no 'alvo' da sonda (price_to_win no
     catálogo; menor concorrente − R$5 fora do catálogo; sempre travado no piso).  [AGE]
  SUBIR / barato demais -> só REGISTRA e avisa (subir exige mexer no anúncio ou reduzir um
     desconto seu; a gente liga a ação depois de provar o baixar).                  [SINALIZA]
"""
import os
import time
from datetime import date, timedelta
import requests
import repricer_sugestoes as rec
import repricer_competitivo as sonda
import repricer_aplicar as apl
from ml_auth import obter_access

SELLER_ID = (os.environ.get("SELLER_ID") or "3244206480").strip()   # MG por padrão
MAX_ALTERACOES = int(os.environ.get("MAX_ALTERACOES", "3"))
MAX_DROP_PCT = float(os.environ.get("MAX_DROP_PCT", "20"))
DIAS = max(1, min(int(os.environ.get("DIAS", "14")), 14))
MAX_ITENS = int(os.environ.get("MAX_ITENS", "0"))                    # 0 = todos
CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
ATIVO = (os.environ.get("ATIVO") or "SIM").strip().upper() == "SIM"  # botão de pânico

ACOES_DESCONTO = {"descontar", "descontar_ean", "descontar_piso"}


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


def _datas():
    hoje = date.today()
    fim = hoje + timedelta(days=DIAS - 1)
    return f"{hoje.isoformat()}T00:00:00", f"{fim.isoformat()}T00:00:00"


def tem_promo_bloqueante(item_id, access):
    """Já tem PRICE_DISCOUNT nosso ou está num DEAL? então não mexe agora."""
    for o in apl.promos_do_item(item_id, access):
        if isinstance(o, dict) and apl.eh_ativa(o) and (o.get("type") or "") in ("PRICE_DISCOUNT", "DEAL"):
            return True
    return False


def criar_desconto(item_id, deal_price, access):
    start, finish = _datas()
    body = {"deal_price": round(float(deal_price), 2), "start_date": start,
            "finish_date": finish, "promotion_type": "PRICE_DISCOUNT"}
    return apl.req("POST", f"/seller-promotions/items/{item_id}?app_version=v2", access, body=body)


def base_log(sid, a):
    return {"seller_id": str(sid), "item_id": a.get("item_id"), "sku": a.get("sku"),
            "titulo": a.get("titulo"), "preco_cheio": a.get("preco_cheio"),
            "margem_alvo": a.get("margem_alvo"), "margem_cheio": a.get("margem_cheio"),
            "conc_min": a.get("conc_min") or a.get("segundo"), "price_to_win": a.get("price_to_win"),
            "piso": a.get("piso"), "grupo": a.get("grupo"), "motivo": a.get("detalhe")}


def main():
    if not ATIVO:
        print("⛔ ATIVO=NAO (botão de pânico) — nada será escrito. Saindo.", flush=True)
        telegram("⛔ Piloto: botão de pânico ligado (ATIVO=NAO). Nada foi feito.")
        return

    rec.preload()
    sonda.carregar_match()
    sonda.carregar_controle()   # liga/desliga, regra individual e PMA por anúncio (painel)

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
    print(f"===== PILOTO | conta {sid} | {modo} | teto {MAX_ALTERACOES}/rodada | "
          f"anti-salto {MAX_DROP_PCT:.0f}% | UNDERCUT R${sonda.UNDERCUT:.0f} =====", flush=True)

    todos, _ = rec.todos_ativos(sid, access)
    if MAX_ITENS:
        todos = todos[:MAX_ITENS]

    # 1) análise (leitura)
    analises = []
    for iid in todos:
        try:
            a = sonda.analisar(iid, access, sid)
            if a:
                analises.append(a)
        except Exception as e:
            print(f"   (analisar {iid} falhou: {e})", flush=True)

    # 2) SINALIZA barato-demais (não age; registra)
    baratos = [a for a in analises if a.get("acao") in ("subir_margem", "ja_competitivo")]
    for a in baratos:
        print(f"💰 BARATO DEMAIS {a['item_id']} {str(a.get('titulo'))[:28]} -> {a.get('detalhe')}", flush=True)
        logar({**base_log(sid, a), "acao": a.get("acao"), "aplicado": False, "modo": "insight",
               "deal_price": a.get("alvo_subir")})

    # 3) BAIXAR: candidatos a desconto
    cands = []
    for a in analises:
        if a.get("acao") not in ACOES_DESCONTO:
            continue
        alvo, p0 = a.get("alvo"), a.get("preco_cheio")
        if not alvo or not p0 or alvo <= 0:
            continue
        a["_desc"] = (1 - alvo / p0) * 100
        cands.append(a)
    cands.sort(key=lambda x: -x["_desc"])   # maior desconto (mais atrás) primeiro

    aplicados = 0
    for a in cands:
        iid, alvo, p0, desc = a["item_id"], a["alvo"], a["preco_cheio"], a["_desc"]
        row = {**base_log(sid, a), "acao": a.get("acao"), "deal_price": alvo,
               "desconto_pct": round(desc, 1)}

        if desc > MAX_DROP_PCT:
            print(f"⏭️  SALTO {desc:.1f}%>{MAX_DROP_PCT:.0f}% {iid} — pulado p/ revisão", flush=True)
            logar({**row, "acao": "pulado_salto", "aplicado": False, "modo": modo.lower()})
            continue
        if desc < 5:   # ML rejeita PRICE_DISCOUNT < 5%
            logar({**row, "acao": "pulado_menor5", "aplicado": False, "modo": modo.lower()})
            continue
        if aplicados >= MAX_ALTERACOES:
            logar({**row, "acao": "fila_teto", "aplicado": False, "modo": modo.lower()})
            continue
        if tem_promo_bloqueante(iid, access):
            logar({**row, "acao": "pulado_ja_promo", "aplicado": False, "modo": modo.lower()})
            continue

        if not CONFIRMA:
            print(f"• SIMULA {iid} {str(a.get('titulo'))[:26]} -> R${alvo:.2f} "
                  f"({desc:.1f}% off, margem {a.get('margem_alvo')}%)", flush=True)
            logar({**row, "aplicado": False, "modo": "simulacao"})
            aplicados += 1
            continue

        st, resp = criar_desconto(iid, alvo, access)
        ok = 200 <= st < 300
        print(f"{'✅' if ok else '⛔'} APLICA {iid} {str(a.get('titulo'))[:26]} -> R${alvo:.2f} "
              f"({desc:.1f}% off) HTTP {st}", flush=True)
        logar({**row, "aplicado": ok, "modo": "live", "http_status": st})
        if ok:
            aplicados += 1
        time.sleep(0.4)

    resumo = (f"Piloto {modo} · conta {sid}: {aplicados} desconto(s) "
              f"{'aplicados' if CONFIRMA else 'simulados'} · "
              f"{len(baratos)} barato-demais · {len(cands)} candidatos.")
    print(f"\n=== {resumo} ===", flush=True)
    telegram("🤖 " + resumo)
    if not CONFIRMA:
        print("SIMULAÇÃO: nada escrito. CONFIRMA=SIM (e ATIVO=SIM) pra aplicar.", flush=True)


if __name__ == "__main__":
    main()
