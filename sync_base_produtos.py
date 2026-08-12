"""
Sincronizador de PRODUTOS BaseLinker -> Supabase (1x/dia).

DUAS ETAPAS:
  1) getInventoryProductsList  -> id, sku, ean, nome, preco   (1000 por pagina)
  2) getInventoryProductsData  -> custo, kit, tax_rate, FOTO  (1000 por chamada)

A etapa 2 existe porque o CUSTO nao vem na listagem simples. E o custo sustenta
margem, DRE, alerta de margem baixa e repricer — so da' para desligar o
sync_ideris.py depois que esta etapa estiver rodando.

A FOTO sai de carona nessa mesma chamada: 'images' vem junto com o custo, entao
o app de contagem ganha imagem sem gastar nada da cota de 100 chamadas/min.

Protecoes (as mesmas dos robos do Ideris):
  - coleta vazia NAO grava nada e avisa no Telegram
  - volume abaixo de MIN_PRODUTOS tambem NAO grava
  - upsert em lotes de 200
  - falha na etapa 2 NAO derruba a etapa 1: grava o que tem e reporta
  - LIMPEZA: produto que sumiu da Base sai do espelho — mas so' se a limpeza
    for pequena. Se de repente 30% dos produtos "desaparecessem", e' muito
    mais provavel que a coleta veio quebrada do que a loja ter esvaziado o
    catalogo. Nesse caso nao apaga nada e avisa no Telegram.
"""

import json
import os
from datetime import datetime, timezone

import requests
from supabase import create_client

from base_api import catalogo_padrao, chamar, paginar

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MIN_PRODUTOS = int(os.environ.get("MIN_PRODUTOS", "100"))
# fracao maxima do espelho que uma rodada pode apagar de uma vez
MAX_APAGAR_PCT = float(os.environ.get("MAX_APAGAR_PCT", "10"))
PULAR_CUSTO = os.environ.get("PULAR_CUSTO", "0") == "1"
LOTE_DADOS = 1000

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def tg_send(texto):
    if not TG_TOKEN or not TG_CHAT:
        print("(sem Telegram)\n" + texto)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": texto, "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        print("Aviso: falha ao enviar Telegram:", e)


def limpar_ean(v):
    s = str(v or "").strip()
    if not s or s == "0" or not s.isdigit():
        return None
    return s if 8 <= len(s) <= 14 else None


def primeiro_preco(precos):
    if isinstance(precos, dict):
        for v in precos.values():
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    try:
        return float(precos)
    except (TypeError, ValueError):
        return None


def numero(v):
    try:
        n = float(v)
        return n if n != 0 else None      # custo zero na Base = nao preenchido
    except (TypeError, ValueError):
        return None


def primeira_foto(imagens):
    """Primeira foto utilizavel do cadastro da Base.

    'images' vem ora como dict {"0": url, "1": url}, ora como lista. So aceita
    http(s): em algumas contas vem base64 embutido, e guardar isso numa coluna
    de texto incha a tabela a toa - o app precisa de um endereco para carregar,
    nao do arquivo.
    """
    if isinstance(imagens, dict):
        # a ordem importa: "0" e' a foto principal
        valores = [imagens[k] for k in sorted(imagens.keys(), key=lambda x: str(x))]
    elif isinstance(imagens, (list, tuple)):
        valores = list(imagens)
    else:
        valores = []

    for v in valores:
        s = str(v or "").strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
    return None


def buscar_dados(inv_id, ids):
    """Etapa 2: custo, kit, imposto e foto, em lotes. Devolve {product_id: {...}}."""
    saida = {}
    for i in range(0, len(ids), LOTE_DADOS):
        lote = ids[i:i + LOTE_DADOS]
        d = chamar("getInventoryProductsData",
                   {"inventory_id": inv_id, "products": lote})
        if not d:
            print(f"  ! etapa 2 falhou no lote {i // LOTE_DADOS + 1} — sigo sem esses custos")
            continue
        prods = d.get("products") or {}
        itens = prods.items() if isinstance(prods, dict) else [
            (str(p.get("id")), p) for p in prods
        ]
        for pid, p in itens:
            bundle = p.get("bundle_products")
            saida[str(pid)] = {
                "custo": numero(p.get("average_cost")),
                "custo_landed": numero(p.get("average_landed_cost")),
                "tax_rate": numero(p.get("tax_rate")),
                "is_bundle": bool(p.get("is_bundle")),
                "bundle_itens": json.dumps(bundle, ensure_ascii=False) if bundle else None,
                "imagem": primeira_foto(p.get("images")),
            }
        print(f"  etapa 2: lote {i // LOTE_DADOS + 1} — {len(itens)} produtos")
    return saida


def main():
    inv_id, inv_nome = catalogo_padrao()
    if not inv_id:
        tg_send("⚠️ Robô de produtos da Base: não consegui listar os catálogos.")
        return
    print(f"Catálogo: {inv_id} ({inv_nome})")

    agora = datetime.now(timezone.utc).isoformat()
    linhas = {}
    ruins = []

    # ---------------------------------------------------------------- etapa 1
    for pagina, itens in paginar("getInventoryProductsList", inv_id):
        for pid, p in itens:
            try:
                linhas[str(pid)] = {
                    "product_id": str(pid),
                    "sku": str(p.get("sku") or "").strip() or None,
                    "ean": limpar_ean(p.get("ean")),
                    "nome": (p.get("name") or "").strip() or None,
                    "preco": primeiro_preco(p.get("prices")),
                    "atualizado_em": agora,
                }
            except Exception as e:
                ruins.append((pid, str(e)[:120]))
        print(f"  página {pagina}: {len(itens)} produtos (acumulado {len(linhas)})")

    total = len(linhas)
    if total == 0:
        tg_send("⚠️ Robô de produtos da Base: não coletei nada nesta rodada. Não gravei.")
        return
    if total < MIN_PRODUTOS:
        tg_send(f"⚠️ Robô de produtos da Base: vieram só {total} produtos "
                f"(mínimo {MIN_PRODUTOS}). Não gravei, por segurança.")
        return

    # ---------------------------------------------------------------- etapa 2
    com_custo = 0
    kits = 0
    com_foto = 0
    if not PULAR_CUSTO:
        print("Etapa 2: buscando custo, composição de kit e foto...")
        dados = buscar_dados(inv_id, [int(k) for k in linhas.keys()])
        for pid, extra in dados.items():
            if pid in linhas:
                linhas[pid].update(extra)
                linhas[pid]["dados_sync_em"] = agora
                if extra.get("custo") is not None:
                    com_custo += 1
                if extra.get("is_bundle"):
                    kits += 1
                if extra.get("imagem"):
                    com_foto += 1
    else:
        print("Etapa 2 pulada (PULAR_CUSTO=1)")

    com_ean = sum(1 for v in linhas.values() if v.get("ean"))
    com_sku = sum(1 for v in linhas.values() if v.get("sku"))

    if ruins:
        print(f"⚠️ {len(ruins)} registro(s) com problema — CORRIJA na Base:")
        for pid, err in ruins[:20]:
            print(f"   - product_id {pid}: {err}")

    dados_lista = list(linhas.values())
    for i in range(0, len(dados_lista), 200):
        sb.table("base_produtos").upsert(dados_lista[i:i + 200],
                                         on_conflict="product_id").execute()

    # ------------------------------------------------------------- limpeza
    # O upsert acima carimbou atualizado_em em TODOS os produtos que ainda
    # existem na Base. Entao quem ficou com carimbo antigo sumiu de la.
    apagados = 0
    nao_apagados = 0
    try:
        r = sb.table("base_produtos").select("product_id", count="exact") \
              .lt("atualizado_em", agora).execute()
        sumidos = r.count or 0
        limite = max(5, int(total * MAX_APAGAR_PCT / 100))
        if 0 < sumidos <= limite:
            sb.table("base_produtos").delete().lt("atualizado_em", agora).execute()
            apagados = sumidos
            print(f"Limpeza: {apagados} produto(s) apagados do espelho (sumiram da Base).")
        elif sumidos > limite:
            nao_apagados = sumidos
            print(f"⚠️ {sumidos} produtos sumiram de uma vez (limite {limite}). "
                  f"NAO apaguei nada — confira a Base antes.")
    except Exception as e:
        print("Aviso: limpeza nao rodou:", e)

    pct_ean = com_ean / total * 100
    pct_custo = com_custo / total * 100 if total else 0
    pct_foto = com_foto / total * 100 if total else 0
    print(f"✅ Espelho atualizado: {total} produtos. "
          f"{com_ean} com EAN, {com_sku} com SKU, {com_custo} com custo, "
          f"{com_foto} com foto, {kits} kits.")
    tg_send(f"📦 <b>Base — produtos espelhados</b>\n"
            f"{total} produtos\n"
            f"{com_ean} com EAN ({pct_ean:.1f}%)\n"
            f"{com_custo} com custo ({pct_custo:.1f}%)\n"
            f"{com_foto} com foto ({pct_foto:.1f}%)\n"
            f"{kits} kits"
            + (f"\n🗑 {apagados} apagados do espelho" if apagados else "")
            + (f"\n⚠️ {nao_apagados} sumiram de uma vez — NÃO apaguei, confira a Base"
               if nao_apagados else ""))


if __name__ == "__main__":
    main()
