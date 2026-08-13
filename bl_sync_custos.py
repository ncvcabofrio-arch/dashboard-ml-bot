"""
Sincronizador BaseLinker -> Supabase (roda 1x/dia).
Atualiza CUSTO, NOME e MODELO de cada produto.

Porte do antigo sincronizador do Ideris. O que mudou e o que NAO mudou:

  MUDOU (fonte dos dados)
  - Nao existe mais login: o token vai no header X-BLToken a cada chamada.
  - Nao existe mais a divisao "modelo de anuncio" x "produto". Na BaseLinker
    o catalogo (inventory) ja e' a fonte completa: cobre produto sem anuncio
    ativo. Ou seja, o papel que /sku/search fazia agora e' do catalogo, e o
    reforco via /listingModel deixou de ser necessario.
  - CUSTO vem de average_landed_cost (custo medio COM frete/impostos) ou
    average_cost, configuravel em BL_CAMPO_CUSTO. Tambem da' para ler de um
    grupo de precos ou do cadastro de fornecedor.
  - NOME vem de text_fields['name'].
  - MODELO nao tem campo nativo na BaseLinker. Configure BL_CAMPO_MODELO
    apontando para um campo extra (extra_field_123), para o fabricante ou
    para as tags. Rode bl_diag.py para descobrir qual e'.

  NAO MUDOU (as travas que voce ja tinha)
  - So grava custo REAL (> 0). Zero e nulo sao "sem custo" e NAO sobrescrevem
    custo digitado a mao. Foi essa trava que faltava em 01/08 07:31.
  - Nao aborta por causa de um registro ruim: se um lote falhar, divide o lote
    ao meio ate isolar e pular so o produto problematico.
  - Upsert em blocos de 200, on_conflict="sku".
  - Chama a funcao backfill_custos no fim, para congelar custo nas vendas que
    ainda estao sem.
  - NAO mexe em estoque: quem cuida disso e' o sync_estoque.py.
"""

import os

from supabase import create_client

from baselinker import BaseLinker, feature, num, texto
from bl_supa import DRY_RUN, relatar_orfaos, upsert_blocos

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

# de onde sai o custo: average_landed_cost | average_cost | price_group:<id> | supplier
CAMPO_CUSTO = os.environ.get("BL_CAMPO_CUSTO", "average_landed_cost")
# de onde sai o modelo: extra_field_<id> | manufacturer | tags | "" (nao atualiza)
# Enquanto voce nao cadastrar o campo Modelo no painel da BaseLinker, deixe
# VAZIO: assim a coluna 'modelo' que ja existe no Supabase fica intacta.
CAMPO_MODELO = os.environ.get("BL_CAMPO_MODELO", "").strip()
# idioma dos textos. Na sua conta o codigo e' 'br' (nao 'pt').
IDIOMA = os.environ.get("BL_IDIOMA", "br").strip() or None
# variantes entram como SKUs proprios? (no Ideris cada SKU era uma linha)
INCLUIR_VARIANTES = os.environ.get("BL_INCLUIR_VARIANTES", "1") != "0"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# --------------------------------------------------------------------- custo

def extrair_custo(prod):
    """Custo do produto como NUMERO, ou 0 quando nao der para ler.
    Devolver 0 no que nao der para ler e' de proposito: quem chama so grava
    quando for maior que zero, entao valor duvidoso nunca sobrescreve custo bom."""
    if CAMPO_CUSTO.startswith("price_group:"):
        gid = CAMPO_CUSTO.split(":", 1)[1].strip()
        precos = prod.get("prices") or {}
        return num(precos.get(gid) if gid in precos else precos.get(int(gid) if gid.isdigit() else gid))

    if CAMPO_CUSTO == "supplier":
        custos = [num(s.get("cost")) for s in (prod.get("suppliers") or [])]
        custos = [c for c in custos if c > 0]
        return min(custos) if custos else 0.0

    valor = num(prod.get(CAMPO_CUSTO))
    if valor <= 0 and CAMPO_CUSTO == "average_landed_cost":
        # landed cost so existe quando ha documento de entrada com frete.
        # cai para o custo medio simples em vez de devolver zero.
        valor = num(prod.get("average_cost"))
    return valor


# -------------------------------------------------------------------- modelo

def extrair_modelo(prod, fabricantes):
    if not CAMPO_MODELO:
        return None

    # ficha tecnica: BL_CAMPO_MODELO="feature:Modelo detalhado"
    if CAMPO_MODELO.startswith("feature:"):
        nome = CAMPO_MODELO.split(":", 1)[1].strip()
        return feature(prod.get("text_fields") or {}, nome, IDIOMA)

    if CAMPO_MODELO == "manufacturer":
        mid = prod.get("manufacturer_id")
        nome = fabricantes.get(str(mid)) if mid else None
        return nome.strip() if isinstance(nome, str) and nome.strip() else None

    if CAMPO_MODELO == "tags":
        tags = prod.get("tags")
        if isinstance(tags, list) and tags:
            return str(tags[0]).strip() or None
        if isinstance(tags, str) and tags.strip():
            return tags.strip()
        return None

    # extra_field_<id> ou qualquer chave de text_fields
    v = texto(prod.get("text_fields") or {}, CAMPO_MODELO, IDIOMA)
    return v


# --------------------------------------------------------------------- coleta

def coletar(bl, inv_id):
    """Devolve custos, nomes e modelos (todos {sku: valor}) e a lista de
    produtos que a BaseLinker nao conseguiu devolver."""
    print("Listando catalogo...")
    lista = bl.lista_produtos(inv_id, include_variants=INCLUIR_VARIANTES)

    # Variantes nao tem custo proprio: pego os dados no produto pai.
    # (na lista, variante vem com parent_id preenchido)
    ids_pai = sorted({
        int(p.get("parent_id") or pid)
        for pid, p in lista.items()
    })
    print(f"Catalogo: {len(lista)} itens, {len(ids_pai)} produtos-pai a ler.")

    fabricantes = {}
    if CAMPO_MODELO == "manufacturer":
        fabricantes = {
            str(m.get("manufacturer_id")): m.get("name")
            for m in bl.manufacturers()
        }

    dados, pulados = bl.dados_produtos(
        inv_id, ids_pai,
        include_suppliers=(CAMPO_CUSTO == "supplier"),
    )

    custos, nomes, modelos = {}, {}, {}

    for pid, prod in dados.items():
        sku = prod.get("sku")
        custo = extrair_custo(prod)
        nome = texto(prod.get("text_fields") or {}, "name", IDIOMA)
        modelo = extrair_modelo(prod, fabricantes)

        if sku:
            # So grava custo REAL (> 0). Zero e nulo nao sobrescrevem.
            if custo > 0:
                custos[sku] = custo
            if nome:
                nomes[sku] = nome
            if modelo:
                modelos[sku] = modelo

        if not INCLUIR_VARIANTES:
            continue

        for _vid, v in (prod.get("variants") or {}).items():
            vsku = v.get("sku")
            if not vsku:
                continue
            if custo > 0:                       # variante herda o custo do pai
                custos[vsku] = custo
            vnome = v.get("name") or nome
            if isinstance(vnome, str) and vnome.strip():
                nomes[vsku] = vnome.strip()
            if modelo:
                modelos[vsku] = modelo

    if pulados:
        print(f"⚠️ {len(pulados)} produto(s) pulado(s) por erro na BaseLinker "
              f"— CORRIJA no painel:")
        for p in pulados:
            print(f"   - product_id {p['product_id']} | {p['detalhe']}")

    print(f"Coletado: {len(custos)} custos, {len(nomes)} nomes e "
          f"{len(modelos)} modelos.")
    return custos, nomes, modelos


# ----------------------------------------------------------------- gravacao

def main():
    bl = BaseLinker()
    inv_id = bl.inventory_id_padrao()
    print(f"Catalogo BaseLinker: inventory_id={inv_id} | "
          f"custo={CAMPO_CUSTO} | modelo={CAMPO_MODELO or '(nao atualiza)'}"
          + ("  [DRY RUN: nao grava nada]" if DRY_RUN else ""))

    custos, nomes, modelos = coletar(bl, inv_id)

    if not custos and not nomes:
        print("⚠️ Nada coletado. Me avise para ajustar.")
        return

    # 1b) confere se algum SKU do Supabase ficou de fora da BaseLinker
    relatar_orfaos(sb, set(nomes) | set(custos), "Cobertura de SKUs")

    # 2) atualiza CUSTO (nao toca no estoque)
    upsert_blocos(sb, [{"sku": s, "custo": c} for s, c in custos.items()],
                  "Custo atualizado")

    # 3) atualiza NOME com o nome exato da BaseLinker
    upsert_blocos(sb, [{"sku": s, "nome": n} for s, n in nomes.items()],
                  "Nome atualizado")

    # 3b) atualiza MODELO. Sem BL_CAMPO_MODELO configurado nao grava nada --
    # o que ja esta na coluna 'modelo' do Supabase fica intacto.
    if modelos:
        upsert_blocos(sb, [{"sku": s, "modelo": m} for s, m in modelos.items()],
                      "Modelo atualizado")
    else:
        print("Modelo: nao atualizado (BL_CAMPO_MODELO vazio). "
              "A coluna 'modelo' do Supabase permanece como esta.")

    # 4) congela o custo nas vendas que ainda estao sem (nunca sobrescreve)
    if DRY_RUN:
        print("[DRY RUN] backfill_custos nao foi chamado.")
        return
    try:
        sb.rpc("backfill_custos").execute()
        print("Backfill de custo nas vendas concluido.")
    except Exception as e:
        print("Aviso: backfill falhou:", e)


if __name__ == "__main__":
    main()
