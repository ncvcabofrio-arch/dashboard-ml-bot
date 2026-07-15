"""
RELATÓRIO — roda no GitHub Actions LOGO DEPOIS do piloto, na mesma conta.
Não encosta na lógica de decisão. Ele só:
  1) lê o que o robô ACABOU de fazer (repricer_log, últimos X min) — "esta passada"
  2) calcula a margem DIÁRIA de verdade (vendas × custo) dos últimos N dias
  3) EMPURRA tudo pra uma planilha Google via webhook (Apps Script)

Por que assim: o acesso ao banco fica SÓ aqui no GitHub (onde as chaves já moram).
A planilha é a ponte — a análise (Claude) lê a planilha, não o banco.
Sem Telegram. Se WEBHOOK_URL não estiver setado, só imprime (não quebra a rodada).

Margem = total − comissão − frete − (custo × quantidade), igual à base validada.
Só NÃO conta venda cancelada (paid e partially_refunded contam).
"""
import os
import json
from datetime import date, datetime, timedelta, timezone
import requests
import repricer_sugestoes as rec

WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or "").strip()
SELLER_ID = (os.environ.get("SELLER_ID") or "3244206480").strip()
JANELA_MIN = int(os.environ.get("RELATORIO_JANELA_MIN", "65"))      # "esta passada" = últimos X min (só live)
MARGEM_DIAS = int(os.environ.get("RELATORIO_MARGEM_DIAS", "20"))    # janela do painel de margem
EMAIL_RELATORIO = (os.environ.get("EMAIL_RELATORIO") or "").strip()  # p/ quem o Apps Script manda o aviso
BASE_MARGEM = (os.environ.get("BASE_MARGEM") or "").strip()          # margem-base p/ comparar (ex "14,3")
NOME_CONTA = (os.environ.get("NOME_CONTA") or "").strip() or f"conta {SELLER_ID}"
MODO = "live" if (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM" else "simulacao"
_CANCEL = ("cancel",)   # só cancelada não conta


def _post(payload):
    if not WEBHOOK_URL:
        print("(WEBHOOK_URL não setado — não enviei; prévia abaixo)", flush=True)
        print(json.dumps(payload, ensure_ascii=False)[:1500], flush=True)
        return
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
        print(f"webhook aba={payload.get('aba')} -> HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"(webhook aba={payload.get('aba')} falhou: {e})", flush=True)


def _num(x):
    try:
        return float(x if x is not None else 0)
    except (TypeError, ValueError):
        return 0.0


def custos_map():
    """sku -> custo (numa passada, paginado)."""
    m, ini = {}, 0
    while True:
        try:
            lote = (rec.sb.table("produtos").select("sku,custo")
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: não li produtos: {e})", flush=True)
            break
        for r in lote:
            if r.get("sku") is not None:
                m[str(r["sku"])] = _num(r.get("custo"))
        if len(lote) < 1000:
            break
        ini += 1000
    return m


def carregar_vendas_full(sid, dias):
    corte = (date.today() - timedelta(days=dias)).isoformat()
    linhas, ini = [], 0
    while True:
        try:
            lote = (rec.sb.table("vendas")
                    .select("item_id,sku,quantidade,total,comissao,frete,status,data_aprovacao")
                    .eq("seller_id", str(sid)).gte("data_aprovacao", corte)
                    .range(ini, ini + 999).execute().data) or []
        except Exception as e:
            print(f"(aviso: não li vendas: {e})", flush=True)
            break
        linhas += lote
        if len(lote) < 1000:
            break
        ini += 1000
    return linhas


def margem_diaria(sid, dias):
    """[[dia, pedidos, unid, receita, custo, margem_rs, margem_pct], ...] por dia."""
    custos = custos_map()
    vendas = carregar_vendas_full(sid, dias)
    porq = {}
    for v in vendas:
        if any(x in str(v.get("status") or "").lower() for x in _CANCEL):
            continue
        da = v.get("data_aprovacao")
        if not da:
            continue
        dia = str(da)[:10]
        qtd = _num(v.get("quantidade")) or 1
        total = _num(v.get("total"))
        margem = total - _num(v.get("comissao")) - _num(v.get("frete")) \
            - custos.get(str(v.get("sku")), 0.0) * qtd
        d = porq.setdefault(dia, {"ped": 0, "un": 0.0, "rec": 0.0, "cus": 0.0, "mar": 0.0})
        d["ped"] += 1
        d["un"] += qtd
        d["rec"] += total
        d["cus"] += custos.get(str(v.get("sku")), 0.0) * qtd
        d["mar"] += margem
    linhas = []
    for dia in sorted(porq):
        d = porq[dia]
        pct = round(100 * d["mar"] / d["rec"], 1) if d["rec"] else None
        linhas.append([dia, d["ped"], round(d["un"]), round(d["rec"], 2),
                       round(d["cus"], 2), round(d["mar"], 2), pct])
    return linhas


WRITE_ACOES = {"descontar", "descontar_ean", "descontar_piso", "entrar_campanha", "remover_desconto"}


def mudancas_da_passada(sid, janela_min):
    """Toda TENTATIVA de escrita nos últimos `janela_min` min — sucesso E falha.
    Retorna (linhas, n_ok, n_erro). Assim dá pra ver se o proposto foi mesmo feito."""
    corte = (datetime.now(timezone.utc) - timedelta(minutes=janela_min)).isoformat()
    try:
        # SÓ modo=live: simulação (http vazio, aplicado=false) NÃO é falha e não pode entrar aqui.
        rows = (rec.sb.table("repricer_log").select("*")
                .eq("seller_id", str(sid)).eq("modo", "live").gte("ts", corte)
                .order("ts", desc=False).execute().data) or []
    except Exception as e:
        print(f"(aviso: não li repricer_log: {e})", flush=True)
        return [], 0, 0
    linhas, n_ok, n_erro = [], 0, 0
    for r in rows:
        if r.get("acao") not in WRITE_ACOES:      # pula insight/pulado/fila (não tentou escrever)
            continue
        ok = bool(r.get("aplicado"))
        n_ok += int(ok)
        n_erro += int(not ok)
        linhas.append([str(r.get("ts"))[:19], r.get("item_id"), (r.get("titulo") or "")[:40],
                       r.get("acao"), r.get("deal_price"), r.get("margem_alvo"),
                       r.get("motivo"), r.get("http_status"), "ok" if ok else "ERRO"])
    return linhas, n_ok, n_erro


def nota_curta(hora, linhas, n_ok, n_erro, m_hoje):
    """A mensagenzinha comentada de cada passada (o Apps Script manda por email)."""
    if linhas:
        det = "; ".join(
            f"{(x[2] or x[1] or '')[:22]} {x[3]}" + (" ❌" if x[8] == "ERRO" else "")
            for x in linhas[:6])
        if len(linhas) > 6:
            det += f" (+{len(linhas) - 6})"
        txt = f"🤖 {hora} {NOME_CONTA}: aplicou {n_ok} mudança(s)"
        if n_erro:
            txt += f" — ⚠️ {n_erro} FALHOU/FALHARAM"
        txt += f" — {det}."
    else:
        txt = f"🤖 {hora} {NOME_CONTA}: sem mudanças nesta passada."
    if m_hoje and m_hoje[6] is not None:
        base = f" (base {BASE_MARGEM}%)" if BASE_MARGEM else ""
        txt += f" Margem do dia {m_hoje[6]}%{base}, {m_hoje[1]} pedido(s), receita R${m_hoje[3]:.0f}."
    return txt


def main():
    agora = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    hora = datetime.now(timezone.utc).astimezone().strftime("%Hh")
    linhas, n_ok, n_erro = mudancas_da_passada(SELLER_ID, JANELA_MIN)
    _post({"aba": "Mudancas", "quando": agora, "conta": SELLER_ID, "rows": linhas})

    dias = margem_diaria(SELLER_ID, MARGEM_DIAS)
    _post({"aba": "MargemDiaria", "quando": agora, "conta": SELLER_ID, "rows": dias})

    hoje = date.today().isoformat()
    m_hoje = next((l for l in dias if l[0] == hoje), None)
    nota = nota_curta(hora, linhas, n_ok, n_erro, m_hoje)
    # o Apps Script grava a linha E manda o PULSO por email SÓ quando NÃO houve nenhuma
    # tentativa de mudança. Passada com mudança (ou falha!) fica pro comentário narrado
    # da Claude de hora em hora, pra não chegar email em dobro e pra ela sinalizar falhas.
    _post({"aba": "Passadas", "quando": agora, "conta": SELLER_ID,
           "nota": nota, "para": EMAIL_RELATORIO, "tem_mudanca": len(linhas) > 0,
           "modo": MODO,   # Apps Script só manda o pulso por email em modo 'live'
           "assunto": f"Repricer {hora} — {NOME_CONTA}",
           "rows": [[agora, SELLER_ID, f"{n_ok} ok / {n_erro} erro", nota]]})
    print(f"Relatório: {n_ok} ok, {n_erro} erro, {len(dias)} dia(s) de margem. {nota}", flush=True)


if __name__ == "__main__":
    main()
