"""
Planilha de ESTOQUE x RESERVA da BaseLinker.  SO LE, nao grava nada.

Para que serve
--------------
Quando um produto e' vendido com entrega para depois, a peca continua na
prateleira mas ja tem dono. Na contagem fisica ela e' contada de novo e o
inventario incha. Esta planilha poe o fisico e o reservado lado a lado para
o conferente saber quanto abater.

Colunas
   sku, nome, estoque, reservado, disponivel, product_id, ean, e_variante

   estoque     soma do 'stock' de todos os depositos
   reservado   quantidade ja comprometida com pedidos na BaseLinker
   disponivel  estoque - reservado
   e_variante  1 quando a linha e' variante de outro produto

Roda sozinho no GitHub Actions (o token vem do secret BASELINKER_TOKEN) e
sobe a planilha como artefato para download. Tambem roda local:
   BASELINKER_TOKEN=xxx python3 bl_export_estoque.py
"""

import json
import os
import sys
import time

import requests

API = "https://api.baselinker.com/connector.php"
TOKEN = (os.environ.get("BASELINKER_TOKEN") or "").strip()
INVENTORY = (os.environ.get("BL_INVENTORY_ID") or "").strip()
SAIDA = os.environ.get("BL_SAIDA", "estoque_reservas.xlsx")

# depositos a considerar; vazio = soma todos
WAREHOUSES = [w.strip() for w in os.environ.get("BL_WAREHOUSES", "").split(",") if w.strip()]

if not TOKEN:
    print("[ERRO] Falta o BASELINKER_TOKEN.")
    sys.exit(1)


def chamar(metodo, parametros=None, tentativas=4):
    """A BaseLinker responde HTTP 200 mesmo quando da erro -- quem manda e' o
    campo 'status' do corpo. Por isso a checagem e' aqui, nao no status HTTP."""
    corpo = {"method": metodo,
             "parameters": json.dumps(parametros or {}, ensure_ascii=False)}
    for t in range(tentativas):
        try:
            r = requests.post(API, headers={"X-BLToken": TOKEN}, data=corpo, timeout=90)
        except requests.RequestException as e:
            if t == tentativas - 1:
                raise
            print(f"   rede falhou ({e}); tentando de novo...")
            time.sleep(2 * (t + 1))
            continue

        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(3 * (t + 1))
            continue

        d = r.json()
        if d.get("status") == "ERROR":
            code = d.get("error_code", "")
            if code in ("ERROR_RATE_LIMIT", "ERROR_INTERNAL") and t < tentativas - 1:
                time.sleep(3 * (t + 1))
                continue
            raise RuntimeError(f"{metodo}: {code} — {d.get('error_message')}")
        return d
    raise RuntimeError(f"{metodo}: nao respondeu depois de {tentativas} tentativas")


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def somar(mapa):
    total = 0.0
    for wid, qtd in (mapa or {}).items():
        if WAREHOUSES and wid not in WAREHOUSES:
            continue
        total += num(qtd)
    return total


def limpo(n):
    """Mostra 8 em vez de 8.0, mas preserva 8.5."""
    n = float(n)
    return int(n) if n.is_integer() else round(n, 2)


def inventario():
    if INVENTORY:
        return int(INVENTORY)
    invs = chamar("getInventories").get("inventories", []) or []
    if not invs:
        raise RuntimeError("nenhum catalogo encontrado nesta conta")
    padrao = next((i for i in invs if i.get("is_default")), invs[0])
    return int(padrao["inventory_id"])


def paginar(metodo, inv_id, rotulo):
    """getInventoryProductsList e getInventoryProductsStock sao os dois
    paginados por 'page', 1000 por vez."""
    out, page = {}, 1
    while True:
        d = chamar(metodo, {"inventory_id": inv_id, "page": page})
        prods = d.get("products") or {}
        if not prods:
            break
        out.update(prods)
        print(f"  {rotulo}: pagina {page} -> {len(prods)} itens (acumulado {len(out)})")
        if len(prods) < 1000:
            break
        page += 1
        time.sleep(0.65)          # limite da API e' 100 chamadas/min
    return out


def reservas_por_id(estoques):
    """Achata o retorno de getInventoryProductsStock em {id: {deposito: qtd}},
    incluindo as variantes -- que a BaseLinker devolve aninhadas no pai, e
    dependendo da versao da conta vem em 'variants' ou 'variant_reservations'."""
    mapa = {}
    for pid, p in (estoques or {}).items():
        mapa[str(pid)] = p.get("reservations") or {}
        aninhadas = p.get("variants") or p.get("variant_reservations") or {}
        if isinstance(aninhadas, dict):
            for vid, v in aninhadas.items():
                if isinstance(v, dict) and "reservations" in v:
                    mapa[str(vid)] = v.get("reservations") or {}
                elif isinstance(v, dict):
                    mapa[str(vid)] = v          # ja e' {deposito: qtd}
    return mapa


def main():
    inv_id = inventario()
    print(f"Catalogo BaseLinker: {inv_id} | depositos: "
          f"{', '.join(WAREHOUSES) if WAREHOUSES else 'TODOS'}")

    print("Lendo catalogo...")
    lista = paginar("getInventoryProductsList", inv_id, "lista")

    print("Lendo estoque e reservas...")
    reservas = {}
    try:
        reservas = reservas_por_id(paginar("getInventoryProductsStock", inv_id, "estoque"))
    except Exception as e:
        print(f"⚠️ Nao consegui ler as reservas ({e}). A coluna vai zerada — "
              f"me avise, porque isso muda a conclusao.")

    linhas = []
    for pid, p in lista.items():
        sku = p.get("sku")
        if not sku:
            continue
        est = somar(p.get("stock"))
        res = somar(reservas.get(str(pid)))
        linhas.append([
            str(sku),
            p.get("name") or "",
            limpo(est),
            limpo(res),
            limpo(est - res),
            pid,
            p.get("ean") or "",
            1 if p.get("parent_id") else 0,
        ])

    linhas.sort(key=lambda l: (-float(l[3]), l[0]))   # reservados primeiro

    com_reserva = [l for l in linhas if float(l[3]) > 0]
    total_res = sum(float(l[3]) for l in com_reserva)
    negativos = [l for l in linhas if float(l[4]) < 0]

    cab = ["sku", "nome", "estoque", "reservado", "disponivel",
           "product_id", "ean", "e_variante"]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Estoque x Reserva"
        ws.append(cab)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="DDDDDD")
        destaque = PatternFill("solid", fgColor="FFF2CC")
        for l in linhas:
            ws.append(l)
            if float(l[3]) > 0:
                for c in ws[ws.max_row]:
                    c.fill = destaque
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cab))}{len(linhas)+1}"
        for i, w in enumerate([22, 70, 10, 11, 12, 13, 16, 11], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        wb.save(SAIDA)
        saida = SAIDA
    except ImportError:
        import csv
        saida = SAIDA.replace(".xlsx", ".csv")
        with open(saida, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(cab)
            w.writerows(linhas)

    print("\n" + "=" * 60)
    print("RESULTADO")
    print("=" * 60)
    print(f"  SKUs no catalogo ............... {len(linhas)}")
    print(f"  SKUs COM RESERVA .............. {len(com_reserva)}")
    print(f"  unidades reservadas ........... {limpo(total_res)}")
    if negativos:
        print(f"  ⚠️ disponivel negativo ......... {len(negativos)} "
              f"(vendeu mais do que tem em estoque)")
    print()
    if com_reserva:
        print("  A BaseLinker ESTA controlando reserva. Os 10 maiores:")
        for l in com_reserva[:10]:
            print(f"     {l[0]:<18} estoque={l[2]:<6} reservado={l[3]:<6} "
                  f"disponivel={l[4]:<6} {str(l[1])[:40]}")
    else:
        print("  Nenhuma reserva encontrada. Ou nao ha pedido pendente agora,")
        print("  ou os pedidos do Mercado Livre nao estao entrando no modulo de")
        print("  pedidos da BaseLinker — nesse caso a informacao tem que sair")
        print("  do Supabase, pelo status de envio.")
    print(f"\n✅ {saida}")


if __name__ == "__main__":
    main()
