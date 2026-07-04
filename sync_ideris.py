"""
Sincronizador Ideris -> Supabase (roda 1x/dia).
Atualiza CUSTO, NOME e MODELO de cada produto.

- Login no Ideris (POST /login com o token como texto puro)
- CUSTO e MODELO: vêm dos modelos de anúncio (/listingModel/search).
- NOME: vem do endpoint de PRODUTO (/sku/search), que é a fonte completa
  (cobre até produtos sem anúncio ativo). O /listingModel serve só de reforço.
- Congela o custo nas vendas que ainda estao sem (funcao backfill_custos)

Robustez: se uma página do /listingModel der erro 500 (algum registro com valor
inválido, ex.: '300O'), o robô NÃO aborta — varre aquele trecho de 1 em 1 e pula
só o registro ruim. Erros 404 (fim da lista / registros-fantasma) são ignorados
sem varredura.

OBS.: o ESTOQUE não é mais atualizado aqui — quem cuida disso é o 'sync_estoque.py'
(de 2 em 2h, pela mesma fonte /sku/search).
"""

import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client

IDERIS_TOKEN = os.environ["IDERIS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

BASE = "https://apiv3.ideris.com.br"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


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


# campos onde o nome do produto pode estar no modelo de anúncio do Ideris.
# tenta nesta ordem e usa o primeiro que vier preenchido.
CAMPOS_NOME = ["title", "name", "productName", "nome", "modelName",
               "listingName", "product_title", "description"]


def extrair_nome(item):
    for c in CAMPOS_NOME:
        v = item.get(c)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _buscar(H, offset, limit, endpoint="/listingModel/search"):
    """Uma chamada à busca do Ideris. Devolve o objeto Response."""
    return requests.get(
        BASE + f"{endpoint}?limit={limit}&offset={offset}",
        headers=H, timeout=60)


def _processar(batch, custos, nomes, modelos):
    """Extrai custo/nome/modelo de cada item e guarda por sku."""
    for item in batch:
        sku = item.get("sku")
        if not sku:
            continue
        if item.get("cost") is not None:
            custos[sku] = item.get("cost")
        nome = extrair_nome(item)
        if nome:
            nomes[sku] = nome
        modelo = item.get("model")
        if isinstance(modelo, str) and modelo.strip():
            modelos[sku] = modelo.strip()
        # guarda o último sku lido OK, pra servir de "vizinho" quando algo falhar
        _processar.ultimo_sku_ok = sku


_processar.ultimo_sku_ok = None


def coletar(token):
    """Retorna dicts: custos, nomes e modelos (todos {sku: valor}).

    Se uma página der erro (algum registro do Ideris com valor inválido, ex.: '300O'),
    NÃO aborta tudo: varre aquele trecho de 1 em 1 e pula só o registro ruim,
    coletando todo o resto.
    """
    H = {"Authorization": "Bearer " + token}
    custos, nomes, modelos = {}, {}, {}
    offset, total, limit = 0, None, 100
    mostrou_campos = False
    puladas = []                      # info dos registros que o Ideris não consegue devolver
    while total is None or offset < total:
        resp = _buscar(H, offset, limit)

        if resp.status_code == 404:
            # fim da lista / registros-fantasma do Ideris — nada a recuperar aqui.
            # pula a janela inteira, sem varrer de 1 em 1.
            print(f"Aviso: 404 em offset={offset} (fim da lista/registros vazios). Pulando.")
            offset += limit
            continue

        if resp.status_code != 200:
            # 500 e afins: a página inteira falhou por causa de UM registro quebrado
            # (ex.: valor '300O'). Varre o trecho de 1 em 1 pra pular só o ruim.
            print(f"Aviso: pagina falhou em offset={offset} (status {resp.status_code}). "
                  f"Varrendo 1 a 1 pra pular so o registro ruim...")
            fim = (offset + limit) if total is None else min(offset + limit, total)
            for off1 in range(offset, fim):
                sku_antes = _processar.ultimo_sku_ok
                r1 = _buscar(H, off1, 1)
                time.sleep(1.3)
                if r1.status_code == 404:
                    continue  # registro-fantasma isolado, ignora em silêncio
                if r1.status_code != 200:
                    detalhe = r1.text[:200]
                    puladas.append({"offset": off1, "sku_anterior": sku_antes, "detalhe": detalhe})
                    print(f"  ⚠️ registro invalido no Ideris (offset {off1}, "
                          f"logo depois do sku '{sku_antes}'): {detalhe}")
                    continue
                d1 = r1.json()
                if total is None:
                    total = d1.get("total", 0)
                _processar(d1.get("obj", []) or [], custos, nomes, modelos)
            offset = fim
            continue

        data = resp.json()
        total = data.get("total", 0)
        batch = data.get("obj", []) or []
        if not batch:
            break
        if not mostrou_campos:
            # debug (1x): mostra os campos disponíveis para confirmarmos o nome
            print("Campos do modelo Ideris:", sorted(batch[0].keys()))
            mostrou_campos = True
        _processar(batch, custos, nomes, modelos)
        offset += len(batch)
        time.sleep(1.3)               # respeita o limite (50 chamadas/min)

    if puladas:
        print(f"⚠️ {len(puladas)} registro(s) pulado(s) por erro no Ideris — CORRIJA no Ideris:")
        for p in puladas:
            print(f"   - offset {p['offset']}, logo depois do sku '{p['sku_anterior']}' | {p['detalhe']}")
    print(f"Coletado (anúncios): {len(custos)} custos, "
          f"{len(nomes)} nomes e {len(modelos)} modelos (de {total} modelos)")
    return custos, nomes, modelos


def coletar_nomes_sku(token):
    """NOME pela fonte completa /sku/search (nível do produto).

    Cobre inclusive produtos sem anúncio ativo — que é o que faltava no
    /listingModel. Devolve {sku: title}.
    """
    H = {"Authorization": "Bearer " + token}
    nomes = {}
    offset, total, limit = 0, None, 100
    while total is None or offset < total:
        resp = _buscar(H, offset, limit, endpoint="/sku/search")
        if resp.status_code == 404:
            print(f"Aviso(sku): 404 em offset={offset} (fim/vazios). Pulando.")
            offset += limit
            continue
        if resp.status_code != 200:
            print(f"Aviso(sku): falha em offset={offset} (status {resp.status_code}). Parando a busca de nomes.")
            break
        data = resp.json()
        total = data.get("total", 0)
        batch = data.get("obj", []) or []
        if not batch:
            break
        for item in batch:
            sku = item.get("sku")
            if not sku:
                continue
            nome = extrair_nome(item)   # /sku/search usa 'title', já coberto em CAMPOS_NOME
            if nome:
                nomes[sku] = nome
        offset += len(batch)
        time.sleep(1.3)
    print(f"Coletado (produtos /sku/search): {len(nomes)} nomes (de {total} SKUs)")
    return nomes


def main():
    token = login()
    custos, nomes_anuncio, modelos = coletar(token)

    # NOME vem da fonte completa (produtos); anúncios servem só de reforço.
    nomes_sku = coletar_nomes_sku(token)
    nomes = {**nomes_anuncio, **nomes_sku}   # /sku/search tem prioridade
    print(f"Nomes totais após juntar as duas fontes: {len(nomes)}")

    if not custos and not nomes:
        print("⚠️ Nada coletado. Me avise para ajustar.")
        return

    # 2) atualiza CUSTO (não toca no estoque)
    linhas_custo = [{"sku": s, "custo": c} for s, c in custos.items()]
    for i in range(0, len(linhas_custo), 200):
        sb.table("produtos").upsert(linhas_custo[i:i + 200], on_conflict="sku").execute()
    print(f"Custo atualizado: {len(linhas_custo)} produtos")

    # 3) atualiza NOME com o nome exato do Ideris
    linhas_nome = [{"sku": s, "nome": n} for s, n in nomes.items()]
    for i in range(0, len(linhas_nome), 200):
        sb.table("produtos").upsert(linhas_nome[i:i + 200], on_conflict="sku").execute()
    print(f"Nome atualizado: {len(linhas_nome)} produtos")

    # 3b) atualiza MODELO com o campo 'model' do Ideris
    linhas_modelo = [{"sku": s, "modelo": m} for s, m in modelos.items()]
    for i in range(0, len(linhas_modelo), 200):
        sb.table("produtos").upsert(linhas_modelo[i:i + 200], on_conflict="sku").execute()
    print(f"Modelo atualizado: {len(linhas_modelo)} produtos")

    # 4) congela o custo nas vendas que ainda estão sem (nunca sobrescreve)
    try:
        sb.rpc("backfill_custos").execute()
        print("Backfill de custo nas vendas concluído.")
    except Exception as e:
        print("Aviso: backfill falhou:", e)


if __name__ == "__main__":
    main()
