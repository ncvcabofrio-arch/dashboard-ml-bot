"""
Sincronizador de ESTOQUE BaseLinker -> Supabase (de 2 em 2 horas).

Espelho do estoque da Base na tabela 'base_produtos'. Escrito nos mesmos moldes
do sync_estoque.py (o do Ideris), inclusive nas protecoes:

  - soma o estoque de TODOS os depositos num numero so, igual o robo do Ideris
    faz com stocks[].currentStock
  - guarda tambem o detalhe por deposito em JSON, sem custo, para nao perder
    informacao caso um dia queiram separar por local
  - guarda as reservas separadas (a Base informa reserva; o Ideris nao informava)
  - coleta vazia NAO grava nada e avisa no Telegram
  - upsert em lotes de 200

IMPORTANTE: escreve SO em 'base_produtos'. Nao encosta em 'produtos', nao mexe
na view 'estoque_atual' e nao alimenta painel nenhum. O sync_estoque.py do
Ideris continua rodando normalmente, sem saber que este existe.

Secrets no GitHub Actions:
  BASELINKER_TOKEN, SUPABASE_URL, SUPABASE_KEY
Opcionais:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  MIN_PRODUTOS -> abaixo disso nao grava (padrao 100)
  CONFERIR     -> SKUs separados por virgula para imprimir no log e voce conferir
                  no painel da Base (mesma ideia do CONFERIR do sync_estoque.py)
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
CONFERIR = {s.strip() for s in os.environ.get("CONFERIR", "").split(",") if s.strip()}

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


def numero(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def separar(dados):
    """Devolve (estoque_por_deposito, reservas_por_deposito).

    A BaseLinker pode devolver duas formas, dependendo da versao:
      {"bl_123": 5, "bl_456": 2}
      {"stock": {"bl_123": 5}, "reservations": {"bl_123": 1}}
    Trata as duas — assim o robo nao quebra se a resposta mudar de formato.
    """
    if not isinstance(dados, dict):
        return {}, {}
    if "stock" in dados or "reservations" in dados:
        est = dados.get("stock") or {}
        res = dados.get("reservations") or {}
        return (est if isinstance(est, dict) else {},
                res if isinstance(res, dict) else {})
    est = {k: v for k, v in dados.items() if str(k).startswith("bl_")}
    return est, {}


def main():
    inv_id, inv_nome = catalogo_padrao()
    if not inv_id:
        tg_send("⚠️ Robô de estoque da Base: não consegui listar os catálogos.")
        return
    print(f"Catálogo: {inv_id} ({inv_nome})")

    # mapa product_id -> sku, para o log de conferência ficar legível
    skus = {}
    try:
        for r in (sb.table("base_produtos").select("product_id, sku").execute().data or []):
            skus[str(r["product_id"])] = r.get("sku")
    except Exception as e:
        print("Aviso: não consegui ler os SKUs do espelho:", e)

    linhas = {}
    agora = datetime.now(timezone.utc).isoformat()

    for pagina, itens in paginar("getInventoryProductsStock", inv_id):
        for pid, dados in itens:
            est, res = separar(dados)
            total = round(sum(numero(v) for v in est.values()), 2)
            reservado = round(sum(numero(v) for v in res.values()), 2)
            linhas[str(pid)] = {
                "product_id": str(pid),
                "estoque_base": total,
                "estoque_reservado": reservado,
                "estoque_por_deposito": json.dumps(est, ensure_ascii=False),
                "estoque_sync_em": agora,
            }
            sku = skus.get(str(pid))
            if sku and sku in CONFERIR:
                print(f"  CONFERIR SKU {sku} (product_id {pid}) "
                      f"estoque={total} reservado={reservado} depósitos={est}")
        print(f"  página {pagina}: {len(itens)} produtos (acumulado {len(linhas)})")

    total_itens = len(linhas)
    print(f"Coletado estoque de {total_itens} produtos.")

    if total_itens == 0:
        tg_send("⚠️ Robô de estoque da Base: não coletei nada nesta rodada. "
                "Não gravei — o espelho continua com os dados anteriores.")
        return

    if total_itens < MIN_PRODUTOS:
        tg_send(f"⚠️ Robô de estoque da Base: vieram só {total_itens} produtos "
                f"(mínimo esperado {MIN_PRODUTOS}). Não gravei, por segurança.")
        return

    dados = list(linhas.values())
    for i in range(0, len(dados), 200):
        sb.table("base_produtos").upsert(dados[i:i + 200], on_conflict="product_id").execute()

    com_estoque = sum(1 for v in dados if v["estoque_base"] > 0)
    pecas = round(sum(v["estoque_base"] for v in dados), 2)
    print(f"✅ Estoque espelhado: {total_itens} produtos, "
          f"{com_estoque} com saldo, {pecas} peças no total.")


if __name__ == "__main__":
    main()
