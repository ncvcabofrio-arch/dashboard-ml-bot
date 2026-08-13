"""
Cliente da API BaseLinker (https://api.baselinker.com/).

Substitui a camada de acesso ao Ideris. Tudo na BaseLinker e' um POST unico
para /connector.php com:
    header  X-BLToken: <seu token>
    body    method=<nome>&parameters=<json>

Diferencas importantes em relacao ao Ideris:
  - Erro NAO vem por status HTTP. Vem 200 com {"status": "ERROR", ...}.
  - Limite: 100 chamadas/minuto (o Ideris eram 50). Ver PAUSA abaixo.
  - Paginacao e' por 'page' (1000 registros por pagina), nao por offset.
  - Nao existe endpoint de login: o token e' fixo e vai no header.
"""

import json
import os
import time

import requests

BASE = "https://api.baselinker.com/connector.php"

# 100 req/min = 0.60s. Deixo 0.65 de folga.
PAUSA = float(os.environ.get("BL_PAUSA", "0.65"))

# Quantos IDs de produto por chamada de getInventoryProductsData.
CHUNK_DADOS = int(os.environ.get("BL_CHUNK_DADOS", "500"))


class BLError(Exception):
    """Erro devolvido pela propria BaseLinker (status=ERROR)."""

    def __init__(self, code, message, method=None):
        self.code = code or ""
        self.message = message or ""
        self.method = method
        super().__init__(f"{method}: {self.code} - {self.message}")


# Erros que valem a pena tentar de novo (transitorios).
RETRIAVEIS = {"ERROR_RATE_LIMIT", "ERROR_INTERNAL", "ERROR_UNKNOWN"}


class BaseLinker:
    def __init__(self, token=None, pausa=PAUSA):
        self.token = token or os.environ["BASELINKER_TOKEN"]
        self.pausa = pausa
        self.s = requests.Session()
        self.s.headers.update({"X-BLToken": self.token})
        self._ultima = 0.0

    # ------------------------------------------------------------------ base

    def _espera(self):
        delta = time.time() - self._ultima
        if delta < self.pausa:
            time.sleep(self.pausa - delta)
        self._ultima = time.time()

    def call(self, method, parameters=None, tentativas=4):
        """Uma chamada a API. Devolve o dict de resposta ja validado.

        Levanta BLError quando a BaseLinker responde status=ERROR de forma
        definitiva (SKU inexistente, parametro invalido, etc.).
        """
        body = {"method": method, "parameters": json.dumps(parameters or {})}
        ultimo = None

        for t in range(tentativas):
            self._espera()
            try:
                r = self.s.post(BASE, data=body, timeout=90)
            except requests.RequestException as e:
                ultimo = BLError("REDE", str(e), method)
                time.sleep(2 * (t + 1))
                continue

            if r.status_code == 429 or r.status_code >= 500:
                ultimo = BLError(f"HTTP_{r.status_code}", r.text[:200], method)
                time.sleep(3 * (t + 1))
                continue

            if r.status_code != 200:
                raise BLError(f"HTTP_{r.status_code}", r.text[:300], method)

            try:
                data = r.json()
            except ValueError:
                ultimo = BLError("JSON_INVALIDO", r.text[:200], method)
                time.sleep(2 * (t + 1))
                continue

            if data.get("status") == "SUCCESS":
                return data

            code = data.get("error_code", "")
            msg = data.get("error_message", "")
            erro = BLError(code, msg, method)
            if code in RETRIAVEIS:
                ultimo = erro
                time.sleep(3 * (t + 1))
                continue
            raise erro

        raise ultimo or BLError("DESCONHECIDO", "sem resposta", method)

    # ------------------------------------------------------------- catalogos

    def inventories(self):
        return self.call("getInventories").get("inventories", []) or []

    def price_groups(self):
        return self.call("getInventoryPriceGroups").get("price_groups", []) or []

    def warehouses(self):
        return self.call("getInventoryWarehouses").get("warehouses", []) or []

    def manufacturers(self):
        return self.call("getInventoryManufacturers").get("manufacturers", []) or []

    def extra_fields(self, inventory_id):
        return self.call(
            "getInventoryExtraFields", {"inventory_id": inventory_id}
        ).get("extra_fields", []) or []

    def inventory_id_padrao(self):
        """Resolve o catalogo: BL_INVENTORY_ID no ambiente, ou o marcado
        como is_default, ou o primeiro da lista."""
        env = os.environ.get("BL_INVENTORY_ID")
        if env:
            return int(env)
        invs = self.inventories()
        if not invs:
            raise BLError("SEM_CATALOGO", "conta sem catalogos de produtos")
        for i in invs:
            if i.get("is_default"):
                return int(i["inventory_id"])
        return int(invs[0]["inventory_id"])

    # -------------------------------------------------------------- produtos

    def lista_produtos(self, inventory_id, include_variants=True, verbose=True):
        """Percorre getInventoryProductsList pagina a pagina.

        Devolve dict {product_id(str): {id, sku, ean, name, prices, stock}}.
        Com include_variants=True as variantes entram como itens proprios,
        com seu proprio sku e com parent_id preenchido.
        """
        out = {}
        page = 1
        while True:
            data = self.call(
                "getInventoryProductsList",
                {
                    "inventory_id": inventory_id,
                    "page": page,
                    "include_variants": bool(include_variants),
                },
            )
            lote = data.get("products", {}) or {}
            if not lote:
                break
            out.update({str(k): v for k, v in lote.items()})
            if verbose:
                print(f"  lista: pagina {page} -> {len(lote)} itens "
                      f"(acumulado {len(out)})")
            if len(lote) < 1000:
                break
            page += 1
        return out

    def dados_produtos(self, inventory_id, ids, **flags):
        """getInventoryProductsData em lotes, com tolerancia a lote quebrado.

        Se um lote falhar (registro invalido no catalogo), divide o lote ao
        meio recursivamente ate isolar e pular somente o produto ruim --
        mesma ideia da varredura 1 a 1 do robo do Ideris, so que em log2.

        Devolve (produtos, pulados).
        """
        ids = [int(i) for i in ids]
        produtos, pulados = {}, []

        for i in range(0, len(ids), CHUNK_DADOS):
            bloco = ids[i:i + CHUNK_DADOS]
            self._dados_bloco(inventory_id, bloco, produtos, pulados, flags)
            print(f"  dados: {len(produtos)} produtos lidos "
                  f"({min(i + CHUNK_DADOS, len(ids))}/{len(ids)})")

        return produtos, pulados

    def _dados_bloco(self, inventory_id, bloco, produtos, pulados, flags):
        if not bloco:
            return
        params = {"inventory_id": inventory_id, "products": bloco}
        params.update(flags)
        try:
            data = self.call("getInventoryProductsData", params, tentativas=2)
        except BLError as e:
            if len(bloco) == 1:
                pulados.append({"product_id": bloco[0], "detalhe": str(e)})
                print(f"  ⚠️ produto {bloco[0]} nao pode ser lido: {e}")
                return
            meio = len(bloco) // 2
            self._dados_bloco(inventory_id, bloco[:meio], produtos, pulados, flags)
            self._dados_bloco(inventory_id, bloco[meio:], produtos, pulados, flags)
            return
        produtos.update({str(k): v for k, v in (data.get("products") or {}).items()})

    def estoque_produtos(self, inventory_id, verbose=True):
        """getInventoryProductsStock paginado.
        Devolve {product_id(str): {stock, reservations, variants, ...}}."""
        out = {}
        page = 1
        while True:
            data = self.call(
                "getInventoryProductsStock",
                {"inventory_id": inventory_id, "page": page},
            )
            lote = data.get("products", {}) or {}
            if not lote:
                break
            out.update({str(k): v for k, v in lote.items()})
            if verbose:
                print(f"  estoque: pagina {page} -> {len(lote)} itens "
                      f"(acumulado {len(out)})")
            if len(lote) < 1000:
                break
            page += 1
        return out


# ------------------------------------------------------------------ utilidades

def num(v):
    """Converte para float aceitando '1234.56', '1.234,56', 'R$ 12,00'.
    Devolve 0.0 no que nao der para ler -- mesma politica do robo do Ideris:
    valor duvidoso nunca sobrescreve um custo bom, porque quem grava so
    grava quando for > 0."""
    if isinstance(v, bool) or v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(" ", "").replace("R$", "")
        for tentativa in (t, t.replace(".", "").replace(",", ".")):
            try:
                return float(tentativa)
            except ValueError:
                pass
    return 0.0


def texto(text_fields, chave, idioma=None):
    """Le um campo de text_fields, tolerando as chaves com idioma
    ('name', 'name|pt', 'name|de|amazon_0')."""
    if not isinstance(text_fields, dict):
        return None
    if idioma:
        v = text_fields.get(f"{chave}|{idioma}")
        if isinstance(v, str) and v.strip():
            return v.strip()
    v = text_fields.get(chave)
    if isinstance(v, str) and v.strip():
        return v.strip()
    # fallback: primeira variante do campo com qualquer idioma
    for k, val in text_fields.items():
        if k == chave or k.startswith(chave + "|"):
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _norm(s):
    """Normaliza para comparar nome de caracteristica sem sofrer com
    acento, maiuscula ou espaco sobrando ('Modelo Detalhado' == 'modelo detalhado')."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def feature(text_fields, nome, idioma=None):
    """Le uma caracteristica da FICHA TECNICA.

    Na BaseLinker a ficha tecnica mora em text_fields['features'], como um
    dicionario {nome_da_caracteristica: valor}, por exemplo:
        "features": {"Cor": "Preto", "Modelo detalhado": "SM57-LC"}
    A chave pode vir com idioma ('features|br'), por isso a varredura.
    """
    if not isinstance(text_fields, dict):
        return None
    alvo = _norm(nome)
    chaves = ["features"]
    if idioma:
        chaves.insert(0, f"features|{idioma}")
    chaves += [k for k in text_fields if k == "features" or k.startswith("features|")]

    for ch in chaves:
        bloco = text_fields.get(ch)
        if not isinstance(bloco, dict):
            continue
        for k, v in bloco.items():
            if _norm(k) == alvo and isinstance(v, str) and v.strip():
                return v.strip()
    return None
