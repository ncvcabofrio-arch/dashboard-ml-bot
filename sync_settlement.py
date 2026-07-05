"""
Sincronizador do relatório "dinheiro em conta" (SETTLEMENT) do Mercado Pago
-> tabela settlement_mp no Supabase.

IMPORTANTE — autenticação:
  ML e MP compartilham o MESMO OAuth. O robô mint o access_token a partir do
  refresh_token de CADA conta (tabela `contas`), igual aos outros robôs, e usa
  esse MESMO token contra api.mercadopago.com. NÃO precisa de secret novo.

Fonte da API (família settlement_report):
  POST /v1/account/settlement_report/config   -> escolhe as colunas
  POST /v1/account/settlement_report          -> manda gerar (begin_date/end_date)
  GET  /v1/account/settlement_report          -> lista os relatórios gerados
  GET  /v1/account/settlement_report/{file}   -> baixa o CSV

MODOS (env MODO):
  sonda  (padrão) -> só TESTA: mint token de cada conta, chama a API do MP e
                     imprime o status + a resposta crua. NÃO gera, NÃO grava.
                     É o "testar antes" — confirma que o token abre a API do MP
                     e nos mostra o formato real das respostas.
  cheio           -> pipeline completo: garante config, gera a janela, baixa,
                     e carrega na settlement_mp (apaga a janela e reinsere).

Env:
  ML_CLIENT_ID, ML_CLIENT_SECRET  (mesmos secrets dos outros robôs)
  SUPABASE_URL, SUPABASE_KEY
  MODO         = sonda | cheio         (padrão: sonda)
  DIAS         = janela p/ trás em dias (padrão: 45)
  BEGIN, END   = datas ISO fixas (opcional; sobrepõem DIAS) ex.: 2026-05-01
  ONLY_SELLER  = roda só uma conta (seller_id) — bom pro 1º teste
"""

import os
import io
import csv
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client

CLIENT_ID     = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]

MODO        = os.environ.get("MODO", "sonda").strip().lower()
DIAS        = int(os.environ.get("DIAS", "45"))
BEGIN_FIX   = os.environ.get("BEGIN", "").strip()
END_FIX     = os.environ.get("END", "").strip()
ONLY_SELLER = os.environ.get("ONLY_SELLER", "").strip()

OAUTH = "https://api.mercadolibre.com/oauth/token"   # servidor de OAuth (ML=MP)
MP    = "https://api.mercadopago.com"                 # onde estão os relatórios
REPORT = "/v1/account/settlement_report"

# colunas que validamos no arquivo que você baixou (o cupom vem em OPERATION_TAGS)
COLUNAS = ["EXTERNAL_REFERENCE", "SOURCE_ID", "TRANSACTION_AMOUNT", "TRANSACTION_DATE",
           "FEE_AMOUNT", "SETTLEMENT_NET_AMOUNT", "SETTLEMENT_DATE",
           "PRODUCT_SKU", "SALE_DETAIL", "OPERATION_TAGS"]

# como os cabeçalhos do CSV mapeiam pras colunas da tabela
MAPA = {
    "EXTERNAL_REFERENCE":   "external_reference",
    "SOURCE_ID":            "source_id",
    "TRANSACTION_AMOUNT":   "transaction_amount",
    "TRANSACTION_DATE":     "transaction_date",
    "FEE_AMOUNT":           "fee_amount",
    "SETTLEMENT_NET_AMOUNT":"net_amount",
    "SETTLEMENT_DATE":      "settlement_date",
    "PRODUCT_SKU":          "sku",
    "SALE_DETAIL":          "sale_detail",
    "OPERATION_TAGS":       "operation_tags",
}
NUMERICAS = {"transaction_amount", "fee_amount", "net_amount"}
DATAS     = {"transaction_date", "settlement_date"}

try:
    from supabase.lib.client_options import ClientOptions
    sb = create_client(SUPABASE_URL, SUPABASE_KEY,
                       options=ClientOptions(postgrest_client_timeout=120))
except Exception:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def renovar_token(refresh_token):
    r = requests.post(OAUTH, data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Falha ao renovar token: " + str(d)[:200])
    return d


def contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    out = {str(c["seller_id"]): c["refresh_token"]
           for c in (res.data or []) if c.get("refresh_token")}
    if ONLY_SELLER:
        out = {k: v for k, v in out.items() if k == ONLY_SELLER}
    return out


def mp_headers(access):
    return {"Authorization": "Bearer " + access, "Content-Type": "application/json"}


# ----------------------------------------------------------------------
#  MODO SONDA — só testa e mostra o formato real (não altera nada)
# ----------------------------------------------------------------------
def sonda(seller_id, access):
    print(f"\n=== [{seller_id}] SONDA ===")
    # 1) o token abre a API do MP? (quem sou eu)
    r = requests.get(MP + "/users/me", headers=mp_headers(access), timeout=20)
    print(f"  GET /users/me -> {r.status_code}")
    if r.status_code == 200:
        me = r.json()
        print(f"    id={me.get('id')} nickname={me.get('nickname')} site={me.get('site_id')}")
    else:
        print("    corpo:", r.text[:300])

    # 2) consigo listar relatórios de settlement? (é o que precisamos)
    r = requests.get(MP + REPORT, headers=mp_headers(access), timeout=30)
    print(f"  GET {REPORT} (lista) -> {r.status_code}")
    print("    corpo (cru, 800 chars):", r.text[:800])

    # 3) config atual das colunas
    r = requests.get(MP + REPORT + "/config", headers=mp_headers(access), timeout=30)
    print(f"  GET {REPORT}/config -> {r.status_code}")
    print("    corpo (cru, 500 chars):", r.text[:500])


# ----------------------------------------------------------------------
#  MODO CHEIO — gera, baixa e carrega
# ----------------------------------------------------------------------
def janela():
    if BEGIN_FIX and END_FIX:
        b = BEGIN_FIX + "T00:00:00Z"
        e = END_FIX + "T00:00:00Z"
        b_date = BEGIN_FIX
    else:
        hoje = datetime.now(timezone.utc)
        ini  = hoje - timedelta(days=DIAS)
        b = ini.strftime("%Y-%m-%dT00:00:00Z")
        e = (hoje + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        b_date = ini.strftime("%Y-%m-%d")
    return b, e, b_date


def garantir_config(access):
    body = {"columns": [{"key": k} for k in COLUNAS]}
    r = requests.post(MP + REPORT + "/config", headers=mp_headers(access),
                      data=json.dumps(body), timeout=30)
    print(f"  config colunas -> {r.status_code} {r.text[:150]}")


def gerar(access, begin, end):
    body = {"begin_date": begin, "end_date": end}
    r = requests.post(MP + REPORT, headers=mp_headers(access),
                      data=json.dumps(body), timeout=60)
    print(f"  gerar {begin}..{end} -> {r.status_code} {r.text[:200]}")
    # a resposta costuma trazer um id/file da tarefa; devolvemos o que vier
    try:
        return r.json()
    except Exception:
        return {}


def achar_arquivo(access, desde_ts):
    """Fica olhando a lista até aparecer um relatório novo pronto pra baixar."""
    for tentativa in range(40):                       # ~5 min
        r = requests.get(MP + REPORT, headers=mp_headers(access), timeout=30)
        if r.status_code == 200:
            try:
                lista = r.json()
            except Exception:
                lista = []
            # a lista pode vir como array ou dentro de uma chave
            if isinstance(lista, dict):
                lista = lista.get("results") or lista.get("reports") or []
            # pega o mais recente que tenha nome de arquivo
            cand = None
            for it in lista:
                fn = it.get("file_name") or it.get("filename") or it.get("name")
                if fn:
                    cand = fn                          # lista costuma vir do mais novo
                    break
            if cand:
                return cand
        time.sleep(8)
    return None


def baixar(access, file_name):
    r = requests.get(MP + REPORT + "/" + file_name, headers=mp_headers(access), timeout=120)
    if r.status_code != 200:
        print(f"  download {file_name} -> {r.status_code} {r.text[:150]}")
        return None
    return r.content.decode("utf-8", errors="replace")


def num(v):
    if v is None:
        return None
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if s == "":
        return None
    # trata "1.234,56" (pt) e "1234.56" (en)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except Exception:
        return None


def data(v):
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


def parse_csv(texto, seller_id):
    # detecta o separador (MP costuma usar ; ou ,)
    amostra = texto[:2000]
    try:
        dialect = csv.Sniffer().sniff(amostra, delimiters=";,")
        sep = dialect.delimiter
    except Exception:
        sep = ";" if amostra.count(";") >= amostra.count(",") else ","
    rd = csv.DictReader(io.StringIO(texto), delimiter=sep)
    linhas = []
    for row in rd:
        # normaliza cabeçalho (maiúsculo, sem espaços)
        rr = {(k or "").strip().upper(): v for k, v in row.items()}
        out = {"seller_id": seller_id}
        for col_csv, col_tab in MAPA.items():
            val = rr.get(col_csv)
            if col_tab in NUMERICAS:
                out[col_tab] = num(val)
            elif col_tab in DATAS:
                out[col_tab] = data(val)
            else:
                out[col_tab] = (val or None)
        # só guarda o que assentou de fato (tem data de liberação e valor líquido)
        if out.get("settlement_date") and out.get("net_amount") is not None:
            linhas.append(out)
    return linhas


def carregar(seller_id, linhas, begin_date):
    if not linhas:
        print(f"  [{seller_id}] nada a carregar.")
        return
    # apaga a janela (settlement_date >= inicio) só dessa conta e reinsere
    sb.table("settlement_mp").delete() \
        .eq("seller_id", seller_id).gte("settlement_date", begin_date).execute()
    for i in range(0, len(linhas), 500):
        sb.table("settlement_mp").insert(linhas[i:i + 500]).execute()
    print(f"  [{seller_id}] carregado: {len(linhas)} linhas (janela desde {begin_date}).")


def cheio(seller_id, access):
    print(f"\n=== [{seller_id}] CHEIO ===")
    begin, end, begin_date = janela()
    garantir_config(access)
    inicio = time.time()
    gerar(access, begin, end)
    file_name = achar_arquivo(access, inicio)
    if not file_name:
        print(f"  [{seller_id}] relatório não ficou pronto a tempo. Tente de novo mais tarde.")
        return
    print(f"  arquivo: {file_name}")
    texto = baixar(access, file_name)
    if not texto:
        return
    linhas = parse_csv(texto, seller_id)
    carregar(seller_id, linhas, begin_date)


def main():
    cs = contas()
    if not cs:
        print("Nenhuma conta com refresh_token em `contas`.")
        return
    print(f"MODO={MODO} | contas={list(cs.keys())} | DIAS={DIAS}")
    for seller_id, refresh in cs.items():
        try:
            d = renovar_token(refresh)
        except Exception as e:
            print(f"[{seller_id}] falha ao renovar token: {e}")
            continue
        access = d["access_token"]
        # salva o refresh rotacionado (bom hábito, igual aos outros robôs)
        try:
            sb.table("contas").update({"refresh_token": d.get("refresh_token", refresh)}) \
              .eq("seller_id", int(seller_id)).execute()
        except Exception:
            pass

        if MODO == "cheio":
            cheio(seller_id, access)
        else:
            sonda(seller_id, access)

    print("\n✅ Concluído.")


if __name__ == "__main__":
    main()
