"""
CHECAGEM DO MAPA (só leitura, não escreve nada).

1) Retrato do mapa repricer_match: total de chaves, quebra por tipo e confiança,
   quantos têm concorrente (product_ids).
2) Para cada 'sem_concorrente', lê o catalog_product_id NATIVO do próprio anúncio
   (o ML entrega direto no /items) e classifica:
     - COM id de catálogo -> dá pra puxar concorrência direto (sem busca/navegador);
     - AVULSO sem id       -> precisa navegador, ou é produto único mesmo.

Uso: (sem inputs). Secrets: ML_* e SUPABASE_*.
"""
import os
import repricer_sugestoes as rec
from ml_auth import obter_access


def carregar_mapa():
    linhas, ini = [], 0
    while True:
        lote = (rec.sb.table("repricer_match")
                .select("sku,product_ids,confianca,tipo,nota")
                .range(ini, ini + 999).execute().data) or []
        linhas.extend(lote)
        if len(lote) < 1000:
            break
        ini += 1000
    return linhas


def multiget_cat(ids, access):
    """Multiget incluindo catalog_listing e catalog_product_id (20 por chamada)."""
    out = {}
    attrs = ("id,seller_sku,seller_custom_field,title,catalog_listing,"
             "catalog_product_id,available_quantity,status")
    for i in range(0, len(ids), 20):
        lote = ids[i:i + 20]
        st, d = rec.get(f"/items?ids={','.join(lote)}&attributes={attrs}", access)
        if isinstance(d, list):
            for row in d:
                b = row.get("body") if isinstance(row, dict) else None
                if isinstance(b, dict) and b.get("id"):
                    out[b["id"]] = b
    return out


def main():
    rec.preload()
    linhas = carregar_mapa()
    if not linhas:
        print("Mapa vazio — nada em repricer_match ainda.", flush=True)
        return

    por_tipo, por_conf, com_conc = {}, {}, 0
    for r in linhas:
        t = r.get("tipo") or "—"
        c = r.get("confianca") or "—"
        por_tipo[t] = por_tipo.get(t, 0) + 1
        por_conf[c] = por_conf.get(c, 0) + 1
        if r.get("product_ids"):
            com_conc += 1

    print(f"===== MAPA repricer_match: {len(linhas)} chaves =====", flush=True)
    print("por tipo:      " + ", ".join(f"{k}={v}" for k, v in sorted(por_tipo.items(), key=lambda x: -x[1])), flush=True)
    print("por confiança: " + ", ".join(f"{k}={v}" for k, v in sorted(por_conf.items(), key=lambda x: -x[1])), flush=True)
    print(f"com concorrente (product_ids): {com_conc}  |  sem: {len(linhas) - com_conc}\n", flush=True)

    alvos = {r.get("sku") for r in linhas if r.get("tipo") == "sem_concorrente"}
    print(f">>> investigando {len(alvos)} 'sem_concorrente' — lendo catalog_product_id nativo de cada anúncio <<<\n", flush=True)

    achados = {}
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if not s:
            continue
        todos, _ = rec.todos_ativos(s, a)
        det = multiget_cat(todos, a)
        for iid, b in det.items():
            sku = b.get("seller_sku") or b.get("seller_custom_field")
            key = sku or iid
            if key in alvos and key not in achados:
                achados[key] = {
                    "item_id": iid, "sku": sku, "conta": s,
                    "titulo": b.get("title"),
                    "catalog_listing": bool(b.get("catalog_listing")),
                    "cpid": b.get("catalog_product_id"),
                }

    tem_id = [v for v in achados.values() if v["cpid"]]
    avulso = [v for v in achados.values() if not v["cpid"]]
    nao_achei = [k for k in alvos if k not in achados]

    print(f"----- COM catalog_product_id nativo (dá pra puxar DIRETO): {len(tem_id)} -----", flush=True)
    for v in sorted(tem_id, key=lambda x: str(x["titulo"])):
        rot = v["sku"] or v["item_id"]
        print(f"  ✅ {rot:22} cat={v['cpid']:16} catalog_listing={v['catalog_listing']} "
              f"| {str(v['titulo'])[:38]}", flush=True)

    print(f"\n----- AVULSO, sem id nativo (navegador ou produto único): {len(avulso)} -----", flush=True)
    for v in sorted(avulso, key=lambda x: str(x["titulo"])):
        rot = v["sku"] or v["item_id"]
        print(f"  ∅ {rot:22} item={v['item_id']} conta={v['conta']} "
              f"| {str(v['titulo'])[:42]}", flush=True)

    if nao_achei:
        print(f"\n(sem anúncio ATIVO no momento p/ {len(nao_achei)} chaves — ex.: {sorted(nao_achei)[:8]})", flush=True)

    print(f"\n=== RESUMO: {len(tem_id)} puxam direto | {len(avulso)} avulsos | "
          f"{len(nao_achei)} sem anúncio ativo ===", flush=True)


if __name__ == "__main__":
    main()
