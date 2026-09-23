"""
Robô: puxa as CONTAS A PAGAR do Bling (das contas em bling_contas) e mantém
a tabela contas_pagar sempre igual ao Bling (boleto pago some sozinho).

- Renova o token só se estiver perto de expirar (< 15 min).
- Resolve o NOME do fornecedor (com cache em bling_contatos).
- Upsert por id (atualiza a situação quando um boleto passa de aberto p/ pago).
Python puro (sem pip install).

=====================================================================================
 LIGA/DESLIGA DO FILTRO DE DATA DE LANÇAMENTO  (carga faseada)
=====================================================================================
 Com uma data aqui, o robô traz SÓ o que foi lançado ATÉ essa data (ex.: fechar agosto).
 Vale também nas rodadas automáticas — então o painel fica "travado" nessa fase.

   DATA_ATE = "2026-08-31"   -> traz só o que foi lançado até 31/08 (fase 1)

 Quando quiser VOLTAR AO NORMAL (trazer tudo, inclusive setembro em diante),
 troque a linha abaixo por uma vazia:

   DATA_ATE = ""             -> sem filtro, traz tudo (normal)
=====================================================================================
"""
import os
import sys
import json
import time
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# Mostra as mensagens AO VIVO no log (sem segurar o texto até o fim).
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# >>> AJUSTE AQUI <<<  (vazio "" = normal; com data = só até essa data de lançamento)
DATA_ATE = ""
# Também dá pra controlar por variável no GitHub, sem mexer no código:
DATA_ATE = (os.environ.get("DATA_ATE", DATA_ATE) or "").strip()[:10]

# ATENÇÃO (set/2026): o Bling BLOQUEOU www.bling.com.br para requisições de DADOS (HTTP 403).
# Agora os dados precisam ir para api.bling.com.br. Já o OAuth/token continua em www.bling.com.br.
API_BASE = "https://api.bling.com.br/Api/v3"           # dados: /contas/pagar, /contatos
TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"   # oauth continua no www
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


# ---- Chamadas ao BLING com freio (rate limit) + retry no 429/5xx ----
_ultima = [0.0]
MIN_INTERVALO = 0.6   # segundos entre chamadas ao Bling (~1,6/s; abaixo do limite de 3/s)


def bling(method, url, headers, data=None, tentativas=6):
    st, raw = 0, ""
    for i in range(tentativas):
        espera = MIN_INTERVALO - (time.time() - _ultima[0])
        if espera > 0:
            time.sleep(espera)
        _ultima[0] = time.time()
        st, raw = http(method, url, headers, data)
        if st == 429 or st >= 500:
            time.sleep(2 * (i + 1))   # backoff: 2s, 4s, 6s...
            continue
        return st, raw
    return st, raw


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
    st, raw = bling("POST", TOKEN_URL, {
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


def baixar_contas(access):
    """Baixa TODAS as contas a pagar (paginado): em aberto (1) e pagas (2)."""
    hdr = {"Authorization": "Bearer " + access, "Accept": "application/json"}
    tudo, pagina = [], 1
    while pagina <= 300:  # trava de segurança
        st, raw = bling("GET", f"{API_BASE}/contas/pagar?pagina={pagina}&limite=100", hdr)
        if st >= 300:
            raise RuntimeError(f"contas/pagar HTTP {st}: {raw[:200]}")
        data = json.loads(raw).get("data", [])
        if not data:
            break
        tudo += [x for x in data if x.get("situacao") in (1, 2)]
        if len(data) < 100:
            break
        pagina += 1
    return tudo


def achar_emissao(obj):
    """Acha a DATA DE LANÇAMENTO/EMISSÃO por qualquer chave que contenha 'emiss' ou
    'lanca' (nunca casa com 'vencimento'). Retorna 'AAAA-MM-DD' ou None."""
    for k, v in obj.items():
        lk = k.lower()
        if "emiss" in lk or "lanca" in lk or "lançа" in lk:
            if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
                return v[:10]
    return None


def emissao_detalhe(access, cid):
    hdr = {"Authorization": "Bearer " + access, "Accept": "application/json"}
    st, raw = bling("GET", f"{API_BASE}/contas/pagar/{cid}", hdr)
    if st < 300:
        return achar_emissao(json.loads(raw).get("data", {}))
    return None


def filtrar_por_lancamento(access, conta, registros):
    """Se DATA_ATE estiver ligado, mantém só os lançados até essa data."""
    if not DATA_ATE:
        return registros
    print(f"[{conta}] AMOSTRA do 1º registro: "
          f"{json.dumps(registros[0], ensure_ascii=False)[:600]}")
    tem_na_lista = any(achar_emissao(r) for r in registros[:8])
    modo = "lista" if tem_na_lista else "detalhe"
    print(f"[{conta}] filtro LIGADO: só lançados até {DATA_ATE}. Data de lançamento via: {modo}"
          + ("" if tem_na_lista else " (consultando detalhe de cada boleto — pode demorar alguns minutos)"))
    selecionados, sem_data = [], 0
    for idx, r in enumerate(registros, 1):
        em = achar_emissao(r) if tem_na_lista else emissao_detalhe(access, r["id"])
        if not em:
            sem_data += 1
            continue
        if em <= DATA_ATE:
            selecionados.append(r)
        if modo == "detalhe" and idx % 50 == 0:
            print(f"    ...{idx}/{len(registros)} conferidos ({len(selecionados)} até {DATA_ATE})")
    if sem_data:
        print(f"[{conta}] AVISO: {sem_data} sem data de lançamento legível (ignorados).")
    return selecionados


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
        st, raw = bling("GET", f"{API_BASE}/contatos/{i}", hdr)
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
    if DATA_ATE:
        print(f">>> FILTRO LIGADO: importando só o que foi LANÇADO até {DATA_ATE} <<<")
    else:
        print(">>> Modo normal: importando tudo <<<")
    contas = sb_get("bling_contas?select=conta,client_id,refresh_token,access_token,access_expira_em")
    print("Contas:", [c["conta"] for c in contas])
    total = 0
    for row in contas:
        conta = row["conta"]
        print(f"--- Processando {conta} ---")
        try:
            access = obter_access(row)
            registros = baixar_contas(access)
            print(f"[{conta}] baixei {len(registros)} boletos do Bling.")
        except Exception as e:
            print(f"[{conta}] pulei (erro, mantive dados de ontem): {e}")
            continue

        registros = filtrar_por_lancamento(access, conta, registros) if registros else registros

        ids = sorted({(x.get("contato") or {}).get("id") for x in registros})
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
        } for x in registros]

        # Upsert por id: atualiza a situação quando um boleto passa de aberto p/ pago.
        for i in range(0, len(linhas), 200):
            sb_write("POST", "contas_pagar?on_conflict=id", linhas[i:i + 200])
        aberto = sum((l["valor"] or 0) for l in linhas if l["situacao"] == 1)
        pago = sum((l["valor"] or 0) for l in linhas if l["situacao"] == 2)
        print(f"[{conta}] {len(linhas)} contas — em aberto R$ {aberto:,.2f} · pagas R$ {pago:,.2f}")
        total += len(linhas)
    print(f"Fim. {total} contas a pagar no total"
          + (f" (só lançadas até {DATA_ATE})." if DATA_ATE else "."))


if __name__ == "__main__":
    main()
