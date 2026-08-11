"""
Explorador da BaseLinker — SO LE, nao grava nada em lugar nenhum.

Mesma ideia do shopee_explorar.py e do explorar_bling_contas_pagar.py: antes de
escrever o robo de verdade, a gente olha o que a API devolve de fato.

Imprime:
  1. os catalogos (inventory_id) da conta
  2. os depositos (warehouse_id) de cada catalogo
  3. a estrutura CRUA de 2 produtos (para confirmarmos os nomes dos campos)
  4. a estrutura CRUA do estoque de 2 produtos
  5. um resumo: total de produtos, quantos tem EAN, quantos sao kit

O item 5 e' o que mais interessa agora: quantos produtos da Base ja tem codigo
de barras cadastrado. E' esse numero que diz se a contagem vai ser rapida.

Secret necessario no GitHub Actions:
  BASELINKER_TOKEN
"""

import json
import os
import time

import requests

API = "https://api.baselinker.com/connector.php"
TOKEN = os.environ["BASELINKER_TOKEN"]
PAUSA = 0.7          # limite da BaseLinker: 100 chamadas/min. 0,7s da ~85/min.

_ultima = [0.0]


def chamar(metodo, parametros=None, tentativas=4):
    """Uma chamada a API. Respeita o limite e tenta de novo em erro passageiro.

    A BaseLinker usa UM endpoint so: o metodo vai no corpo, nao na URL.
    Resposta sempre traz 'status': SUCCESS ou ERROR.
    """
    for i in range(tentativas):
        espera = PAUSA - (time.time() - _ultima[0])
        if espera > 0:
            time.sleep(espera)
        _ultima[0] = time.time()
        try:
            r = requests.post(
                API,
                headers={"X-BLToken": TOKEN},
                data={"method": metodo, "parameters": json.dumps(parametros or {})},
                timeout=60,
            )
        except Exception as e:
            print(f"  rede falhou ({e}), tentativa {i+1}")
            time.sleep(2 * (i + 1))
            continue

        if r.status_code == 429 or r.status_code >= 500:
            print(f"  HTTP {r.status_code}, tentativa {i+1}")
            time.sleep(2 * (i + 1))
            continue

        try:
            d = r.json()
        except Exception:
            print(f"  resposta nao e' JSON: {r.text[:200]}")
            return None

        if d.get("status") == "ERROR":
            print(f"  ERRO da BaseLinker em {metodo}: "
                  f"{d.get('error_code')} - {d.get('error_message')}")
            return None
        return d
    return None


def amostra(rotulo, obj, n=2):
    """Imprime os primeiros itens de um dicionario/lista, bem formatado."""
    print(f"\n--- {rotulo} ---")
    if isinstance(obj, dict):
        for i, (k, v) in enumerate(obj.items()):
            if i >= n:
                break
            print(f"  chave: {k}")
            print("  " + json.dumps(v, ensure_ascii=False, indent=2)[:1500])
    elif isinstance(obj, list):
        for v in obj[:n]:
            print("  " + json.dumps(v, ensure_ascii=False, indent=2)[:1500])
    else:
        print("  " + str(obj)[:1000])


def main():
    print("=" * 70)
    print("1) CATALOGOS")
    print("=" * 70)
    d = chamar("getInventories")
    if not d:
        print("Nao consegui listar os catalogos. Confira o BASELINKER_TOKEN.")
        return
    inventarios = d.get("inventories") or []
    for inv in inventarios:
        print(f"  inventory_id={inv.get('inventory_id')}  "
              f"nome={inv.get('name')}  padrao={inv.get('is_default')}")
    if not inventarios:
        print("  Nenhum catalogo retornado.")
        return

    # usa o catalogo padrao, ou o primeiro
    alvo = next((i for i in inventarios if i.get("is_default")), inventarios[0])
    inv_id = alvo.get("inventory_id")
    print(f"\n>>> Vou explorar o catalogo {inv_id} ({alvo.get('name')})")

    print("\n" + "=" * 70)
    print("2) DEPOSITOS")
    print("=" * 70)
    d = chamar("getInventoryWarehouses", {"inventory_id": inv_id})
    if d:
        amostra("warehouses (cru)", d.get("warehouses"), n=10)

    print("\n" + "=" * 70)
    print("3) PRODUTOS — estrutura crua da pagina 1")
    print("=" * 70)
    d = chamar("getInventoryProductsList", {"inventory_id": inv_id, "page": 1})
    if not d:
        print("Nao consegui listar produtos.")
        return
    produtos = d.get("products") or {}
    print(f"  Tipo do campo 'products': {type(produtos).__name__}")
    print(f"  Itens nesta pagina: {len(produtos)}")
    amostra("produtos (cru)", produtos, n=2)

    print("\n" + "=" * 70)
    print("4) ESTOQUE — estrutura crua da pagina 1")
    print("=" * 70)
    d2 = chamar("getInventoryProductsStock", {"inventory_id": inv_id, "page": 1})
    if d2:
        est = d2.get("products") or {}
        print(f"  Tipo do campo 'products': {type(est).__name__}")
        print(f"  Itens nesta pagina: {len(est)}")
        amostra("estoque (cru)", est, n=2)

    print("\n" + "=" * 70)
    print("5) RESUMO — o numero que interessa: quantos tem EAN")
    print("=" * 70)
    total = 0
    com_ean = 0
    com_sku = 0
    pagina = 1
    while True:
        d = chamar("getInventoryProductsList", {"inventory_id": inv_id, "page": pagina})
        if not d:
            break
        lote = d.get("products") or {}
        itens = lote.values() if isinstance(lote, dict) else lote
        n = 0
        for p in itens:
            n += 1
            total += 1
            ean = str(p.get("ean") or "").strip()
            sku = str(p.get("sku") or "").strip()
            if ean and ean != "0":
                com_ean += 1
            if sku:
                com_sku += 1
        if n == 0:
            break
        print(f"  pagina {pagina}: {n} produtos (acumulado {total})")
        pagina += 1
        if pagina > 50:      # trava de seguranca
            print("  parei em 50 paginas por seguranca")
            break

    pct = (com_ean / total * 100) if total else 0
    pct_sku = (com_sku / total * 100) if total else 0
    print(f"\n  TOTAL DE PRODUTOS ........ {total}")
    print(f"  COM EAN CADASTRADO ....... {com_ean}  ({pct:.1f}%)")
    print(f"  COM SKU CADASTRADO ....... {com_sku}  ({pct_sku:.1f}%)")
    print("\n  Se a cobertura de EAN estiver baixa, a contagem vai depender muito")
    print("  da busca por nome no comeco — e cada vinculo feito no app aumenta")
    print("  essa cobertura para a proxima vez.")


if __name__ == "__main__":
    main()
