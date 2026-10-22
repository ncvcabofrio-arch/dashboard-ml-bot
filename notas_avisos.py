"""
Avisa no Telegram as NOTAS NOVAS do Bling (boletos que apareceram pela 1ª vez).
Roda logo depois do robô "Bling - contas a pagar". Python puro (sem pip install).
- Gera os avisos no Supabase (função notas_gerar_avisos)
- Manda no Telegram os que ainda não foram mandados
Não imprime token nenhum.
"""
import os, json, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SB = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
MES = ["", "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def http(method, url, headers, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def brl(v):
    s = f"{float(v or 0):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def mes(d):
    if not d:
        return "?"
    a, m = d[:4], int(d[5:7])
    return f"{MES[m]}/{a[2:]}"


def esc(t):
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 1) gera os avisos dos boletos novos
st, raw = http("POST", f"{SB_URL}/rest/v1/rpc/notas_gerar_avisos", SB, b"{}")
if st >= 300:
    raise SystemExit(f"Erro ao gerar avisos: HTTP {st} {raw[:300]}")
novos = json.loads(raw) or []
print(f"Avisos novos gerados: {len(novos)}")

# 2) pega todos que ainda não foram pro Telegram
st, raw = http("GET", f"{SB_URL}/rest/v1/notas_avisos?telegram_em=is.null&ciente_em=is.null"
               "&select=id,conta_nome,fornecedor,valor,boletos,primeiro_venc,ultimo_venc&order=valor.desc", SB)
pend = json.loads(raw) if st < 300 else []
if not pend:
    print("Nada pra avisar.")
    raise SystemExit(0)
if not (TG_TOKEN and TG_CHAT):
    print(f"{len(pend)} aviso(s) no painel. Telegram não configurado (faltam os secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
    raise SystemExit(0)


def tg(texto):
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": texto[:4000], "parse_mode": "HTML"}).encode()
    st, raw = http("POST", f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   {"Content-Type": "application/x-www-form-urlencoded"}, body)
    return st < 300


linhas = []
for a in pend:
    venc = mes(a["primeiro_venc"]) if a["primeiro_venc"] == a["ultimo_venc"] else f"{mes(a['primeiro_venc'])} → {mes(a['ultimo_venc'])}"
    bol = f"{a['boletos']} boleto" + ("s" if a["boletos"] != 1 else "")
    linhas.append(f"• <b>{esc(a['fornecedor'])}</b> — {brl(a['valor'])}\n   {bol} ({venc}) · {esc(a['conta_nome'])} · /ciente {a['id']}")

titulo = "🧾 <b>Nota nova no Bling</b>" if len(pend) == 1 else f"🧾 <b>{len(pend)} notas novas no Bling</b>"
msg = titulo + "\n\n" + "\n".join(linhas) + "\n\nDê ciência no painel ou mande /ciente (ou /ciente tudo)."
if tg(msg):
    ids = ",".join(str(a["id"]) for a in pend)
    agora = datetime.now(timezone.utc).isoformat()
    http("PATCH", f"{SB_URL}/rest/v1/notas_avisos?id=in.({ids})",
         {**SB, "Prefer": "return=minimal"}, json.dumps({"telegram_em": agora}).encode())
    print(f"Telegram: {len(pend)} aviso(s) enviados.")
else:
    print("Falha ao mandar no Telegram (fica pra próxima rodada).")
