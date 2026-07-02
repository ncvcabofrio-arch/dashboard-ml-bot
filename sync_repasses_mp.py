"""
Robô — baixa o Relatório de Liberações do Mercado Pago e guarda em 'releases_mp'.
É a foto real do que o Mercado Pago te pagou (por pagamento), pra conciliar com as vendas.

Fluxo por conta:
  1) renova o token do ML (que também acessa o Mercado Pago)
  2) manda GERAR o relatório do período (POST) — assíncrono
  3) espera ficar pronto (consulta a lista até aparecer o arquivo)
  4) baixa o arquivo (CSV ou XLSX) e lê pela POSIÇÃO das colunas
  5) grava as linhas em 'releases_mp' (sem duplicar, via hash)

Roda no GitHub Actions, na trava 'ml-puxador' (renova o token, que é de uso único).
Período: env DIAS_MP (padrão 40 dias). Pra puxar histórico, aumente DIAS_MP.
"""

import os
import io
import csv
import json
import time
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client

CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DIAS_MP = int(os.environ.get("DIAS_MP", "40"))

ML = "https://api.mercadolibre.com"
MP = "https://api.mercadopago.com"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def renovar_token(refresh):
    r = requests.post(ML + "/oauth/token", data={
        "grant_type": "refresh_token", "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "refresh_token": refresh}, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Falha ao renovar token: " + str(d))
    return d


def lista_contas():
    res = sb.table("contas").select("seller_id, apelido, refresh_token").execute()
    tk = [(c["seller_id"], c.get("apelido"), c["refresh_token"])
          for c in (res.data or []) if c.get("refresh_token")]
    if not tk and SEED_REFRESH:
        tk = [(None, "(seed)", SEED_REFRESH)]
    return tk


def num(s):
    try:
        return round(float(str(s).strip() or 0), 2)
    except Exception:
        return 0.0


def parse_linha(cells, seller_id):
    c = [(x if x is not None else "") for x in cells]
    while len(c) < 15:
        c.append("")
    data = str(c[0]).strip() or None
    d = {
        "seller_id": seller_id,
        "data": data,
        "source_id": str(c[1]).strip(),
        "descricao": str(c[2]).strip(),
        "net_credit": num(c[3]),
        "net_debit": num(c[4]),
        "gross": num(c[5]),
        "mp_fee": num(c[6]),
        "taxes": num(c[7]),
        "payment_method": str(c[8]).strip(),
        "approval_date": (str(c[9]).strip() or None),
        "balance": num(c[12]),
        "payment_method_type": str(c[13]).strip(),
        "purchase_id": str(c[14]).strip(),
    }
    base = f"{seller_id}|{d['data']}|{d['source_id']}|{d['descricao']}|{d['net_credit']}|{d['net_debit']}|{d['balance']}"
    d["hash"] = hashlib.md5(base.encode()).hexdigest()
    return d


def linhas_do_arquivo(file_name, conteudo, seller_id):
    linhas = []
    if (file_name or "").lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue  # cabeçalho
            linhas.append(parse_linha(row, seller_id))
    else:
        texto = conteudo.decode("utf-8", errors="replace")
        rd = csv.reader(io.StringIO(texto), delimiter=";")
        for i, row in enumerate(rd):
            if i == 0 or not row:
                continue
            linhas.append(parse_linha(row, seller_id))
    return linhas


def gerar_e_baixar(H, ini, fim):
    """Gera o relatório do período e espera ficar pronto. Retorna o file_name."""
    # o que já existe antes (pra achar o novo depois)
    antes = requests.get(MP + "/v1/account/release_report/list", headers=H, timeout=30).json()
    tinha = set(x.get("file_name") for x in antes if isinstance(x, dict)) if isinstance(antes, list) else set()

    corpo = {"begin_date": ini, "end_date": fim}
    g = requests.post(MP + "/v1/account/release_report",
                      headers={**H, "Content-Type": "application/json"},
                      data=json.dumps(corpo), timeout=30)
    if g.status_code not in (200, 202):
        print("  falha ao gerar:", g.status_code, g.text[:150])
        return None

    for tent in range(18):          # até ~6 min
        time.sleep(20)
        lst = requests.get(MP + "/v1/account/release_report/list", headers=H, timeout=30).json()
        if not isinstance(lst, list):
            continue
        novos = [x for x in lst if x.get("file_name") and x.get("file_name") not in tinha]
        if novos:
            rel = sorted(novos, key=lambda x: x.get("date_created", ""), reverse=True)[0]
            return rel.get("file_name")
    print("  relatório ainda não ficou pronto (tenta de novo na próxima rodada).")
    return None


def main():
    fim = datetime.now(timezone.utc)
    ini = fim - timedelta(days=DIAS_MP)
    ini_s = ini.strftime("%Y-%m-%dT%H:%M:%SZ")
    fim_s = fim.strftime("%Y-%m-%dT%H:%M:%SZ")

    for sid, apelido, refresh in lista_contas():
        print("=" * 55)
        print(f"CONTA {apelido or sid} ({sid})")
        try:
            d = renovar_token(refresh)
        except Exception as e:
            print("  não consegui renovar o token:", e)
            continue
        access = d["access_token"]
        novo = d.get("refresh_token", refresh)
        sid_real = str(d.get("user_id") or sid or "")
        if sid_real:
            sb.table("contas").upsert(
                {"seller_id": sid_real, "refresh_token": novo},
                on_conflict="seller_id").execute()
        H = {"Authorization": "Bearer " + access}

        fn = gerar_e_baixar(H, ini_s, fim_s)
        if not fn:
            continue
        r = requests.get(MP + f"/v1/account/release_report/{fn}", headers=H, timeout=90)
        if r.status_code != 200:
            print("  falha ao baixar:", r.status_code)
            continue
        linhas = linhas_do_arquivo(fn, r.content, sid_real)
        # grava sem duplicar
        n = 0
        for i in range(0, len(linhas), 200):
            lote = linhas[i:i + 200]
            try:
                sb.table("releases_mp").upsert(lote, on_conflict="hash").execute()
                n += len(lote)
            except Exception as e:
                print("  erro ao gravar lote:", str(e)[:100])
        print(f"  {fn}: {len(linhas)} movimentos lidos, {n} gravados.")

    print("=" * 55)
    print("✅ Fim. Tabela releases_mp atualizada.")


if __name__ == "__main__":
    main()
