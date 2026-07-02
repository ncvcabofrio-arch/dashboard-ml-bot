"""
Sincronizador de ESTOQUE Ideris -> Supabase (roda de 2 em 2 horas).

FONTE CORRETA: endpoint /sku/search (nível do SKU/Produto), campo
'stocks[].currentStock' — que é o estoque físico consolidado (o mesmo "Atual"
que aparece na tela e no Power BI). Antes o robô lia o 'quantity' do modelo de
anúncio (/listingModel/search), que era um campo cru e podia vir com erro de
digitação (ex.: '300O'); agora não usamos mais isso.

- Atualiza 'estoque_base' (+ 'estoque_sync_em') na tabela 'produtos'.
- O estoque do dia a dia continua sendo calculado pela view 'estoque_atual'
  (estoque_base - vendas pagas desde estoque_sync_em).
- Não mexe em custo/nome/vendas.

Obs.: o /sku/search também traz 'type' = SIMPLES ou COMPOSTO (kit). Guardamos
esse tipo no log (e dá, se você quiser, pra classificar kit automaticamente
depois, sem depender do campo 'contar').
"""

import os
import time
import requests
from datetime import datetime, timezone
from supabase import create_client

IDERIS_TOKEN = os.environ["IDERIS_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://apiv3.ideris.com.br"
PAUSA = 1.3          # respeita o limite (50 chamadas/min)
# SKUs pra imprimir no log e você conferir com o Ideris/Power BI:
CONFERIR = {"1262933080", "1262174135", "1262932243", "1262943429"}
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def login():
    r = requests.post(BASE + "/login", json=IDERIS_TOKEN, timeout=30)
    try:
        j = r.json()
        tok = j if isinstance(j, str) else (j.get("token") or j.get("obj") or j)
    except Exception:
        tok = r.text.strip().strip('"')
    if r.status_code != 200 or not tok:
        raise RuntimeError("Falha no login Ideris: " + str(r.status_code) + " " + r.text[:200])
    return str(tok)


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


def buscar(H, limit, offset, tentativas=3):
    texto = ""
    for t in range(tentativas):
        try:
            r = requests.get(BASE + f"/sku/search?limit={limit}&offset={offset}",
                             headers=H, timeout=60)
        except Exception as e:
            texto = f"rede: {e}"
            time.sleep(2 * (t + 1))
            continue
        if r.status_code == 200:
            return True, r.json(), ""
        texto = r.text[:200]
        time.sleep(2 * (t + 1))
    return False, None, texto


def estoque_do_sku(item):
    """Soma o currentStock de todos os depósitos do SKU."""
    total = 0.0
    for s in (item.get("stocks") or []):
        try:
            total += float(s.get("currentStock") or 0)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def coletar_estoque(token):
    H = {"Authorization": "Bearer " + token}
    estoques = {}
    tipos = {"SIMPLES": 0, "COMPOSTO": 0, "OUTRO": 0}
    limit, offset, total = 100, 0, None

    while total is None or offset < total:
        ok, data, texto = buscar(H, limit, offset)
        if not ok:
            print(f"Aviso: falha no offset {offset}: {texto}")
            break
        total = data.get("total", total or 0)
        batch = data.get("obj", []) or []
        if not batch:
            break
        for item in batch:
            sku = item.get("sku")
            if not sku:
                continue
            est = estoque_do_sku(item)
            estoques[sku] = est
            tp = (item.get("type") or "OUTRO").upper()
            tipos[tp if tp in tipos else "OUTRO"] += 1
            if sku in CONFERIR:
                det = [f"{s.get('currentStock')}" for s in (item.get("stocks") or [])]
                print(f"  CONFERIR SKU {sku} [{item.get('type')}] "
                      f"estoque={est}  (depósitos: {det})  — {item.get('title','')[:50]}")
        offset += len(batch)
        time.sleep(PAUSA)

    print(f"Coletado estoque de {len(estoques)} SKUs (de {total}). "
          f"Tipos: {tipos['SIMPLES']} simples, {tipos['COMPOSTO']} compostos(kit), "
          f"{tipos['OUTRO']} outros.")
    return estoques, total


def main():
    token = login()
    estoques, total = coletar_estoque(token)

    if not estoques:
        tg_send("⚠️ Robô de estoque: não consegui coletar nada do Ideris nesta rodada.")
        print("Nada coletado.")
        return

    agora = datetime.now(timezone.utc).isoformat()
    linhas = [{"sku": s, "estoque_base": q, "estoque_sync_em": agora}
              for s, q in estoques.items()]
    for i in range(0, len(linhas), 200):
        sb.table("produtos").upsert(linhas[i:i + 200], on_conflict="sku").execute()
    print(f"✅ Estoque atualizado: {len(linhas)} produtos em {agora}")


if __name__ == "__main__":
    main()
