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
import json
from urllib.parse import quote
import repricer_sugestoes as rec
import repricer_competitivo as comp
from ml_auth import obter_access

SELLER_ID = (os.environ.get("SELLER_ID") or "").strip()
LIMITE = int(os.environ.get("LIMITE", "15"))
MODO = (os.environ.get("MODO") or "amostra").strip().lower()
N_CAND = int(os.environ.get("N_CAND", "6"))   # candidatos de catálogo por item


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


def candidatos(consulta, access):
    st, d = rec.get(f"/products/search?status=active&site_id=MLB&q={quote(str(consulta))}", access)
    prods = (d.get("results") if isinstance(d, dict) else None) or []
    out = []
    for p in prods[:N_CAND]:
        pid = p.get("id")
        if not pid:
            continue
        ps = precos_produto(pid, access)
        out.append({"pid": pid, "nome": p.get("name"),
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
        "titulo": it.get("title"),
        "marca": a.get("BRAND"),
        "modelo": a.get("MODEL") or a.get("ALPHANUMERIC_MODEL") or a.get("LINE"),
        "gtin": comp.gtin_do_item(it),
        "preco": it.get("price"),
        "catalog_listing": bool(it.get("catalog_listing")),
        "consulta": consulta,
        "candidatos": candidatos(consulta, access) if consulta else [],
    }
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
        ids, _ = comp.rec.todos_ativos(sid, access)
        ids = ids[:LIMITE]

    print(f">>> COLETOR DE MATCH | conta {sid} | {len(ids)} itens | (cole tudo abaixo pro Claude) <<<", flush=True)
    print("=====BEGIN_MATCH_JSONL=====", flush=True)
    for item_id in ids:
        reg = coletar(item_id, access)
        if reg:
            print(json.dumps(reg, ensure_ascii=False), flush=True)
    print("=====END_MATCH_JSONL=====", flush=True)


if __name__ == "__main__":
    main()
