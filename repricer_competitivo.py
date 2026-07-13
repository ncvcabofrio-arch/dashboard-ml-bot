"""
FASE 1 — SONDA COMPETITIVA (somente leitura, NÃO escreve nada no ML nem no banco).
Cruza cada anúncio de CATÁLOGO com a concorrência (price_to_win + concorrentes na PDP)
e a sua margem, e imprime a DECISÃO que o robô tomaria — pra validarmos a régua antes
de ligar no painel/aplicador.

Régua (definida com o vendedor):
  - Perdendo por PREÇO: alvo = price_to_win, TRAVADO no piso da etiqueta (18% ou grupo).
      * price_to_win >= preço-piso  -> DESCONTAR até o price_to_win (via PRICE_DISCOUNT/co-financiada).
      * price_to_win <  preço-piso  -> NÃO PERSEGUIR (ganhar daria prejuízo). Alerta.
  - Perdendo por motivo NÃO-preço (reputação, envio, Full, manufacturing): não desconta, reporta motivo.
  - GANHANDO: se há folga até o 2º lugar, pode SUBIR o preço (reduzir desconto) até logo
      abaixo do concorrente pra maximizar margem. Se já está no cheio, nada a fazer.
  - Sem concorrente / não-catálogo: sem visão competitiva (fica pra política por etiqueta).

Uso: SELLER_ID=<conta>  [MAX_ITENS=30]  [MARGEM_MIN=18]
Só leitura: usa GET /items/{id}, /items/{id}/price_to_win, /products/{pid}/items.
"""
import os
import time
import repricer_sugestoes as rec
from ml_auth import obter_access

API = rec.API
SELLER_ID = (os.environ.get("SELLER_ID") or "").strip()
MAX_ITENS = int(os.environ.get("MAX_ITENS", "30"))
EPS = 0.01


def margem_no_preco(pb, cat, ltid, frete, custo, access):
    """Fórmula 'Você recebe' com meli%=0 (sem cofinanciamento)."""
    com = rec.comissao(round(pb, 2), cat, ltid, access) or 0
    recebe = pb - com - frete
    return (((recebe - custo) / pb * 100) if pb else -999), round(com, 2), round(recebe, 2)


def preco_piso(piso_pct, cat, ltid, frete, custo, access, teto):
    """Menor preço que ainda rende a margem mínima (piso). Busca binária monotônica."""
    lo, hi = 0.5, max(teto, 1.0)
    m_hi, _, _ = margem_no_preco(hi, cat, ltid, frete, custo, access)
    if m_hi < piso_pct:              # nem no preço cheio a margem alcança o piso
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        m, _, _ = margem_no_preco(mid, cat, ltid, frete, custo, access)
        if m < piso_pct:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 2)


def price_to_win(item_id, access):
    st, d = rec.get(f"/items/{item_id}/price_to_win?version=v2", access)
    return d if isinstance(d, dict) else None


def concorrentes(product_id, sid, access):
    """Preços dos concorrentes na página de produto (exclui você). Retorna lista de floats."""
    st, d = rec.get(f"/products/{product_id}/items?limit=50", access)
    res = d.get("results") if isinstance(d, dict) else None
    precos = []
    for r in (res or []):
        try:
            if str(r.get("seller_id")) != str(sid) and r.get("price"):
                precos.append(float(r["price"]))
        except (TypeError, ValueError):
            pass
    return sorted(precos)


def analisar(item_id, access, sid):
    st, it = rec.get(f"/items/{item_id}", access)
    if not isinstance(it, dict):
        return None
    p0 = float(it.get("price") or 0)
    cat, ltid = it.get("category_id"), it.get("listing_type_id")
    sku = it.get("seller_sku") or it.get("seller_custom_field")
    pid = it.get("catalog_product_id")
    catalog = bool(it.get("catalog_listing")) or bool(pid)
    custo = rec.custo_de(sku)
    if custo is None or not p0:
        return {"item_id": item_id, "titulo": it.get("title"), "acao": "sem_custo",
                "detalhe": "sem custo cadastrado ou sem preço"}
    frete, _ = rec.frete_de(sku, item_id, access)
    piso, grupo = rec.margem_minima_do(sku)
    pmin = preco_piso(piso, cat, ltid, frete, custo, access, p0)
    m_cheio, _, _ = margem_no_preco(p0, cat, ltid, frete, custo, access)

    base = {"item_id": item_id, "titulo": it.get("title"), "grupo": grupo, "piso": piso,
            "preco_cheio": round(p0, 2), "margem_cheio": round(m_cheio, 1),
            "preco_piso": pmin, "catalog": catalog}

    if not catalog:
        base.update({"acao": "sem_concorrencia", "detalhe": "não é catálogo (sem price_to_win)"})
        return base

    ptw = price_to_win(item_id, access)
    if not ptw:
        base.update({"acao": "sem_dado", "detalhe": "price_to_win indisponível"})
        return base
    status = ptw.get("status")
    alvo = ptw.get("price_to_win")
    winner = (ptw.get("winner") or {})
    reason = ptw.get("reason") or []
    base.update({"status": status, "price_to_win": alvo,
                 "winner_price": winner.get("price"), "reason": ", ".join(reason)})

    if status == "winning":
        precos = concorrentes(pid, sid, access)
        segundo = precos[0] if precos else None
        base["segundo"] = segundo
        if segundo and segundo - EPS > p0 + EPS:
            m_seg, _, _ = margem_no_preco(segundo - EPS, cat, ltid, frete, custo, access)
            base.update({"acao": "subir_margem",
                         "detalhe": f"ganhando; 2º lugar R${segundo:.2f} > seu R${p0:.2f} "
                                    f"-> pode subir até ~R${segundo - EPS:.2f} (margem {m_seg:.1f}%)"})
        else:
            base.update({"acao": "manter_ganhando", "detalhe": "ganhando, sem folga p/ subir"})
        return base

    # perdendo (competing / listed / sharing_first_place)
    reason_preco = (alvo is not None)
    if not reason_preco:
        base.update({"acao": "perde_nao_preco", "detalhe": f"perde por: {base['reason'] or 'motivo não-preço'}"})
        return base
    alvo = float(alvo)
    if pmin is not None and alvo >= pmin:
        m_alvo, _, rec_alvo = margem_no_preco(alvo, cat, ltid, frete, custo, access)
        desc = (1 - alvo / p0) * 100
        base.update({"acao": "descontar", "alvo": round(alvo, 2), "margem_alvo": round(m_alvo, 1),
                     "detalhe": f"descontar até R${alvo:.2f} ({desc:.1f}% off) -> margem {m_alvo:.1f}%"})
    else:
        base.update({"acao": "nao_perseguir",
                     "detalhe": f"price_to_win R${alvo:.2f} fura o piso (R${pmin}) -> não perseguir"})
    return base


def main():
    if not SELLER_ID:
        print("Defina SELLER_ID (ex 177795203).", flush=True); return
    rec.preload()
    # acha o access da conta escolhida
    access = sid = None
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if str(s) == str(SELLER_ID):
            access, sid = a, s; break
    if not access:
        print(f"não autentiquei a conta {SELLER_ID}.", flush=True); return

    ids, total = rec.todos_ativos(sid, access)
    ids = ids[:MAX_ITENS]
    print(f"===== SONDA COMPETITIVA | conta {sid} | amostra {len(ids)} de {total} (só leitura) =====\n", flush=True)

    cont = {}
    linhas = []
    for item_id in ids:
        r = analisar(item_id, access, sid)
        if not r:
            continue
        cont[r["acao"]] = cont.get(r["acao"], 0) + 1
        linhas.append(r)
        tag = {"descontar": "🎯", "nao_perseguir": "🛑", "subir_margem": "⬆️",
               "manter_ganhando": "🏆", "perde_nao_preco": "⚠️", "sem_concorrencia": "·",
               "sem_dado": "?", "sem_custo": "∅"}.get(r["acao"], "·")
        print(f"{tag} [{r['acao']}] {item_id} {str(r.get('titulo'))[:34]} "
              f"| cheio R${r.get('preco_cheio')} (margem {r.get('margem_cheio')}%) "
              f"| {r.get('detalhe')}", flush=True)
        time.sleep(0.1)

    print("\n=== RESUMO: " + ", ".join(f"{k}: {v}" for k, v in cont.items()) + " ===", flush=True)
    cat = sum(1 for l in linhas if l.get("catalog"))
    print(f"itens de catálogo na amostra: {cat}/{len(linhas)} "
          f"(só esses têm visão de concorrência).", flush=True)
    print("Nada foi escrito. É só a leitura da régua competitiva.", flush=True)


if __name__ == "__main__":
    main()
