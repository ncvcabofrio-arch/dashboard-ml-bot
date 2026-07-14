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


def _model_code(titulo):
    """Maior token do título que mistura letra+dígito (ex.: 'Umc204hd', 'Cb-30bk', 'Zcc19')."""
    best = ""
    for t in re.findall(r"[0-9A-Za-zÀ-ÿ\-]+", titulo or ""):
        if re.search(r"\d", t) and re.search(r"[A-Za-z]", t) and len(t) > len(best):
            best = t
    return best or None


def candidatos(reg, access):
    """Junta candidatos de VÁRIAS queries (título limpo + marca+código + EAN), dedup por pid."""
    queries = []
    if reg.get("consulta"):
        queries.append(("q", reg["consulta"]))
    code = _model_code(reg.get("titulo"))
    if code:
        queries.append(("q", f"{(reg.get('marca') or '').strip()} {code}".strip()))
    if reg.get("gtin"):
        queries.append(("ean", reg["gtin"]))

    vistos, out = set(), []
    for tipo, q in queries:
        if tipo == "ean":
            url = f"/products/search?status=active&site_id=MLB&product_identifier={q}"
        else:
            url = f"/products/search?status=active&site_id=MLB&q={quote(str(q))}"
        st, d = rec.get(url, access)
        for p in ((d.get("results") if isinstance(d, dict) else None) or [])[:8]:
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
            if len(out) >= 12:
                break
        if len(out) >= 12:
            break
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
