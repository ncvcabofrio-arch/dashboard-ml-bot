"""
Sincronizador de ESTOQUE BaseLinker -> Supabase (roda de 2 em 2 horas).

FONTE: getInventoryProductsList (catalogo), campo 'stock' -- um dicionario
{warehouse_id: quantidade}. E' o equivalente ao 'stocks[].currentStock' do
Ideris: estoque fisico por deposito.

DOIS OLHARES AO MESMO TEMPO (novo)
  - 'estoque_base'  continua sendo o TOTAL (soma dos depositos). E' esse que
    a view 'estoque_atual' usa, entao a serie historica que vinha do Ideris
    nao quebra.
  - alem dele, gravamos UMA COLUNA POR EMPRESA (deposito), configuravel em
    BL_EMPRESAS. Assim da' para ver a operacao inteira e cada empresa.

    BL_EMPRESAS="estoque_padrao=bl_68154,estoque_agn=bl_71620,estoque_nova=bl_71621"
    (para juntar dois depositos numa empresa so: "estoque_agn=bl_71620+bl_71622")

  Se as colunas ainda nao existirem na tabela 'produtos', o robo AVISA e grava
  so o total -- nada quebra. Rode o sql_estoque_por_empresa.sql para cria-las.

Diferenca importante em relacao ao Ideris: a BaseLinker separa ESTOQUE de
RESERVA. 'stock' e' o fisico; 'reservations' e' o que ja esta comprometido
com pedidos. O Ideris devolvia so o fisico, entao o padrao aqui e' o mesmo
(fisico puro) -- se quiser descontar reservas, ligue BL_DESCONTAR_RESERVAS=1.

- Nao mexe em custo/nome/vendas.
"""

import os
import time
from datetime import datetime, timezone

import requests
from supabase import create_client

from baselinker import BaseLinker
from bl_supa import DRY_RUN, orfaos_com_estoque, relatar_orfaos, upsert_blocos

def _limpar_url(bruto):
    """O .bat grava o que voce digita: espaco no fim, aspas ou /rest/v1
    colado fazem o Supabase devolver PGRST125 sem explicar nada."""
    u = (bruto or "").strip().strip('"').strip("'").strip().rstrip("/")
    for sufixo in ("/rest/v1", "/rest"):
        if u.lower().endswith(sufixo):
            u = u[: -len(sufixo)].rstrip("/")
    return u


SUPABASE_URL = _limpar_url(os.environ["SUPABASE_URL"])
SUPABASE_KEY = (os.environ["SUPABASE_KEY"] or "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# depositos a considerar no TOTAL, ex.: "bl_206,bl_207". Vazio = soma todos.
WAREHOUSES = [w.strip() for w in os.environ.get("BL_WAREHOUSES", "").split(",") if w.strip()]
DESCONTAR_RESERVAS = os.environ.get("BL_DESCONTAR_RESERVAS", "0") == "1"
INCLUIR_VARIANTES = os.environ.get("BL_INCLUIR_VARIANTES", "1") != "0"

# Divisao por empresa: DESLIGADA por padrao (decisao de 13/08).
# Hoje o robo grava so o total em 'estoque_base', exatamente como antes.
# Para ligar depois, basta criar as colunas no Supabase e definir:
#   BL_EMPRESAS=estoque_padrao=bl_68154,estoque_agn=bl_71620,estoque_nova=bl_71621
EMPRESAS_PADRAO = ""


def _parse_empresas(bruto):
    """'colA=bl_1,colB=bl_2+bl_3' -> {'colA': ['bl_1'], 'colB': ['bl_2','bl_3']}"""
    mapa = {}
    for parte in (bruto or "").split(","):
        parte = parte.strip()
        if not parte or "=" not in parte:
            continue
        col, ids = parte.split("=", 1)
        col = col.strip()
        depositos = [w.strip() for w in ids.replace("+", " ").split() if w.strip()]
        if col and depositos:
            mapa[col] = depositos
    return mapa


EMPRESAS = _parse_empresas(os.environ.get("BL_EMPRESAS", EMPRESAS_PADRAO))

# SKUs pra imprimir no log e voce conferir com o painel da BaseLinker.
CONFERIR = {s.strip() for s in os.environ.get(
    "BL_CONFERIR", "SM57,UCA222,GD20CE").split(",") if s.strip()}

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("(sem Telegram)\n" + text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        print("Aviso: falha ao enviar Telegram:", e)


def colunas_da_tabela():
    """Le uma linha de 'produtos' so para descobrir quais colunas existem.
    Devolve None quando nao der para saber -- ai nao arriscamos gravar coluna
    inexistente (o PostgREST recusaria o lote inteiro com PGRST204)."""
    try:
        r = sb.table("produtos").select("*").limit(1).execute()
        linhas = r.data or []
        if linhas:
            return set(linhas[0].keys())
        print("Aviso: tabela 'produtos' vazia, nao da' para conferir as colunas.")
    except Exception as e:
        print(f"Aviso: nao consegui inspecionar as colunas de 'produtos' ({e}).")
    return None


def somar(mapa, filtro=None):
    """Soma as quantidades de um dict {warehouse_id: qtd}.
    filtro=None usa BL_WAREHOUSES (comportamento do total)."""
    permitidos = WAREHOUSES if filtro is None else filtro
    total = 0.0
    for wid, qtd in (mapa or {}).items():
        if permitidos and wid not in permitidos:
            continue
        try:
            total += float(qtd or 0)
        except (TypeError, ValueError):
            pass
    return total


def estoque_do_item(item, reservas_por_produto=None, empresas=None):
    """Devolve {'total': x, '<coluna_empresa>': y, ...}."""
    est = item.get("stock")
    res = reservas_por_produto if DESCONTAR_RESERVAS else None

    fisico = somar(est)
    if res:
        fisico -= somar(res)
    out = {"total": round(fisico, 2)}

    for col, depositos in (empresas or {}).items():
        v = somar(est, depositos)
        if res:
            v -= somar(res, depositos)
        out[col] = round(v, 2)
    return out


def coletar_estoque(bl, inv_id, empresas):
    lista = bl.lista_produtos(inv_id, include_variants=INCLUIR_VARIANTES)

    reservas = {}
    if DESCONTAR_RESERVAS:
        print("Lendo reservas...")
        for pid, p in bl.estoque_produtos(inv_id).items():
            reservas[pid] = p.get("reservations") or {}

    estoques = {}
    variantes = 0
    for pid, item in lista.items():
        sku = item.get("sku")
        if not sku:
            continue
        if item.get("parent_id"):
            variantes += 1
        est = estoque_do_item(item, reservas.get(str(pid)), empresas)
        estoques[sku] = est
        if sku in CONFERIR:
            porempresa = " ".join(f"{c}={est[c]}" for c in empresas)
            print(f"  CONFERIR SKU {sku} (product_id={pid}) total={est['total']} "
                  f"{porempresa} (depositos crus: {item.get('stock')}) "
                  f"— {str(item.get('name',''))[:50]}")

    print(f"Coletado estoque de {len(estoques)} SKUs "
          f"({variantes} sao variantes). "
          f"Depositos no total: {', '.join(WAREHOUSES) if WAREHOUSES else 'TODOS'}. "
          f"Reservas descontadas: {'sim' if DESCONTAR_RESERVAS else 'nao'}.")

    if empresas:
        print("  Por empresa:")
        for col, depositos in empresas.items():
            soma = sum(v.get(col, 0) for v in estoques.values())
            com = sum(1 for v in estoques.values() if v.get(col, 0) > 0)
            print(f"     {col:<18} ({'+'.join(depositos)}): "
                  f"{round(soma, 2)} unidades em {com} SKU(s)")
        soma_total = sum(v["total"] for v in estoques.values())
        print(f"     {'TOTAL (estoque_base)':<18} {round(soma_total, 2)} unidades")
    return estoques


def main():
    bl = BaseLinker()
    inv_id = bl.inventory_id_padrao()
    print(f"Catalogo BaseLinker: inventory_id={inv_id}"
          + ("  [DRY RUN: nao grava nada]" if DRY_RUN else ""))

    # quais colunas por empresa podemos realmente gravar?
    colunas = colunas_da_tabela()
    empresas = dict(EMPRESAS)
    if EMPRESAS and colunas is not None:
        faltando = [c for c in EMPRESAS if c not in colunas]
        if faltando:
            print(f"⚠️ A tabela 'produtos' ainda nao tem: {', '.join(faltando)}.")
            print("   Rode o sql_estoque_por_empresa.sql no SQL Editor do Supabase.")
            print("   Por enquanto gravo so o total em estoque_base.")
            empresas = {c: d for c, d in EMPRESAS.items() if c in colunas}
    elif EMPRESAS and colunas is None:
        print("⚠️ Nao consegui conferir as colunas: gravo so o total, por seguranca.")
        empresas = {}

    if empresas:
        print("Estoque por empresa: "
              + ", ".join(f"{c} <- {'+'.join(d)}" for c, d in empresas.items()))

    try:
        # coleto SEMPRE com o mapa completo (o log mostra todas as empresas,
        # mesmo as que ainda nao tem coluna); gravo so as que existem.
        estoques = coletar_estoque(bl, inv_id, EMPRESAS)
    except Exception as e:
        tg_send(f"⚠️ Robo de estoque: falha ao ler a BaseLinker — {e}")
        raise

    if not estoques:
        tg_send("⚠️ Robo de estoque: nao consegui coletar nada da BaseLinker nesta rodada.")
        print("Nada coletado.")
        return

    # Orfao aqui e' perigoso: se um SKU para de vir da BaseLinker, o
    # 'estoque_sync_em' dele congela e a view 'estoque_atual' fica subtraindo
    # vendas para sempre, sem nunca resetar. Por isso o alerta no Telegram.
    orfaos, _ = relatar_orfaos(sb, set(estoques), "Cobertura de estoque")
    if orfaos:
        perigosos = orfaos_com_estoque(sb, orfaos)
        print(f"  Desses, {len(perigosos)} ainda tem estoque no Supabase "
              f"(o resto e' cadastro velho zerado, inofensivo).")
        if perigosos:
            linhas_alerta = "\n".join(f"• {s}: {q}" for s, q in perigosos[:15])
            tg_send(f"⚠️ {len(perigosos)} SKU(s) COM ESTOQUE no BI que a "
                    f"BaseLinker nao conhece. O estoque deles vai desandar "
                    f"ate serem cadastrados la:\n{linhas_alerta}")

    agora = datetime.now(timezone.utc).isoformat()
    linhas = []
    for sku, est in estoques.items():
        linha = {"sku": sku, "estoque_base": est["total"], "estoque_sync_em": agora}
        for col in empresas:
            linha[col] = est.get(col, 0)
        linhas.append(linha)

    upsert_blocos(sb, linhas, "✅ Estoque atualizado")
    if not DRY_RUN:
        print(f"   carimbo estoque_sync_em = {agora}")


if __name__ == "__main__":
    main()
