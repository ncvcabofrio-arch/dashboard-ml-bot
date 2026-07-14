"""
COLETOR DE MATCH (Fase de mapeamento) — só leitura.
Para uma amostra de anúncios seus, junta os CANDIDATOS de produto de catálogo
(via API sancionada /products/search + /products/{id}/items) e imprime tudo
estruturado (JSONL) pra a IA (Claude) julgar qual é o produto certo, com confiança.

Depois o match confirmado vira uma tabela 'repricer_match' (meu item -> product_id),
e a sonda passa a usar o mapa em vez de re-adivinhar.

Uso: SELLER_ID=<conta>  [LIMITE=15]  [MODO=amostra|sem_match]
  - MODO=amostra (padrão): primeiros LIMITE anúncios ativos (pula estoque 0).
  - MODO=sem_match: só os que a sonda marcou como difíceis (lê de repricer_sugestoes se houver).
Não escreve nada.
"""
import os
import re
import json
import time
import requests
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
import repricer_sugestoes as rec
import repricer_competitivo as comp
from ml_auth import obter_access

SELLER_ID = (os.environ.get("SELLER_ID") or "").strip()
LIMITE = int(os.environ.get("LIMITE", "15"))
MODO = (os.environ.get("MODO") or "amostra").strip().lower()
N_CAND = int(os.environ.get("N_CAND", "6"))   # candidatos de catálogo por item
WORKERS = int(os.environ.get("WORKERS", "8"))


def attrs_do_item(it):
    a = {}
    for x in (it.get("attributes") or []):
        if x.get("id") and x.get("value_name"):
            a[x["id"]] = str(x["value_name"])
    return a


def precos_produto(pid, access):
    st, d = rec.get(f"/products/{pid}/items?limit=50", access)
    precos = []
    for r in ((d.get("results") if isinstance(d, dict) else None) or []):
        try:
            if r.get("price"):
                precos.append(float(r["price"]))
        except (TypeError, ValueError):
            pass
    return sorted(precos)


_VOLT_RE = re.compile(r"^\d{2,3}v$|^bivolt$|^\d{2,3}v[-/]\d{2,3}v$", re.I)   # 220v, bivolt, 110v-120v
_NAO_MODELO = {"b20", "b8", "b10", "b12", "b15", "mg", "ns", "eq"}          # ligas/acabamentos comuns


def _model_code(titulo):
    """Maior token do título que parece CÓDIGO de modelo (letra+dígito), ignorando
    voltagem (220v/bivolt) e ligas/acabamentos (b20, mg...) que davam match errado."""
    best = ""
    for t in re.findall(r"[0-9A-Za-zÀ-ÿ\-]+", titulo or ""):
        tl = t.lower()
        if not (re.search(r"\d", t) and re.search(r"[A-Za-z]", t)):
            continue
        if _VOLT_RE.match(tl) or tl in _NAO_MODELO:
            continue
        if len(t) > len(best):
            best = t
    return best or None


def _variantes_codigo(code):
    """Formas do código pro buscador de catálogo — o ML acha 'ct-x800' também por
    'ctx800' e 'ct x800'. Ex.: 'ct-x800' -> ['ct-x800','ctx800','ct x800','ctx 800']."""
    if not code:
        return []
    c = code.strip()
    base = c.replace("-", "")
    espac = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", base)   # letras|dígitos
    out = []
    for v in (c, base, c.replace("-", " "), espac):
        v = re.sub(r"\s+", " ", v).strip()
        if v and v.lower() not in [o.lower() for o in out]:
            out.append(v)
    return out


def candidatos(reg, access):
    """Junta candidatos de VÁRIAS queries, dedup por pid. Além do título e do EAN,
    tenta o código do modelo em formas normalizadas (com/sem hífen, espaçado) e puro
    (Part Number) — o /products/search aceita q=PART_NUMBER como recurso oficial."""
    marca = (reg.get("marca") or "").strip()
    queries = []
    if reg.get("consulta"):
        queries.append(("q", reg["consulta"]))
    for v in _variantes_codigo(_model_code(reg.get("titulo"))):
        queries.append(("q", f"{marca} {v}".strip()))
        queries.append(("q", v))          # Part Number puro (ML: q=PART_NUMBER)
    if reg.get("gtin"):
        queries.append(("ean", reg["gtin"]))

    vistos, out, qvistos = set(), [], set()
    for tipo, q in queries:
        chave_q = (tipo, str(q).lower())
        if chave_q in qvistos:
            continue                      # não repete a mesma query
        qvistos.add(chave_q)
        if tipo == "ean":
            url = f"/products/search?status=active&site_id=MLB&product_identifier={q}"
        else:
            url = f"/products/search?status=active&site_id=MLB&q={quote(str(q))}"
        st, d = rec.get(url, access)
        for p in ((d.get("results") if isinstance(d, dict) else None) or [])[:15]:
            pid = p.get("id")
            if not pid or pid in vistos:
                continue
            vistos.add(pid)
            if comp._parece_bundle(p.get("name")):
                continue   # bundle (item+extras) -> não é concorrente do item pelado
            ps = precos_produto(pid, access)
            out.append({"pid": pid, "nome": p.get("name"), "via": str(q)[:32],
                        "preco_min": (ps[0] if ps else None),
                        "preco_max": (ps[-1] if ps else None), "n_anuncios": len(ps)})
            if len(out) >= 20:
                break
        if len(out) >= 20:
            break
    return out


# Atributos identificadores (pra busca POST por atributos), em ordem de prioridade.
_ATTR_PRIOR = ["BRAND", "LINE", "MODEL", "ALPHANUMERIC_MODEL", "MODEL_NAME",
               "ITEM_MODEL", "MODEL_CODE", "PART_NUMBER", "FORMAT", "COLOR"]


def _post_search(body, access):
    """POST /products/search (busca por atributos). Retorna (status, json)."""
    for i in range(3):
        try:
            r = requests.post(rec.API + "/products/search",
                              headers={"Authorization": "Bearer " + access,
                                       "Content-Type": "application/json"},
                              json=body, timeout=25)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.6 * (i + 1)); continue
            return r.status_code, (r.json() if r.content else {})
        except Exception:
            time.sleep(0.5)
    return 0, {}


def candidatos_por_atributos(reg, access, n_attrs=4):
    """Busca POST /products/search por domínio + atributos (BRAND/MODEL/LINE...).
    Recurso oficial e mais específico que o texto — pode achar páginas que a busca
    por q= não indexa. Retorna candidatos no mesmo formato de candidatos()."""
    dom = reg.get("domain")
    attrs = reg.get("attrs_full") or []
    if not dom or len(attrs) < 3:
        return []

    def rank(a):
        try:
            return _ATTR_PRIOR.index(a.get("id"))
        except ValueError:
            return 99

    body_attrs = []
    for a in sorted(attrs, key=rank)[:n_attrs]:
        one = {"id": a["id"]}
        if a.get("value_id"):
            one["value_id"] = a["value_id"]
        elif a.get("value_name"):
            one["value_name"] = a["value_name"]
        else:
            continue
        body_attrs.append(one)
    if len(body_attrs) < 3:
        return []

    body = {"site_id": "MLB", "status": "active", "domain_id": dom, "attributes": body_attrs}
    st, d = _post_search(body, access)
    out = []
    for p in ((d.get("results") if isinstance(d, dict) else None) or [])[:15]:
        pid = p.get("id")
        if not pid or comp._parece_bundle(p.get("name")):
            continue
        ps = precos_produto(pid, access)
        out.append({"pid": pid, "nome": p.get("name"), "via": "attr",
                    "preco_min": (ps[0] if ps else None),
                    "preco_max": (ps[-1] if ps else None), "n_anuncios": len(ps)})
    return out


def coletar(item_id, access):
    st, it = rec.get(f"/items/{item_id}?include_attributes=all", access)
    if not isinstance(it, dict):
        return None
    try:
        if int(it.get("available_quantity")) <= 0:
            return None
    except (TypeError, ValueError):
        pass
    a = attrs_do_item(it)
    consulta = comp.consulta_do_item(it)
    reg = {
        "item_id": item_id,
        "sku": it.get("seller_sku") or it.get("seller_custom_field"),
        "titulo": it.get("title"),
        "marca": a.get("BRAND"),
        "modelo": a.get("MODEL") or a.get("ALPHANUMERIC_MODEL") or a.get("LINE"),
        "gtin": comp.gtin_do_item(it),
        "preco": it.get("price"),
        "catalog_listing": bool(it.get("catalog_listing")),
        "consulta": consulta,
        "domain": it.get("domain_id"),
        "attrs_full": [{"id": x.get("id"), "value_id": x.get("value_id"),
                        "value_name": x.get("value_name")}
                       for x in (it.get("attributes") or [])
                       if x.get("id") and (x.get("value_id") or x.get("value_name"))],
    }
    reg["candidatos"] = candidatos(reg, access)
    return reg


def main():
    if not SELLER_ID:
        print("Defina SELLER_ID.", flush=True); return
    rec.preload()
    access = sid = None
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if str(s) == str(SELLER_ID):
            access, sid = a, s; break
    if not access:
        print(f"não autentiquei a conta {SELLER_ID}.", flush=True); return

    if MODO == "sem_match":
        try:
            rows = (rec.sb.table("repricer_sugestoes").select("item_id")
                    .eq("seller_id", str(SELLER_ID)).eq("acao", "sem_match")
                    .limit(LIMITE).execute().data) or []
            ids = [r["item_id"] for r in rows]
        except Exception:
            ids = []
        if not ids:
            print("Sem itens 'sem_match' no banco; use MODO=amostra.", flush=True); return
    else:
        # SKUs já mapeados (pra pular e trazer só produtos NOVOS a cada lote)
        mapeados = set()
        try:
            for r in (rec.sb.table("repricer_match").select("sku").execute().data or []):
                if r.get("sku"):
                    mapeados.add(r["sku"])
        except Exception:
            pass
        todos, _ = rec.todos_ativos(sid, access)
        det = rec.detalhes_itens(todos, access)   # multiget: pega seller_sku barato
        vistos, ids = set(), []
        for iid in todos:
            b = det.get(iid) or {}
            sku = b.get("seller_sku") or b.get("seller_custom_field")
            key = sku or iid
            if key in vistos or (sku and sku in mapeados):
                continue                # dedup por SKU + pula os já confirmados
            vistos.add(key)
            ids.append(iid)
            if len(ids) >= LIMITE:
                break
        print(f"(SKUs já mapeados: {len(mapeados)}; coletando {len(ids)} produtos novos)", flush=True)

    print(f">>> COLETOR DE MATCH | conta {sid} | {len(ids)} itens | (cole tudo abaixo pro Claude) <<<", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        regs = list(ex.map(lambda i: coletar(i, access), ids))
    print("=====BEGIN_MATCH_JSONL=====", flush=True)
    for reg in regs:
        if not reg:
            continue
        # COMPACTO: só candidatos COM anúncios (os que valem pra preço), nomes curtos.
        cand = [[c["pid"], (c.get("nome") or "")[:48], c.get("preco_min"), c.get("preco_max"), c.get("n_anuncios")]
                for c in reg.get("candidatos", []) if c.get("n_anuncios")]
        slim = {"i": reg["item_id"], "s": reg.get("sku"), "t": (reg.get("titulo") or "")[:48],
                "p": reg.get("preco"), "kit": comp._parece_kit(reg.get("titulo")), "c": cand}
        print(json.dumps(slim, ensure_ascii=False), flush=True)
    print("=====END_MATCH_JSONL=====", flush=True)


if __name__ == "__main__":
    main()
