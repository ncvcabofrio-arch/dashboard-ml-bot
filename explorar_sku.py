"""
Explorador (SÓ LEITURA) — descobre o endpoint de SKU/Produto do Ideris e o
campo de estoque correto, pra a gente trocar a fonte do robô de estoque.

Não grava nada e não manda Telegram; só imprime no log do GitHub Actions.
Testa vários caminhos candidatos e, nos que responderem 200, mostra:
  - os NOMES dos campos do primeiro item,
  - e os campos que parecem de estoque (quantity, stock, estoque, saldo, etc.).
"""

import os
import json
import time
import requests

IDERIS_TOKEN = os.environ["IDERIS_TOKEN"]
BASE = "https://apiv3.ideris.com.br"

# caminhos candidatos p/ a "puxada de SKU/Produto" (padrão /{recurso}/search)
CANDIDATOS = [
    "/product/search",
    "/products/search",
    "/sku/search",
    "/skus/search",
    "/productSku/search",
    "/produto/search",
    "/stock/search",
    "/inventory/search",
    "/listingModelSku/search",
]

PALAVRAS_ESTOQUE = ("quant", "stock", "estoque", "saldo", "available", "amount", "qtd", "inventory")


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


def campos_estoque(item, prefixo=""):
    """Acha (chave, valor) que parecem estoque, inclusive dentro de listas/objetos (1 nível)."""
    achados = []
    if isinstance(item, dict):
        for k, v in item.items():
            nome = f"{prefixo}{k}"
            if any(p in k.lower() for p in PALAVRAS_ESTOQUE) and not isinstance(v, (dict, list)):
                achados.append((nome, v))
            elif isinstance(v, dict):
                achados += campos_estoque(v, nome + ".")
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                achados += campos_estoque(v[0], nome + "[].")
    return achados


def main():
    token = login()
    H = {"Authorization": "Bearer " + token}
    print("Login OK. Testando endpoints candidatos...\n")

    achou_algum = False
    for path in CANDIDATOS:
        url = BASE + path + "?limit=2&offset=0"
        try:
            r = requests.get(url, headers=H, timeout=40)
        except Exception as e:
            print(f"[erro rede] {path}: {e}")
            time.sleep(1.3)
            continue

        if r.status_code != 200:
            print(f"[{r.status_code}] {path}  -> {r.text[:120]}")
            time.sleep(1.3)
            continue

        achou_algum = True
        data = r.json()
        total = data.get("total")
        obj = data.get("obj", []) or []
        print("=" * 70)
        print(f"[200] {path}   (total de registros: {total})")
        if obj:
            item = obj[0]
            print("  Campos do 1º item:", sorted(item.keys()))
            est = campos_estoque(item)
            if est:
                print("  >>> Campos que parecem ESTOQUE:")
                for nome, val in est:
                    print(f"        {nome} = {val!r}")
            else:
                print("  (nenhum campo óbvio de estoque no 1º item)")
            print("  Exemplo do 1º item (resumido):")
            resumo = {k: item.get(k) for k in list(item.keys())[:12]}
            print("   ", json.dumps(resumo, ensure_ascii=False)[:600])
        else:
            print("  (respondeu 200 mas sem itens)")
        print("=" * 70 + "\n")
        time.sleep(1.3)

    if not achou_algum:
        print("\nNenhum dos caminhos candidatos respondeu 200. "
              "Me diga como se chama a 'puxada de SKU' no seu Ideris que eu ajusto a lista.")


if __name__ == "__main__":
    main()
