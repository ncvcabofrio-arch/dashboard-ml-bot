"""
AUTO-MATCHER — constrói o mapa (SKU -> produto de catálogo) sozinho.
Heurística resolve os fáceis (regra do código do modelo + guardas); a IA do Claude
julga os ambíguos. Escreve em repricer_match por SKU. Padrão = SIMULAÇÃO.

Fluxo por SKU novo:
  1) coleta candidatos de catálogo (multi-query, sem bundle) — reusa o coletor.
  2) HEURÍSTICA: mantém páginas cujo NOME contém o código do modelo, na faixa de
     preço, mesma "mão" (canhoto/destro). Se sobra match limpo -> confiança ALTA (auto).
  3) Caso contrário -> IA do Claude decide (produto/kit/sem_concorrente/revisar) c/ confiança.
  4) grava upsert em repricer_match (só com CONFIRMA=SIM).

Uso: SELLER_ID=<conta> [LIMITE=40] [CONFIRMA=SIM] [MODEL_IA=...] [SO_IA=SIM]
Secrets: ANTHROPIC_API_KEY (a mesma chave do Claude que você já usa).
"""
import os
import re
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor
import repricer_sugestoes as rec
import repricer_competitivo as comp
import repricer_match as coll
from ml_auth import obter_access

SELLER_ID = (os.environ.get("SELLER_ID") or "").strip()
LIMITE = int(os.environ.get("LIMITE", "40"))
WORKERS = int(os.environ.get("WORKERS", "8"))
CONFIRMA = (os.environ.get("CONFIRMA") or "").strip().upper() == "SIM"
SO_IA = (os.environ.get("SO_IA") or "").strip().upper() == "SIM"   # manda TUDO pra IA (máx acerto)
MODEL_IA = (os.environ.get("MODEL_IA") or "claude-haiku-4-5").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FAIXA_MIN, FAIXA_MAX = 0.40, 3.0


def norm(s):
    return re.sub(r"[^0-9a-z]", "", str(s or "").lower())


def _mao_esq(txt):
    return bool(re.search(r"canhoto|left|\blh\b", (txt or "").lower()))


def heuristica(reg):
    """Retorna decisão (dict) se o match for LIMPO; senão None (=manda pra IA)."""
    tit = reg.get("titulo") or ""
    p0 = float(reg.get("preco") or 0)
    cands = [c for c in reg.get("candidatos", []) if c.get("n_anuncios")]
    code = coll._model_code(tit)
    if not p0:
        return None
    if not cands:
        return {"product_ids": [], "confianca": "nenhum", "tipo": "sem_concorrente",
                "motivo": "sem página de catálogo com anúncios"}
    if not code:
        return None  # modelo sem código (ex.: 'Mininova') -> IA
    nc = norm(code)
    esq = _mao_esq(tit)
    lo, hi = FAIXA_MIN * p0, FAIXA_MAX * p0
    kept = []
    for c in cands:
        nome = c.get("nome") or ""
        mn = c.get("preco_min")
        if nc not in norm(nome):
            continue                      # código do modelo não bate -> outro produto
        if mn is None or not (lo <= mn <= hi):
            continue                      # fora da faixa -> variante/errado
        if _mao_esq(nome) != esq:
            continue                      # canhoto vs destro
        kept.append(c["pid"])
    if kept:
        return {"product_ids": kept, "confianca": "alta", "tipo": "produto",
                "motivo": f"código '{code}' bate em {len(kept)} página(s)"}
    return None   # nada bateu limpo -> IA decide


def ia_match(reg):
    """Chama o Claude pra julgar o match. Retorna dict decisão."""
    cands = [{"pid": c["pid"], "nome": c.get("nome"), "min": c.get("preco_min"),
              "max": c.get("preco_max"), "n": c.get("n_anuncios")}
             for c in reg.get("candidatos", []) if c.get("n_anuncios")]
    prompt = (
        "Você casa o PRODUTO de um vendedor com páginas de produto do catálogo do Mercado Livre.\n"
        "Retorne SÓ um JSON: {\"product_ids\":[...],\"confianca\":\"alta|media|nenhum\","
        "\"tipo\":\"produto|kit|sem_concorrente|revisar\",\"motivo\":\"...\"}.\n"
        "Regras:\n"
        "- INCLUA TODAS as páginas que forem o MESMO produto. Páginas DUPLICADAS (preços, número de "
        "anúncios ou descrições diferentes) são NORMAIS e devem TODAS entrar em product_ids — a gente "
        "agrega os concorrentes de todas. Diferença de PREÇO entre páginas do mesmo produto NÃO é "
        "motivo pra excluir nem pra 'revisar'.\n"
        "- EXCLUA só página de produto DIFERENTE: outro tamanho/polegada, outra cor, outro acabamento, "
        "outra versão (ex.: V2), canhoto vs destro, ou modelo diferente. Também exclua bundle "
        "(item+capa/suporte/pedal/boné), kit e acessório.\n"
        "- Se o anúncio é um KIT/conjunto: tipo='kit', product_ids=[].\n"
        "- Se NENHUMA página é o mesmo produto: tipo='sem_concorrente', product_ids=[].\n"
        "- Use 'revisar' SÓ quando houver dúvida REAL de variante (ex.: não dá pra saber a cor/acabamento). "
        "Nunca use 'revisar' por causa de preço.\n\n"
        f"MEU PRODUTO: {json.dumps({k: reg.get(k) for k in ('titulo','marca','modelo','gtin','preco')}, ensure_ascii=False)}\n"
        f"CANDIDATOS: {json.dumps(cands, ensure_ascii=False)}\n"
    )
    body = {"model": MODEL_IA, "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}]}
    h = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    for tent in range(3):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", headers=h, json=body, timeout=40)
            if r.status_code >= 500 or r.status_code == 429:
                time.sleep(1.2 * (tent + 1)); continue
            txt = "".join(b.get("text", "") for b in r.json().get("content", []))
            m = re.search(r"\{.*\}", txt, re.S)
            d = json.loads(m.group(0)) if m else {}
            d["motivo"] = "IA: " + str(d.get("motivo", ""))[:120]
            d.setdefault("product_ids", []); d.setdefault("confianca", "nenhum")
            d.setdefault("tipo", "revisar")
            return d
        except Exception as e:
            if tent == 2:
                return {"product_ids": [], "confianca": "nenhum", "tipo": "revisar",
                        "motivo": f"IA falhou: {e}"}
            time.sleep(1.0)


def decidir(reg):
    if comp._parece_kit(reg.get("titulo")):
        return {"product_ids": [], "confianca": "nenhum", "tipo": "kit", "motivo": "título é kit/conjunto"}
    d = None if SO_IA else heuristica(reg)
    via = "heurística"
    if d is None:
        if not ANTHROPIC_KEY:
            return {"product_ids": [], "confianca": "nenhum", "tipo": "revisar",
                    "motivo": "sem ANTHROPIC_API_KEY; ambíguo -> revisar", "_via": "sem-ia"}
        d = ia_match(reg); via = "IA"
    d["_via"] = via
    return d


def gravar(sku, d):
    row = {"sku": sku, "product_ids": d.get("product_ids") or [],
           "confianca": d.get("confianca"), "tipo": d.get("tipo"), "nota": d.get("motivo")}
    rec.sb.table("repricer_match").upsert(row, on_conflict="sku").execute()


def processar_conta(access, sid, mapeados):
    """Mapeia até LIMITE SKUs NOVOS de uma conta. Atualiza 'mapeados' com os gravados."""
    todos, _ = rec.todos_ativos(sid, access)
    det = rec.detalhes_itens(todos, access)
    vistos, ids = set(), []
    for iid in todos:
        b = det.get(iid) or {}
        sku = b.get("seller_sku") or b.get("seller_custom_field")
        key = sku or iid
        if key in vistos or (sku and sku in mapeados):
            continue
        vistos.add(key); ids.append(iid)
        if len(ids) >= LIMITE:
            break

    modo = "⚠️ ESCREVER" if CONFIRMA else "SIMULAÇÃO"
    print(f">>> AUTO-MATCH | conta {sid} | {len(ids)} produtos novos | {modo} | "
          f"IA={'todos' if SO_IA else 'só ambíguos'} ({MODEL_IA if ANTHROPIC_KEY else 'sem chave'}) <<<", flush=True)
    if not ids:
        return 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        regs = [r for r in ex.map(lambda i: coll.coletar(i, access), ids) if r]

    cont = {}
    for reg in regs:
        sku = reg.get("sku")
        d = decidir(reg)
        cont[d["tipo"]] = cont.get(d["tipo"], 0) + 1
        pids = ",".join(d.get("product_ids") or []) or "—"
        tag = {"produto": "✅", "kit": "📦", "sem_concorrente": "∅", "revisar": "❓"}.get(d["tipo"], "·")
        print(f"{tag} SKU={sku} [{d['tipo']}/{d['confianca']}] via {d.get('_via')} "
              f"| {str(reg.get('titulo'))[:34]} -> {pids} | {d.get('motivo')}", flush=True)
        if CONFIRMA and sku:
            try:
                gravar(sku, d)
                mapeados.add(sku)   # não repetir na próxima conta/rodada
            except Exception as e:
                print(f"   (erro ao gravar {sku}: {e})", flush=True)
    print("=== " + ", ".join(f"{k}: {v}" for k, v in cont.items()) + " ===\n", flush=True)
    return len(ids)


def main():
    if not ANTHROPIC_KEY:
        print("(aviso: sem ANTHROPIC_API_KEY — só a heurística resolve; ambíguos viram 'revisar')", flush=True)
    rec.preload()

    mapeados = set()
    try:
        for r in (rec.sb.table("repricer_match").select("sku").execute().data or []):
            if r.get("sku"):
                mapeados.add(r["sku"])
    except Exception:
        pass
    print(f"(SKUs já mapeados: {len(mapeados)})", flush=True)

    # SELLER_ID definido = só ela; VAZIO (agendado) = TODAS as contas
    alvos = []
    for seller_id, refresh in rec.contas():
        a, s, refresh = obter_access(rec.sb, seller_id, refresh)
        if not s:
            continue
        if SELLER_ID and str(s) != str(SELLER_ID):
            continue
        alvos.append((a, s))
    if not alvos:
        print(f"nenhuma conta pra processar (SELLER_ID={SELLER_ID or 'todas'}).", flush=True); return

    total = 0
    for access, sid in alvos:
        total += processar_conta(access, sid, mapeados)

    if not CONFIRMA:
        print("SIMULAÇÃO: nada gravado. Rode com CONFIRMA=SIM pra escrever o mapa.", flush=True)
    elif total == 0:
        print("Nenhum SKU novo — mapa em dia. ✅", flush=True)


if __name__ == "__main__":
    main()
