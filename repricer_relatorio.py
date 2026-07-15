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
JANELA_MIN = int(os.environ.get("RELATORIO_JANELA_MIN", "15"))      # só ESTA passada (piloto+relatório <5 min)
MARGEM_DIAS = int(os.environ.get("RELATORIO_MARGEM_DIAS", "20"))    # janela do painel de margem
EMAIL_RELATORIO = (os.environ.get("EMAIL_RELATORIO") or "").strip()  # p/ quem o Apps Script manda o aviso
BASE_MARGEM = (os.environ.get("BASE_MARGEM") or "").strip()          # margem-base p/ comparar (ex "14,3")
NOME_CONTA = (os.environ.get("NOME_CONTA") or "").strip() or f"conta {SELLER_ID}"
MODO = "live" if (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM" else "simulacao"
BR = timezone(timedelta(hours=-3))   # horário de Brasília (o Brasil não usa mais horário de verão)
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
INFO_ACOES = {"manter_desconto"}   # não é escrita; é a trava anti-gangorra segurando


def mudancas_da_passada(sid, janela_min):
    """Escritas dos últimos `janela_min` min (sucesso E falha) + quantos foram mantidos.
    Retorna (linhas, n_ok, n_erro, n_mantido)."""
    corte = (datetime.now(timezone.utc) - timedelta(minutes=janela_min)).isoformat()
    try:
        # SÓ modo=live: simulação (http vazio, aplicado=false) NÃO é falha e não pode entrar aqui.
        rows = (rec.sb.table("repricer_log").select("*")
                .eq("seller_id", str(sid)).eq("modo", "live").gte("ts", corte)
                .order("ts", desc=False).execute().data) or []
    except Exception as e:
        print(f"(aviso: não li repricer_log: {e})", flush=True)
        return [], 0, 0, 0
    linhas, n_ok, n_erro, n_mantido = [], 0, 0, 0
    for r in rows:
        acao = r.get("acao")
        if acao in INFO_ACOES:
            n_mantido += 1
            continue
        if acao not in WRITE_ACOES:               # pula insight/pulado/fila (não tentou escrever)
            continue
        ok = bool(r.get("aplicado"))
        n_ok += int(ok)
        n_erro += int(not ok)
        linhas.append([str(r.get("ts"))[:19], r.get("item_id"), (r.get("titulo") or "")[:40],
                       r.get("acao"), r.get("deal_price"), r.get("margem_alvo"),
                       r.get("motivo"), r.get("http_status"), "ok" if ok else "ERRO"])
    return linhas, n_ok, n_erro, n_mantido


def _rs(v):
    """Formata dinheiro no jeito BR: 1234.5 -> R$1.234,50."""
    try:
        return "R$" + f"{float(v):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    except (TypeError, ValueError):
        return ("R$" + str(v)) if v not in (None, "") else "—"


def _pct(v):
    """18.0 -> '18%', 16.1 -> '16,1%' (jeito BR, sem casa decimal à toa)."""
    try:
        f = float(v)
        s = str(int(f)) if f == int(f) else f"{f:.1f}".replace(".", ",")
        return s + "%"
    except (TypeError, ValueError):
        return str(v) + "%"


_LABEL = {"descontar": "criou desconto", "descontar_ean": "criou desconto",
          "descontar_piso": "criou desconto (piso)", "entrar_campanha": "trocou p/ campanha do ML",
          "remover_desconto": "removeu desconto"}
_PORQUE = {"descontar": "estava perdendo e parado", "descontar_ean": "estava perdendo e parado",
           "descontar_piso": "perdendo; desceu até o piso",
           "entrar_campanha": "campanha paga mais margem e segue competitiva",
           "remover_desconto": "voltou a ganhar sem precisar do desconto"}


def _esc(s):
    """Escapa caracteres de HTML nos títulos dos produtos (evita quebrar o email)."""
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def montar_email(hora, linhas, n_ok, n_erro, n_mantido, m_hoje):
    """Email em HTML: margem no topo, item por item, com espaçamento e hierarquia."""
    P = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;'
         'line-height:1.5;max-width:600px">']
    P.append(f'<div style="font-size:12px;color:#8a8a8a;text-transform:uppercase;'
             f'letter-spacing:.6px">Repricer &middot; {_esc(NOME_CONTA)} &middot; {hora}</div>')

    if m_hoje and m_hoje[6] is not None:
        seta, cor = "", "#333"
        try:
            if BASE_MARGEM:
                acima = float(str(m_hoje[6])) >= float(BASE_MARGEM.replace(",", "."))
                seta = " &#9650;" if acima else " &#9660;"   # ▲ / ▼
                cor = "#16a34a" if acima else "#dc2626"
        except (TypeError, ValueError):
            pass
        base = (f' <span style="font-size:13px;color:#8a8a8a;font-weight:400">(base {BASE_MARGEM}%)</span>'
                if BASE_MARGEM else "")
        P.append(f'<div style="font-size:22px;font-weight:700;margin:8px 0 2px">'
                 f'<span style="color:{cor}">Margem do dia: {_pct(m_hoje[6])}{seta}</span>{base}</div>')
        P.append(f'<div style="color:#666;font-size:13px;margin-bottom:18px">'
                 f'{m_hoje[1]} pedido(s) &middot; receita {_rs(m_hoje[3])}</div>')
    else:
        P.append('<div style="font-size:16px;margin:8px 0 18px;color:#666">'
                 'Margem do dia: ainda sem venda aprovada hoje.</div>')

    feitas = [x for x in linhas if x[8] == "ok"]
    falhas = [x for x in linhas if x[8] != "ok"]

    P.append(f'<div style="font-weight:700;margin:0 0 8px">&#9989; Aplicadas nesta passada: {len(feitas)}</div>')
    if feitas:
        for x in feitas:
            marg = f' &middot; margem-alvo {_pct(x[5])}' if x[5] not in (None, "") else ""
            porque = _PORQUE.get(x[3], "")
            porque = f'<div style="color:#999;font-size:12px">{porque}</div>' if porque else ""
            P.append(f'<div style="border-left:3px solid #22c55e;padding:1px 0 1px 11px;margin-bottom:9px">'
                     f'<b>{_esc(x[2] or x[1])}</b> — {_LABEL.get(x[3], x[3])} &rarr; <b>{_rs(x[4])}</b>{marg}{porque}</div>')
    else:
        P.append('<div style="color:#888;margin-bottom:9px">nenhuma.</div>')

    if n_mantido:
        P.append(f'<div style="margin:16px 0 4px"><b>&#128274; Descontos mantidos (anti-gangorra): {n_mantido}</b>'
                 f'<div style="color:#999;font-size:12px">ganham só por causa do desconto; '
                 f'remover faria o preço subir e perder a ponta</div></div>')

    if falhas:
        P.append(f'<div style="font-weight:700;color:#dc2626;margin:16px 0 8px">'
                 f'&#9888;&#65039; Falhas: {len(falhas)}</div>')
        for x in falhas:
            P.append(f'<div style="border-left:3px solid #dc2626;padding:1px 0 1px 11px;margin-bottom:9px">'
                     f'<b>{_esc(x[2] or x[1])}</b> — {_LABEL.get(x[3], x[3])} &middot; HTTP {x[7]}'
                     f'<div style="color:#999;font-size:12px">retenta na próxima; '
                     f'se insistir, é preço/faixa inválida ou token</div></div>')

    P.append('</div>')
    return "".join(P)


def main():
    agora = datetime.now(BR).strftime("%Y-%m-%d %H:%M")
    hora = datetime.now(BR).strftime("%Hh")
    linhas, n_ok, n_erro, n_mantido = mudancas_da_passada(SELLER_ID, JANELA_MIN)
    _post({"aba": "Mudancas", "quando": agora, "conta": SELLER_ID, "rows": linhas})

    dias = margem_diaria(SELLER_ID, MARGEM_DIAS)
    _post({"aba": "MargemDiaria", "quando": agora, "conta": SELLER_ID, "rows": dias})

    hoje = datetime.now(BR).date().isoformat()
    m_hoje = next((l for l in dias if l[0] == hoje), None)
    corpo = montar_email(hora, linhas, n_ok, n_erro, n_mantido, m_hoje)
    marg_txt = f", margem {m_hoje[6]}%" if (m_hoje and m_hoje[6] is not None) else ""
    falha_txt = f", {n_erro} falha(s)" if n_erro else ""
    assunto = f"Repricer {hora} {NOME_CONTA} — {n_ok} mudança(s){falha_txt}{marg_txt}"
    # o Apps Script grava a linha E manda o email (corpo) em TODA passada ao vivo, na hora.
    _post({"aba": "Passadas", "quando": agora, "conta": SELLER_ID,
           "nota": corpo, "para": EMAIL_RELATORIO, "tem_mudanca": len(linhas) > 0,
           "modo": MODO,   # Apps Script só manda email em modo 'live'
           "assunto": assunto,
           "rows": [[agora, SELLER_ID, f"{n_ok} ok / {n_erro} erro / {n_mantido} mantido", assunto]]})
    print(f"Relatório: {n_ok} ok, {n_erro} erro, {n_mantido} mantido, {len(dias)} dia(s). {assunto}", flush=True)


if __name__ == "__main__":
    main()
