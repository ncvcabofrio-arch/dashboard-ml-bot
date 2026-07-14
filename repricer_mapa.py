"""
FERRAMENTA DO MAPA (tudo-em-um, só leitura por padrão).
Substitui os antigos repricer_mapa / repricer_check / repricer_reparo.

Faz, numa rodada só:
  1) RESUMO do mapa repricer_match (totais por tipo/confiança, com/sem concorrente).
  2) CHECK dos 'sem_concorrente': lê o catalog_product_id NATIVO de cada anúncio e
     separa quem tem id de catálogo pra puxar direto vs. quem é avulso.
  3) REPARO (só com CONFIRMA=SIM): grava no mapa os que têm id nativo (tipo=produto),
     mostrando antes o nome da página e o menor preço pra você conferir.

Uso: [CONFIRMA=SIM]. Secrets: ML_* e SUPABASE_*.
"""
import os
import repricer_sugestoes as rec
import repricer_match as coll
from ml_auth import obter_access

CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
# chaves a NÃO gravar no reparo (id nativo suspeito). Ex.: "CF2000EQ,OUTRO"
EXCLUIR = {x.strip() for x in (os.environ.get("EXCLUIR") or "").split(",") if x.strip()}
# RESEARCH=SIM: roda a busca (melhorada) nos avulsos e mostra páginas candidatas (só leitura)
RESEARCH = (os.environ.get("RESEARCH") or "").strip().upper() == "SIM"


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
    out = {}
    attrs = "id,seller_sku,seller_custom_field,title,catalog_listing,catalog_product_id"
    for i in range(0, len(ids), 20):
        st, d = rec.get(f"/items?ids={','.join(ids[i:i + 20])}&attributes={attrs}", access)
        if isinstance(d, list):
            for row in d:
                b = row.get("body") if isinstance(row, dict) else None
                if isinstance(b, dict) and b.get("id"):
                    out[b["id"]] = b
    return out


def pagina(cpid, access):
    """Nome da página de catálogo + menor preço da página (buy_box_winner_price_range.min)."""
    st, d = rec.get(f"/products/{cpid}", access)
    if not isinstance(d, dict):
        return None, None
    rng = (d.get("buy_box_winner_price_range") or {}).get("min") or {}
    return d.get("name"), rng.get("price")


def gravar(sku, cpid, nome):
    row = {"sku": sku, "product_ids": [cpid], "confianca": "media", "tipo": "produto",
           "nota": f"reparo: catalog_product_id nativo ({str(nome)[:56]})"}
    rec.sb.table("repricer_match").upsert(row, on_conflict="sku").execute()


def main():
    rec.preload()

    # ---------- 1) RESUMO ----------
    linhas = carregar_mapa()
    if not linhas:
        print("Mapa vazio — nada em repricer_match ainda.", flush=True)
        return
    por_tipo, por_conf, com_conc = {}, {}, 0
    for r in linhas:
        por_tipo[r.get("tipo") or "—"] = por_tipo.get(r.get("tipo") or "—", 0) + 1
        por_conf[r.get("confianca") or "—"] = por_conf.get(r.get("confianca") or "—", 0) + 1
        if r.get("product_ids"):
            com_conc += 1
    print(f"===== MAPA repricer_match: {len(linhas)} chaves =====", flush=True)
    print("por tipo:      " + ", ".join(f"{k}={v}" for k, v in sorted(por_tipo.items(), key=lambda x: -x[1])), flush=True)
    print("por confiança: " + ", ".join(f"{k}={v}" for k, v in sorted(por_conf.items(), key=lambda x: -x[1])), flush=True)
    print(f"com concorrente (product_ids): {com_conc}  |  sem: {len(linhas) - com_conc}\n", flush=True)

    # ---------- 2) CHECK dos sem_concorrente ----------
    alvos = {r.get("sku") for r in linhas if r.get("tipo") == "sem_concorrente"}
    print(f">>> checando {len(alvos)} 'sem_concorrente' — catalog_product_id nativo <<<\n", flush=True)
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
                achados[key] = {"item_id": iid, "sku": sku, "conta": s, "titulo": b.get("title"),
                                "cl": bool(b.get("catalog_listing")), "cpid": b.get("catalog_product_id"), "access": a}
    tem_id = [v for v in achados.values() if v["cpid"]]
    avulso = [v for v in achados.values() if not v["cpid"]]
    nao_achei = [k for k in alvos if k not in achados]

    modo = "⚠️ ESCREVER" if CONFIRMA else "SIMULAÇÃO"
    print(f"----- COM catalog_product_id nativo (dá pra puxar DIRETO): {len(tem_id)} | {modo} -----", flush=True)
    n = 0
    for v in sorted(tem_id, key=lambda x: str(x["titulo"])):
        rot = v["sku"] or v["item_id"]
        nome, mn = pagina(v["cpid"], v["access"])
        preco = f"R${mn:.2f}" if mn else "—"
        print(f"  ✅ {rot:20} -> {v['cpid']:16} menor={preco:>12} cl={v['cl']} | pág: {str(nome)[:38]}", flush=True)
        print(f"       (meu anúncio: {str(v['titulo'])[:58]})", flush=True)
        if rot in EXCLUIR:
            print("       (PULADO por EXCLUIR — mantido como sem_concorrente)", flush=True)
            continue
        if CONFIRMA:
            try:
                gravar(rot, v["cpid"], nome); n += 1
            except Exception as e:
                print(f"       (erro ao gravar: {e})", flush=True)

    print(f"\n----- AVULSO, sem id nativo (navegador ou produto único): {len(avulso)} -----", flush=True)
    for v in sorted(avulso, key=lambda x: str(x["titulo"])):
        rot = v["sku"] or v["item_id"]
        print(f"  ∅ {rot:20} item={v['item_id']} conta={v['conta']} | {str(v['titulo'])[:42]}", flush=True)
    if nao_achei:
        print(f"\n(sem anúncio ativo p/ {len(nao_achei)} chaves — ex.: {sorted(nao_achei)[:8]})", flush=True)

    # ---------- 2b) REBUSCA dos avulsos (busca melhorada, só leitura) ----------
    if RESEARCH and avulso:
        print(f"\n>>> REBUSCA dos {len(avulso)} avulsos com a busca melhorada (só leitura) <<<", flush=True)
        for v in sorted(avulso, key=lambda x: str(x["titulo"])):
            rot = v["sku"] or v["item_id"]
            reg = coll.coletar(v["item_id"], v["access"])
            cands = [c for c in ((reg or {}).get("candidatos") or []) if c.get("n_anuncios")]
            if not cands:
                print(f"  ∅ {rot:18} | {str(v['titulo'])[:40]} -> nada", flush=True)
                continue
            print(f"  ? {rot:18} | {str(v['titulo'])[:40]}", flush=True)
            for c in cands[:4]:
                print(f"       {c['pid']} n={c['n_anuncios']} "
                      f"R${c.get('preco_min')}–{c.get('preco_max')} | {str(c.get('nome'))[:44]}", flush=True)

    # ---------- 3) fecho ----------
    if CONFIRMA:
        print(f"\n✅ REPARO gravado: {n} sem_concorrente viraram produto (via id nativo).", flush=True)
    else:
        print(f"\nSIMULAÇÃO: nada gravado. Confira as páginas acima; rode com CONFIRMA=SIM pra aplicar o reparo dos {len(tem_id)}.", flush=True)


if __name__ == "__main__":
    main()
