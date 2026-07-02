"""
Sincronizador de ESTOQUE Ideris -> Supabase (roda de 2 em 2 horas).

- Lê a QUANTIDADE de cada anúncio no Ideris e atualiza 'estoque_base'
  (+ 'estoque_sync_em') na tabela 'produtos'. Não mexe em custo/nome/vendas.
- À prova de dado inválido: se uma página do Ideris travar (ex.: a letra "O"
  no lugar do zero numa quantidade, tipo '300O'), NÃO para o processo — ele
  varre a página item a item, pula só o problemático e segue até o fim.
- No FINAL, se achou algum item travado, manda um resumo no Telegram com os
  produtos vizinhos (antes/depois) pra você localizar e corrigir o item exato.

O estoque do dia a dia continua sendo calculado pela view 'estoque_atual'
(estoque_base - vendas pagas desde estoque_sync_em).
"""

import os
import re
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
        print("(sem Telegram configurado)\n" + text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        print("Aviso: falha ao enviar Telegram:", e)


def get(H, limit, offset):
    """Uma chamada ao Ideris. Retorna o objeto de resposta ou None (erro de rede)."""
    try:
        return requests.get(BASE + f"/listingModel/search?limit={limit}&offset={offset}",
                            headers=H, timeout=60)
    except Exception as e:
        print(f"   erro de rede offset {offset}: {e}")
        return None


def guardar(batch, estoques):
    for item in batch:
        sku = item.get("sku")
        if sku and item.get("quantity") is not None:
            estoques[sku] = item.get("quantity")


def item_de(H, offset):
    """Lê 1 item numa posição (p/ pegar o vizinho da lista)."""
    if offset < 0:
        return None
    r = get(H, 1, offset)
    if r is not None and r.status_code == 200:
        obj = r.json().get("obj") or []
        return obj[0] if obj else None
    return None


def resumo_item(item):
    if not item:
        return "—"
    sku = item.get("sku") or "?"
    modelo = item.get("model") or item.get("title") or ""
    qtd = item.get("quantity")
    return f"SKU <b>{sku}</b>" + (f" — {modelo}" if modelo else "") + (f" — qtd {qtd}" if qtd is not None else "")


def coletar_estoque(token):
    """Retorna (estoques, problemas). Nunca interrompe por causa de um dado ruim."""
    H = {"Authorization": "Bearer " + token}
    estoques, problemas = {}, []
    limit, offset, total = 100, 0, None

    while total is None or offset < total:
        r = get(H, limit, offset)
        if r is not None and r.status_code == 200:
            data = r.json()
            total = data.get("total", total or 0)
            batch = data.get("obj", []) or []
            if not batch:
                offset += limit
                continue
            guardar(batch, estoques)
            offset += len(batch)
            time.sleep(1.3)               # respeita o limite (50 chamadas/min)
            continue

        # página falhou -> 1 tentativa extra (pode ser 500 passageiro)
        time.sleep(3)
        r = get(H, limit, offset)
        if r is not None and r.status_code == 200:
            data = r.json()
            total = data.get("total", total or 0)
            batch = data.get("obj", []) or []
            guardar(batch, estoques)
            offset += (len(batch) or limit)
            time.sleep(1.3)
            continue

        # ainda falhou -> varre item a item, pula só o problemático e ANOTA
        fim = offset + limit if total is None else min(offset + limit, total)
        print(f"Aviso: página no offset {offset} com problema — varrendo 1 a 1 (até {fim}).")
        last_good = item_de(H, offset - 1) if offset > 0 else None
        time.sleep(1.3)
        for o in range(offset, fim):
            r1 = get(H, 1, o)
            if r1 is not None and r1.status_code == 200:
                total = r1.json().get("total", total)
                obj = r1.json().get("obj") or []
                if obj:
                    guardar(obj, estoques)
                    last_good = obj[0]
            else:
                erro = (r1.text[:300] if r1 is not None else "sem resposta")
                m = re.search(r"input string '([^']*)'", erro)
                valor = m.group(1) if m else None
                time.sleep(1.3)
                depois = item_de(H, o + 1)
                problemas.append({"offset": o, "antes": last_good,
                                  "depois": depois, "valor": valor})
                print(f"   -> item travado no offset {o} (valor rejeitado: {valor})")
            time.sleep(1.3)
        offset += limit

    print(f"Coletado estoque de {len(estoques)} SKUs (de {total} modelos)")
    return estoques, problemas


def avisar_problemas(problemas):
    if not problemas:
        return
    partes = ["⚠️ <b>Produto(s) com estoque inválido no Ideris</b>",
              f"Achei {len(problemas)} item(ns) que travam a leitura "
              f"(quantidade digitada errada, ex.: a letra “O” no lugar do zero). "
              f"Atualizei o resto do estoque normalmente."]
    for i, p in enumerate(problemas[:10], 1):
        bloco = [f"\n<b>Problema {i}</b> (posição {p['offset']} na lista):"]
        if p["valor"]:
            bloco.append(f"Valor rejeitado: <b>{p['valor']}</b> (provável: troque “O” por “0”)")
        bloco.append("Está <b>ENTRE</b> estes dois produtos (mesma ordem da lista do Ideris):")
        bloco.append(f"• Antes:  {resumo_item(p['antes'])}")
        bloco.append(f"• Depois: {resumo_item(p['depois'])}")
        partes.append("\n".join(bloco))
    if len(problemas) > 10:
        partes.append(f"\n… e mais {len(problemas) - 10} item(ns).")
    partes.append("\n👉 Corrija no Ideris o produto entre esses vizinhos e o robô já pega na próxima.")
    tg_send("\n".join(partes))
    print("Aviso enviado ao Telegram.")


def main():
    token = login()
    estoques, problemas = coletar_estoque(token)

    if estoques:
        agora = datetime.now(timezone.utc).isoformat()
        linhas = [{"sku": s, "estoque_base": q, "estoque_sync_em": agora}
                  for s, q in estoques.items()]
        for i in range(0, len(linhas), 200):
            sb.table("produtos").upsert(linhas[i:i + 200], on_conflict="sku").execute()
        print(f"✅ Estoque atualizado: {len(linhas)} produtos em {agora}")
    else:
        print("⚠️ Nada coletado. Estoque não foi atualizado.")

    avisar_problemas(problemas)


if __name__ == "__main__":
    main()
