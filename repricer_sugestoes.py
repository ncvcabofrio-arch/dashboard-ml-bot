"""
Fase 1 — ROBÔ DE RECOMENDAÇÃO DE PROMOÇÕES COMPARTILHADAS.
Somente leitura no Mercado Livre (não aplica nada). Para cada anúncio ATIVO:
  - lê as promoções onde o ML banca parte (meli_percentage)
  - calcula a MARGEM resultante de cada uma (recebimento − comissão − frete − custo)
  - recomenda a melhor que MANTÉM o piso do grupo do produto (padrão 18%)
Grava as recomendações na tabela 'repricer_sugestoes' pra você aprovar no app.
"""
import os
import time
import requests
from supabase import create_client
from ml_auth import obter_access

API = "https://api.mercadolibre.com"
AMOSTRA = int(os.environ.get("AMOSTRA", "40"))        # itens ativos por conta (suba quando quiser)
MARGEM_PADRAO = float(os.environ.get("MARGEM_MIN", "18"))
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get(path, access, tent=3):
    r = None
    for i in range(tent):
        r = requests.get(API + path, headers={"Authorization": "Bearer " + access}, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.6 * (i + 1))
            continue
        break
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def contas():
    res = sb.table("contas").select("seller_id, refresh_token").execute()
    cs = [(c["seller_id"], c.get("refresh_token")) for c in (res.data or []) if c.get("refresh_token")]
    seed = os.environ.get("ML_REFRESH_TOKEN", "")
    if not cs and seed:
        cs = [(None, seed)]
    return cs


def custo_de(sku):
    if not sku:
        return None
    try:
        r = sb.table("produtos").select("custo").eq("sku", sku).limit(1).execute().data
        if r and r[0].get("custo"):
            return float(r[0]["custo"])
    except Exception:
        pass
    return None


CEP = os.environ.get("CEP", "01310100")   # destino de referência (Av. Paulista, SP)


def frete_anuncio(item_id, access):
    """Custo de envio DO ANÚNCIO (o que VOCÊ paga no frete grátis), lido de
    /items/{id}/shipping_options com um CEP de referência.

    Retorna (custo, origem):
      - custo = list_cost da opção de frete grátis (o valor que o ML te cobra,
        já com o desconto do Mercado Envios) — é o que bate com o
        "Custo de envio" do painel de promoções.
      - fallback: base_cost (tabela cheia) se não houver list_cost.
    Retorna (None, None) se o anúncio não devolver opções.
    """
    st, so = get(f"/items/{item_id}/shipping_options?zip_code={CEP}", access)
    opts = so.get("options") if isinstance(so, dict) else None
    if not isinstance(opts, list) or not opts:
        return None, None
    # prioriza a opção "grátis para o comprador" (cost == 0); senão a mais barata
    gratis = [o for o in opts if float(o.get("cost") or 0) == 0]
    escolha = (gratis or opts)
    # entre as candidatas, pega a de menor list_cost (opção padrão de envio)
    def keyf(o):
        return float(o.get("list_cost") or o.get("base_cost") or 1e9)
    o = min(escolha, key=keyf)
    lc = o.get("list_cost")
    bc = o.get("base_cost")
    if lc is not None:
        return round(float(lc), 2), "list_cost"
    if bc is not None:
        return round(float(bc), 2), "base_cost"
    return None, None


def frete_historico(sku, item_id):
    """Frete médio histórico (por sku; se não tiver, por item) — só fallback."""
    for campo, val in (("sku", sku), ("item_id", item_id)):
        if not val:
            continue
        try:
            r = (sb.table("vendas").select("frete").eq(campo, val)
                 .not_.is_("frete", "null").limit(30).execute().data)
            fs = [float(x["frete"]) for x in (r or []) if x.get("frete")]
            if fs:
                return round(sum(fs) / len(fs), 2)
        except Exception:
            pass
    return 0.0


def frete_de(sku, item_id, access):
    """Frete do anúncio (preferido); cai pro histórico se a API não trouxer."""
    custo, origem = frete_anuncio(item_id, access)
    if custo is not None:
        return custo, origem
    return frete_historico(sku, item_id), "historico"


def margem_minima_do(sku):
    """Piso do grupo do produto (etiqueta -> grupo); senão, padrão."""
    try:
        et = sb.table("repricer_etiquetas").select("grupo_id").eq("sku", sku).limit(1).execute().data
        if et:
            g = sb.table("repricer_grupos").select("margem_minima, nome").eq("id", et[0]["grupo_id"]).limit(1).execute().data
            if g:
                return float(g[0]["margem_minima"]), g[0]["nome"]
    except Exception:
        pass
    return MARGEM_PADRAO, "Padrão"


def comissao(preco, cat, ltid, access):
    if not preco:
        return None
    path = f"/sites/MLB/listing_prices?price={preco}"
    if ltid:
        path += f"&listing_type_id={ltid}"
    if cat:
        path += f"&category_id={cat}"
    st, d = get(path, access)
    if isinstance(d, list) and d:
        d = d[0]
    return d.get("sale_fee_amount") if isinstance(d, dict) else None


def ofertas_do_item(item_id, access):
    """Lista as promoções do item COM preços (detalhe). Retorna [] se não houver."""
    st, resumo = get(f"/seller-promotions/items/{item_id}?app_version=v2", access)
    if not isinstance(resumo, list) or not resumo:
        return []
    # pega um id/tipo qualquer pra chamar o detalhe (que traz todas com preço)
    pid = resumo[0].get("id")
    ptype = resumo[0].get("type")
    path = f"/seller-promotions/items/{item_id}?app_version=v2"
    if pid and ptype:
        path += f"&promotion_id={pid}&promotion_type={ptype}"
    st, det = get(path, access)
    return det if isinstance(det, list) else []


def main():
    total_sug = 0
    # limpa recomendações antigas ainda pendentes (mantém aprovadas/recusadas no histórico)
    try:
        sb.table("repricer_sugestoes").delete().eq("status", "pendente").execute()
    except Exception as e:
        print("Aviso: não consegui limpar pendentes:", e, flush=True)

    for seller_id, refresh in contas():
        access, sid, refresh = obter_access(sb, seller_id, refresh)
        print(f"\n===== CONTA {sid} =====", flush=True)
        st, busca = get(f"/users/{sid}/items/search?status=active&limit={AMOSTRA}", access)
        ids = (busca or {}).get("results", []) if isinstance(busca, dict) else []
        print(f"itens ativos: {len(ids)}", flush=True)

        for item_id in ids:
            ofertas = ofertas_do_item(item_id, access)
            # só as compartilhadas (ML banca parte) que dá pra ENTRAR (candidate) e com preço
            comp = [o for o in ofertas if isinstance(o, dict) and o.get("meli_percentage")
                    and o.get("status") == "candidate" and o.get("price") and o.get("original_price")]
            if not comp:
                continue

            st, it = get(f"/items/{item_id}", access)
            if not isinstance(it, dict):
                continue
            preco = it.get("price")
            ltid = it.get("listing_type_id")
            cat = it.get("category_id")
            sku = it.get("seller_sku") or it.get("seller_custom_field")
            titulo = it.get("title")
            custo = custo_de(sku)
            if custo is None:
                continue  # sem custo não dá pra avaliar margem
            frete, frete_origem = frete_de(sku, item_id, access)
            piso, grupo = margem_minima_do(sku)

            avaliadas = []
            for o in comp:
                p0 = float(o["original_price"])          # preço cheio
                pb = float(o["price"])                    # preço em promoção (comprador paga)
                sp = float(o.get("seller_percentage") or 0)
                mp = float(o.get("meli_percentage") or 0)
                # comissão cheia sobre o preço em promoção; o ML banca via REDUÇÃO da tarifa
                # (a redução = meli% do preço original), exatamente como o painel mostra.
                com_cheia = comissao(round(pb, 2), cat, ltid, access) or 0
                reducao = mp / 100.0 * p0
                tarifa = max(com_cheia - reducao, 0)
                recebe = pb - tarifa - frete              # = "Você recebe" do painel
                lucro = recebe - custo
                margem = (lucro / pb * 100) if pb else -999
                avaliadas.append({
                    "o": o, "pb": pb, "sp": sp, "mp": mp,
                    "tarifa": round(tarifa, 2), "frete": round(frete, 2),
                    "recebe": round(recebe, 2), "margem": round(margem, 2),
                })

            seguras = [a for a in avaliadas if a["margem"] >= piso]
            if not seguras:
                continue
            # melhor = maior desconto pro comprador (menor preço) que mantém a margem
            melhor = min(seguras, key=lambda a: a["pb"])
            o = melhor["o"]

            sug = {
                "seller_id": str(sid),
                "item_id": item_id,
                "sku": sku,
                "titulo": titulo,
                "preco_atual": preco,
                "promocao_id": o.get("id"),
                "promocao_nome": o.get("name"),
                "promocao_tipo": o.get("type"),
                "preco_comprador": melhor["pb"],
                "seller_percentage": melhor["sp"],
                "meli_percentage": melhor["mp"],
                "tarifa_venda": melhor["tarifa"],
                "custo_envio": melhor["frete"],
                "custo_envio_origem": frete_origem,
                "recebe_liquido": melhor["recebe"],
                "custo": custo,
                "margem_resultante": melhor["margem"],
                "grupo": grupo,
                "margem_minima": piso,
                "alternativas": len(seguras) - 1,
                "status": "pendente",
            }
            try:
                sb.table("repricer_sugestoes").insert(sug).execute()
                total_sug += 1
                print(f"+ {item_id} {str(titulo)[:30]} -> {o.get('name')} | comprador R${melhor['pb']} | recebe R${melhor['recebe']} | margem {melhor['margem']}% (piso {piso})", flush=True)
            except Exception as e:
                print(f"  erro ao gravar sugestão {item_id}: {e}", flush=True)
            time.sleep(0.2)

    print(f"\n=== {total_sug} recomendações gravadas (nada foi aplicado no ML) ===", flush=True)


if __name__ == "__main__":
    main()
