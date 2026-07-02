"""
Teste (diagnóstico) — verifica se o token do Mercado Livre também acessa o
Relatório de Liberações do Mercado Pago (/v1/account/release_report).
Não grava nada de venda; só testa o acesso e mostra a resposta no log.

Roda no GitHub Actions, na MESMA trava do puxador (ml-puxador), porque renova
o token (que é de uso único).
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client

CLIENT_ID = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
SEED_REFRESH = os.environ.get("ML_REFRESH_TOKEN", "")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

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


def mostra(nome, r):
    print(f"  [{nome}] HTTP {r.status_code}")
    txt = r.text[:600]
    print("     " + txt.replace("\n", " "))


def main():
    contas = lista_contas()
    if not contas:
        print("Nenhuma conta com refresh_token.")
        return

    fim = datetime.now(timezone.utc)
    ini = fim - timedelta(days=7)
    corpo = {"begin_date": ini.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "end_date": fim.strftime("%Y-%m-%dT%H:%M:%SZ")}

    for sid, apelido, refresh in contas:
        print("=" * 60)
        print(f"CONTA {apelido or sid} ({sid})")
        try:
            d = renovar_token(refresh)
        except Exception as e:
            print("  não consegui renovar o token:", e)
            continue
        access = d["access_token"]
        novo = d.get("refresh_token", refresh)
        if sid:
            sb.table("contas").upsert(
                {"seller_id": str(d.get("user_id") or sid), "refresh_token": novo},
                on_conflict="seller_id").execute()
        H = {"Authorization": "Bearer " + access}

        # 1) o token acessa a config do relatório? (só leitura)
        mostra("GET config", requests.get(MP + "/v1/account/release_report/config", headers=H, timeout=30))
        # 2) lista de relatórios já gerados (só leitura)
        mostra("GET list", requests.get(MP + "/v1/account/release_report/list", headers=H, timeout=30))
        # 3) tenta gerar um relatório dos últimos 7 dias (gera um arquivo, não altera dados)
        mostra("POST gerar", requests.post(
            MP + "/v1/account/release_report",
            headers={**H, "Content-Type": "application/json"},
            data=json.dumps(corpo), timeout=30))
        time.sleep(1)

    print("=" * 60)
    print("Interprete: se aparecer HTTP 200/202 no 'GET config'/'POST gerar', o token "
          "JÁ acessa o Mercado Pago e a gente segue. Se aparecer 401/403, precisamos de "
          "um access_token separado do Mercado Pago.")


if __name__ == "__main__":
    main()
