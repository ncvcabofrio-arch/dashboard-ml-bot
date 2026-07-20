"""
Exporta o cadastro COMPLETO de produtos do Ideris -> CSV + XLSX.

Junta as duas fontes do Ideris, UMA LINHA POR SKU, com TODOS os campos:
  - /sku/search          -> produto (nome oficial; cobre ate SKU sem anuncio ativo)  -> colunas 'produto_*'
  - /listingModel/search -> modelo de anuncio (custo, modelo, precos, dimensoes,
                            garantia, estoque, descricao, tech spec, etc.)          -> colunas 'anuncio_*'

Robustez (igual ao sync_ideris): se uma pagina do Ideris der 500 (registro com
valor invalido, ex.: '300O'), varre de 1 em 1 e pula so o ruim; se der 404 mas
ainda faltarem registros, varre 1 a 1 pra NAO perder os produtos mais NOVOS.

Gera dois arquivos na pasta atual:
  ideris_produtos.csv   (separador ';', UTF-8 com BOM -> abre certo no Excel PT-BR)
  ideris_produtos.xlsx
"""
import os
import time
import csv
import json
import requests
from openpyxl import Workbook

IDERIS_TOKEN = os.environ["IDERIS_TOKEN"]
BASE = "https://apiv3.ideris.com.br"


def login():
    r = requests.post(BASE + "/login", json=IDERIS_TOKEN, timeout=30)
    try:
        j = r.json()
        tok = j if isinstance(j, str) else (j.get("token") or j.get("obj") or j)
    except Exception:
        tok = r.text.strip().strip('"')
    if r.status_code != 200 or not tok:
        raise RuntimeError("Falha no login Ideris: " + str(r.status_code) + " " + r.text[:200])
    return str(tok)


def _buscar(H, offset, limit, endpoint):
    return requests.get(BASE + f"{endpoint}?limit={limit}&offset={offset}", headers=H, timeout=60)


def _guardar(item, out, mult):
    sku = item.get("sku")
    if not sku:
        return
    mult[sku] = mult.get(sku, 0) + 1
    if sku not in out:        # o primeiro registro do SKU manda
        out[sku] = item


def coletar_tudo(token, endpoint):
    """Devolve ({sku: item_completo}, {sku: qtd_de_registros}).

    Paginacao blindada:
      - offset avanca por JANELA (limit), nao por len(batch) -> nao pula/duplica
        posicao quando o Ideris tem registros-fantasma.
      - 404 com registros ainda faltando -> varre 1 a 1 (nao perde os produtos NOVOS).
      - 500 (registro quebrado) -> varre 1 a 1 e pula so o ruim.
      - janela vazia no meio -> continua (nao para cedo), com trava contra loop.
    """
    H = {"Authorization": "Bearer " + token}
    out, mult = {}, {}
    offset, total, limit = 0, None, 100
    while total is None or offset < total:
        resp = _buscar(H, offset, limit, endpoint)

        if resp.status_code == 404:
            # 404 pode vir numa janela que AINDA tem registros (fim da lista do Ideris).
            if total is not None and offset < total:
                fim = min(offset + limit, total)
                for off1 in range(offset, fim):
                    r1 = _buscar(H, off1, 1, endpoint)
                    time.sleep(1.3)
                    if r1.status_code == 200:
                        for it in (r1.json().get("obj", []) or []):
                            _guardar(it, out, mult)
                offset = fim
                continue
            break   # 404 de verdade = fim da lista

        if resp.status_code != 200:
            # 500 e afins: um registro quebrado derruba a pagina inteira -> varre 1 a 1
            print(f"  {endpoint}: pagina falhou em offset={offset} (status {resp.status_code}); varrendo 1 a 1...")
            fim = (offset + limit) if total is None else min(offset + limit, total)
            for off1 in range(offset, fim):
                r1 = _buscar(H, off1, 1, endpoint)
                time.sleep(1.3)
                if r1.status_code != 200:
                    continue
                d1 = r1.json()
                if total is None:
                    total = d1.get("total", 0)
                for it in (d1.get("obj", []) or []):
                    _guardar(it, out, mult)
            offset = fim
            continue

        data = resp.json()
        total = data.get("total", 0)
        batch = data.get("obj", []) or []
        if not batch:
            break
        for it in batch:
            _guardar(it, out, mult)
        offset += len(batch)          # avanca pelo que REALMENTE veio (Ideris manda ~50/pag)
        time.sleep(1.3)               # respeita o limite (50 chamadas/min)

    print(f"{endpoint}: {len(out)} SKUs coletados (de {total}).")
    return out, mult


def _celula(v):
    """Achata listas/dicts em texto JSON pra caber na celula."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:2000]
    return v


import re

# endpoints prováveis da tabela de NCM no Ideris (tenta na ordem; usa o 1º que responder)
NCM_ENDPOINTS = ["/ncm/search", "/ncm", "/tax/ncm/search", "/fiscal/ncm/search", "/product/ncm/search"]


def _achar_codigo_ncm(it):
    """Descobre, dentro do registro de NCM, qual campo tem o CÓDIGO (6-10 dígitos)."""
    for k in ("ncm", "code", "codigo", "number", "value", "name", "description"):
        v = it.get(k)
        if v is not None:
            so_digitos = re.sub(r"\D", "", str(v))
            if 6 <= len(so_digitos) <= 10:
                return str(v)
    for v in it.values():                 # fallback: qualquer campo com 8 dígitos
        if v is not None and len(re.sub(r"\D", "", str(v))) == 8:
            return str(v)
    return None


def resolver_ncm(token):
    """Monta {id_do_ncm: codigo}. Tenta os endpoints prováveis e loga qual funcionou."""
    H = {"Authorization": "Bearer " + token}
    for ep in NCM_ENDPOINTS:
        try:
            r = requests.get(BASE + ep + "?limit=100&offset=0", headers=H, timeout=60)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            d = r.json()
        except Exception:
            continue
        primeiro = (d.get("obj") if isinstance(d, dict) else d) or []
        if not primeiro:
            continue
        print(f"[NCM] endpoint OK: {ep} | campos do registro: {sorted(primeiro[0].keys())}")
        mp = {}
        offset, total, limit = 0, None, 100
        while total is None or offset < total:
            rr = requests.get(BASE + ep + f"?limit={limit}&offset={offset}", headers=H, timeout=60)
            if rr.status_code != 200:
                break
            dd = rr.json()
            total = dd.get("total", 0) if isinstance(dd, dict) else len(primeiro)
            batch = (dd.get("obj") if isinstance(dd, dict) else dd) or []
            if not batch:
                break
            for it in batch:
                i = it.get("id")
                # doc oficial: NCM tem 'code' (codigo) e 'name' (nome). fallback = detector.
                code = it.get("code") or _achar_codigo_ncm(it)
                if i is not None and code:
                    mp[str(i)] = {"code": str(code), "name": it.get("name") or ""}
            offset += len(batch)
            time.sleep(1.3)
        print(f"[NCM] {len(mp)} códigos mapeados via {ep}")
        return mp
    print("[NCM] nenhum endpoint de NCM respondeu — a coluna 'produto_ncm' fica com o id "
          "(me manda o log que eu acerto o endpoint certo).")
    return {}


def main():
    token = login()
    produtos, _mp = coletar_tudo(token, "/sku/search")
    anuncios, mult = coletar_tudo(token, "/listingModel/search")

    # DIAGNOSTICO (1x): como o Ideris estrutura VARIACAO?
    #  - se cada variacao ja vem como SKU proprio (linha propria) -> nada a fazer.
    #  - se vier ANINHADA num pai (uma lista dentro de um campo 'vari...') -> a
    #    gente expande depois. Isso mostra a estrutura real pra decidir.
    achou = False
    for fonte, dic in (("anuncio", anuncios), ("produto", produtos)):
        for sku, it in dic.items():
            campos_var = {k: v for k, v in it.items()
                          if "vari" in k.lower() and v not in (None, "", [], {})}
            if campos_var:
                print(f"\n[DIAGNOSTICO variacao] fonte={fonte} sku={sku}:")
                print("  " + json.dumps(campos_var, ensure_ascii=False)[:900])
                achou = True
                break
        if achou:
            break
    if not achou:
        print("\n[DIAGNOSTICO variacao] nenhum campo 'vari*' preenchido — "
              "provavelmente cada variacao ja e um SKU proprio (uma linha por variacao).")

    # tabela de NCM: id -> codigo real (ex.: 24 -> "9207.10.10")
    ncm_map = resolver_ncm(token)

    skus = sorted(set(produtos) | set(anuncios))
    linhas = []
    for sku in skus:
        row = {"sku": sku, "anuncios_no_sku": mult.get(sku, 0)}
        for k, v in (produtos.get(sku) or {}).items():
            if k == "sku":
                continue
            row["produto_" + k] = _celula(v)
        # NCM legivel a partir do id (code + nome)
        idncm = str(row.get("produto_ncmId", "") or "").strip()
        _ncm = ncm_map.get(idncm) or {}
        row["produto_ncm"] = _ncm.get("code", "")
        row["produto_ncm_nome"] = _ncm.get("name", "")
        for k, v in (anuncios.get(sku) or {}).items():
            if k == "sku":
                continue
            row["anuncio_" + k] = _celula(v)
        linhas.append(row)

    # colunas = uniao de tudo, com sku primeiro (ordem estavel)
    cols, vistas = ["sku", "anuncios_no_sku"], set()
    vistas.update(cols)
    for r in linhas:
        for k in r:
            if k not in vistas:
                vistas.add(k)
                cols.append(k)

    # CSV (Excel PT-BR: ';' + BOM)
    with open("ideris_produtos.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in linhas:
            w.writerow(r)

    # XLSX
    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos Ideris"
    ws.append(cols)
    for r in linhas:
        ws.append([r.get(c, "") for c in cols])
    ws.freeze_panes = "A2"           # trava o cabecalho
    wb.save("ideris_produtos.xlsx")

    print(f"\nOK: {len(linhas)} produtos x {len(cols)} colunas")
    print("Arquivos: ideris_produtos.csv  e  ideris_produtos.xlsx")


if __name__ == "__main__":
    main()
