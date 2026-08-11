"""
Cliente compartilhado da API BaseLinker.

Mesma ideia do ml_auth.py: os dois puxadores (produtos e estoque) usam daqui,
para nao ter duas versoes da mesma regra espalhadas.

Regras respeitadas (as mesmas dos robos do Ideris):
  - freio entre chamadas para nao estourar o limite (100/min na BaseLinker)
  - retry em 429 e 5xx, com espera crescente
  - erro da API nao derruba a rodada: devolve None e quem chamou decide
  - nada de segredo no codigo: o token vem do ambiente

Secret necessario:
  BASELINKER_TOKEN
"""

import json
import os
import time

import requests

API = "https://api.baselinker.com/connector.php"
PAUSA = float(os.environ.get("BASE_PAUSA", "0.7"))   # 0,7s = ~85 chamadas/min

_token = None
_ultima = [0.0]


def token():
    global _token
    if _token is None:
        _token = os.environ["BASELINKER_TOKEN"]
    return _token


def chamar(metodo, parametros=None, tentativas=4):
    """Devolve o dict da resposta, ou None se falhou de vez.

    A BaseLinker usa um endpoint unico: o metodo vai no corpo do POST.
    Toda resposta traz 'status' = SUCCESS ou ERROR.
    """
    ultimo_erro = ""
    for i in range(tentativas):
        espera = PAUSA - (time.time() - _ultima[0])
        if espera > 0:
            time.sleep(espera)
        _ultima[0] = time.time()

        try:
            r = requests.post(
                API,
                headers={"X-BLToken": token()},
                data={"method": metodo, "parameters": json.dumps(parametros or {})},
                timeout=60,
            )
        except Exception as e:
            ultimo_erro = f"rede: {e}"
            time.sleep(2 * (i + 1))
            continue

        if r.status_code == 429 or r.status_code >= 500:
            ultimo_erro = f"HTTP {r.status_code}"
            time.sleep(2 * (i + 1))
            continue

        try:
            d = r.json()
        except Exception:
            ultimo_erro = f"resposta nao-JSON: {r.text[:200]}"
            break

        if d.get("status") == "ERROR":
            ultimo_erro = f"{d.get('error_code')} - {d.get('error_message')}"
            # erro de negocio nao adianta repetir
            break
        return d

    print(f"  ! falha em {metodo}: {ultimo_erro}")
    return None


def catalogo_padrao():
    """inventory_id do catalogo padrao (ou o primeiro). None se nao achar."""
    d = chamar("getInventories")
    if not d:
        return None, None
    invs = d.get("inventories") or []
    if not invs:
        return None, None
    alvo = next((i for i in invs if i.get("is_default")), invs[0])
    return alvo.get("inventory_id"), alvo.get("name")


def paginar(metodo, inventory_id, limite_paginas=60):
    """Percorre as paginas de um metodo que devolve 'products'.

    A BaseLinker devolve 1000 por pagina no catalogo Base. Para de verdade
    quando a pagina vier vazia. O limite de paginas e' so trava de seguranca.
    """
    pagina = 1
    while pagina <= limite_paginas:
        d = chamar(metodo, {"inventory_id": inventory_id, "page": pagina})
        if not d:
            # falha de rede/API no meio: para e devolve o que ja veio.
            # Quem chamou decide se grava ou nao (regra do Ideris: se veio
            # pouco demais, nao grava nada).
            return
        lote = d.get("products") or {}
        itens = list(lote.items()) if isinstance(lote, dict) else [
            (str(p.get("id")), p) for p in lote
        ]
        if not itens:
            return
        yield pagina, itens
        pagina += 1
