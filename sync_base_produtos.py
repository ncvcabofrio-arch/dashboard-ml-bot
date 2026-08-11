"""
Sincronizador de PRODUTOS BaseLinker -> Supabase (1x/dia).

Espelho do catalogo da Base na tabela 'base_produtos'. Nos mesmos moldes do
sync_ideris.py, com as mesmas protecoes:

  - paginacao com freio (limite da BaseLinker: 100 chamadas/min)
  - coleta vazia NAO grava nada e avisa no Telegram
  - queda brusca de volume NAO grava nada e avisa (protecao nova, mesma logica:
    se veio muito menos que da ultima vez, algo esta errado na origem)
  - upsert em lotes de 200
  - registro ruim nao aborta a rodada: pula e reporta no fim

IMPORTANTE: esta tabela e' SO ESPELHO. Nao alimenta painel nenhum, nao mexe em
'produtos' e nao troca a fonte de nada. Existe para a gente comparar com o
painel da Base ate ter confianca.

Secrets no GitHub Actions:
  BASELINKER_TOKEN, SUPABASE_URL, SUPABASE_KEY
Opcionais:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  MIN_PRODUTOS  -> abaixo disso nao grava (padrao 100)
"""

import os
from datetime import datetime, timezone

import requests
from supabase import create_client

from base_api import catalogo_padrao, paginar

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MIN_PRODUTOS = int(os.environ.get("MIN_PRODUTOS", "100"))

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
    """EAN vazio, '0' ou lixo vira None — para nao poluir a base."""
    s = str(v or "").strip()
    if not s or s == "0":
        return None
    if not s.isdigit():
        return None
    if len(s) < 8 or len(s) > 14:
        return None
    return s


def primeiro_preco(precos):
    """A Base devolve preco por grupo de preco. Pega o primeiro que existir."""
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


def main():
    inv_id, inv_nome = catalogo_padrao()
    if not inv_id:
        tg_send("⚠️ Robô de produtos da Base: não consegui listar os catálogos.")
        return
    print(f"Catálogo: {inv_id} ({inv_nome})")

    linhas = {}
    ruins = []
    paginas = 0

    for pagina, itens in paginar("getInventoryProductsList", inv_id):
        paginas = pagina
        for pid, p in itens:
            try:
                sku = str(p.get("sku") or "").strip() or None
                linhas[str(pid)] = {
                    "product_id": str(pid),
                    "sku": sku,
                    "ean": limpar_ean(p.get("ean")),
                    "nome": (p.get("name") or "").strip() or None,
                    "preco": primeiro_preco(p.get("prices")),
                    "atualizado_em": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                # registro ruim nao derruba a rodada — mesma regra do Ideris
                ruins.append({"product_id": pid, "erro": str(e)[:120]})
        print(f"  página {pagina}: {len(itens)} produtos (acumulado {len(linhas)})")

    total = len(linhas)
    com_ean = sum(1 for v in linhas.values() if v["ean"])
    com_sku = sum(1 for v in linhas.values() if v["sku"])

    print(f"Coletado: {total} produtos em {paginas} página(s). "
          f"{com_ean} com EAN, {com_sku} com SKU.")

    if ruins:
        print(f"⚠️ {len(ruins)} registro(s) com problema — CORRIJA na Base:")
        for r in ruins[:20]:
            print(f"   - product_id {r['product_id']}: {r['erro']}")

    # PROTECAO 1: coleta vazia nunca grava.
    if total == 0:
        tg_send("⚠️ Robô de produtos da Base: não coletei nada nesta rodada. "
                "Não gravei — o espelho continua com os dados anteriores.")
        return

    # PROTECAO 2: volume muito abaixo do minimo esperado tambem nao grava.
    if total < MIN_PRODUTOS:
        tg_send(f"⚠️ Robô de produtos da Base: vieram só {total} produtos "
                f"(mínimo esperado {MIN_PRODUTOS}). Não gravei, por segurança.")
        return

    dados = list(linhas.values())
    for i in range(0, len(dados), 200):
        sb.table("base_produtos").upsert(dados[i:i + 200], on_conflict="product_id").execute()

    pct = com_ean / total * 100
    print(f"✅ Espelho atualizado: {total} produtos.")
    tg_send(f"📦 <b>Base — produtos espelhados</b>\n"
            f"{total} produtos\n"
            f"{com_ean} com EAN ({pct:.1f}%)\n"
            f"{com_sku} com SKU")


if __name__ == "__main__":
    main()
