"""
Robô: puxa as CONTAS A PAGAR em aberto do Bling (das contas em bling_contas) e mantém
a tabela contas_pagar sempre igual ao Bling (boleto pago some sozinho).

- Renova o token só se estiver perto de expirar (< 15 min).
- Resolve o NOME do fornecedor (com cache em bling_contatos).
- Estratégia "substituir": pra cada conta, apaga e regrava só os EM ABERTO (situacao=1).
Python puro (sem pip install).
"""
import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

BASE = "https://www.bling.com.br/Api/v3"
TOKEN_URL = BASE + "/oauth/token"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
SB_HDR = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}


def http(method, url, headers, data=None, timeout=40):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def sb_get(path):
    st, raw = http("GET", f"{SB_URL}/rest/v1/{path}", SB_HDR)
    return json.loads(raw) if st < 300 and raw else []


def sb_write(method, path, body=None):
    hdr = dict(SB_HDR)
    hdr["Content-Type"] = "application/json"
    hdr["Prefer"] = "resolution=merge-duplicates,return=minimal"
    data = json.dumps(body).encode() if body is not None else None
    st, raw = http(method, f"{SB_URL}/rest/v1/{path}", hdr, data)
    if st >= 300:
        raise RuntimeError(f"Supabase {method} {path} -> HTTP {st} {raw[:200]}")
    return st


def obter_access(row):
    """Reutiliza o access_token se ainda vale >15min; senão renova e salva."""
    conta = row["conta"]
    tok = row.get("access_token")
    exp = row.get("access_expira_em")
    if tok and exp:
        try:
            dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if dt > datetime.now(timezone.utc) + timedelta(minutes=15):
                return tok
        except Exception:
            pass
    secret = os.environ.get("BLING_SECRET_" + conta)
    if not secret:
        raise RuntimeError(f"Sem secret BLING_SECRET_{conta}.")
    basic = base64.b64encode(f"{row['client_id']}:{secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": row["refresh_token"]}).encode()
    st, raw = http("POST", TOKEN_URL, {
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"}, body)
    d = json.loads(raw)
    if "access_token" not in d:
        raise RuntimeError(f"Falha no refresh: HTTP {st} {d}")
    expira = (datetime.now(timezone.utc)
              + timedelta(seconds=int(d.get("expires_in", 21600)))).isoformat()
    fields = {"access_token": d["access_token"], "access_expira_em": expira}
    if d.get("refresh_token"):
        fields["refresh_token"] = d["refresh_token"]
    sb_write("PATCH", f"bling_contas?conta=eq.{urllib.parse.quote(conta)}", fields)
    return d["access_token"]


def baixar_abertas(access):
    """Baixa TODAS as contas a pagar (paginado) e retorna só as em aberto (situacao=1)."""
    hdr = {"Authorization": "Bearer " + access, "Accept": "application/json"}
    abertas, pagina = [], 1
    while pagina <= 200:  # trava de segurança
        st, raw = http("GET", f"{BASE}/contas/pagar?pagina={pagina}&limite=100", hdr)
        if st >= 300:
            raise RuntimeError(f"contas/pagar HTTP {st}: {raw[:200]}")
        data = json.loads(raw).get("data", [])
        if not data:
            break
        abertas += [x for x in data if x.get("situacao") == 1]
        if len(data) < 100:
            break
        pagina += 1
    return abertas


def resolver_fornecedores(access, ids):
    """Devolve {contato_id: nome}, usando cache bling_contatos e buscando os que faltam."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    lista = ",".join(str(i) for i in ids)
    cache = sb_get(f"bling_contatos?id=in.({lista})&select=id,nome")
    nome = {c["id"]: c["nome"] for c in cache}
    novos = []
    hdr = {"Authorization": "Bearer " + access, "Accept": "application/json"}
    for i in ids:
        if i in nome:
            continue
        st, raw = http("GET", f"{BASE}/contatos/{i}", hdr)
        if st < 300:
            d = json.loads(raw).get("data", {})
            nome[i] = d.get("nome") or f"(contato {i})"
            novos.append({"id": i, "nome": nome[i], "documento": d.get("numeroDocumento") or ""})
        else:
            nome[i] = f"(contato {i})"
    if novos:
        sb_write("POST", "bling_contatos?on_conflict=id", novos)
    return nome


def main():
    contas = sb_get("bling_contas?select=conta,client_id,refresh_token,access_token,access_expira_em")
    print("Contas:", [c["conta"] for c in contas])
    total = 0
    for row in contas:
        conta = row["conta"]
        try:
            access = obter_access(row)
            abertas = baixar_abertas(access)
        except Exception as e:
            print(f"[{conta}] pulei (erro, mantive dados de ontem): {e}")
            continue

        ids = sorted({(x.get("contato") or {}).get("id") for x in abertas})
        nome = resolver_fornecedores(access, ids)

        linhas = [{
            "id": x["id"],
            "conta": conta,
            "situacao": x.get("situacao"),
            "vencimento": x.get("vencimento"),
            "valor": x.get("valor"),
            "contato_id": (x.get("contato") or {}).get("id"),
            "fornecedor": nome.get((x.get("contato") or {}).get("id")),
            "forma_pagamento_id": (x.get("formaPagamento") or {}).get("id"),
        } for x in abertas]

        # Substitui: apaga as desta conta e regrava só as em aberto de agora.
        sb_write("DELETE", f"contas_pagar?conta=eq.{urllib.parse.quote(conta)}")
        for i in range(0, len(linhas), 200):
            sb_write("POST", "contas_pagar", linhas[i:i + 200])
        soma = sum((l["valor"] or 0) for l in linhas)
        print(f"[{conta}] {len(linhas)} contas a pagar em aberto — total R$ {soma:,.2f}")
        total += len(linhas)
    print(f"Fim. {total} boletos em aberto no total.")


if __name__ == "__main__":
    main()
