"""Helpers de Supabase compartilhados pelos dois sincronizadores."""

import os

DRY_RUN = os.environ.get("BL_DRY_RUN", "0") == "1"


def skus_existentes(sb):
    """Todos os SKUs que ja existem na tabela 'produtos'.
    Serve para detectar orfaos: SKU que esta no Supabase (veio do Ideris)
    mas nao veio da BaseLinker nesta rodada."""
    achados, passo, i = set(), 1000, 0
    while True:
        r = sb.table("produtos").select("sku").range(i, i + passo - 1).execute()
        linhas = r.data or []
        if not linhas:
            break
        achados.update(str(l["sku"]) for l in linhas if l.get("sku"))
        if len(linhas) < passo:
            break
        i += passo
    return achados


def relatar_orfaos(sb, vistos, rotulo, limite=25):
    """Compara o que veio da BaseLinker com o que ja existe no Supabase.
    Devolve (orfaos, novos) e imprime o resumo."""
    try:
        existentes = skus_existentes(sb)
    except Exception as e:
        print(f"Aviso: nao consegui listar SKUs do Supabase ({e}).")
        if "PGRST125" in str(e):
            print("   A SUPABASE_URL esta malformada (espaco, aspas ou /rest/v1).")
            print("   Apague %USERPROFILE%\\.supabase_creds e informe de novo.")
        print("   Pulo o relatorio de orfaos -- o resto do sync continua valendo.")
        return set(), set()

    orfaos = existentes - set(vistos)
    novos = set(vistos) - existentes

    print(f"\n--- {rotulo} ---")
    print(f"  no Supabase: {len(existentes)} | veio da BaseLinker: {len(vistos)}")

    if novos:
        print(f"  ➕ {len(novos)} SKU(s) novo(s), serao criados: "
              f"{', '.join(sorted(novos)[:limite])}"
              f"{' ...' if len(novos) > limite else ''}")

    if orfaos:
        print(f"  ⚠️ {len(orfaos)} SKU(s) existem no Supabase mas NAO vieram da "
              f"BaseLinker (cadastre la ou desative aqui):")
        print(f"     {', '.join(sorted(orfaos)[:limite])}"
              f"{' ...' if len(orfaos) > limite else ''}")
    else:
        print("  ✅ nenhum orfao: todo SKU do Supabase veio da BaseLinker.")

    return orfaos, novos


def orfaos_com_estoque(sb, orfaos, tamanho=200):
    """Dos SKUs orfaos, quais ainda tem estoque no Supabase.

    Por que isso importa: orfao zerado e' so' cadastro velho, nao faz mal a
    ninguem. Orfao COM estoque e' que e' perigoso -- o 'estoque_sync_em' dele
    congela e a view 'estoque_atual' fica subtraindo venda para sempre, sem
    nunca resetar. So esses merecem alerta no Telegram; alertar os 1.800
    zerados so' ensina voce a ignorar o aviso."""
    if not orfaos:
        return []
    achados, lista = [], sorted(orfaos)
    for i in range(0, len(lista), tamanho):
        bloco = lista[i:i + tamanho]
        try:
            r = (sb.table("produtos")
                 .select("sku,estoque_base")
                 .in_("sku", bloco)
                 .gt("estoque_base", 0)
                 .execute())
            achados.extend((str(l["sku"]), l.get("estoque_base"))
                           for l in (r.data or []) if l.get("sku"))
        except Exception as e:
            print(f"Aviso: nao consegui checar estoque dos orfaos ({e}).")
            return []
    return sorted(achados, key=lambda x: -float(x[1] or 0))


def upsert_blocos(sb, linhas, rotulo, tamanho=200):
    """Upsert em blocos, com respeito ao modo BL_DRY_RUN."""
    if not linhas:
        print(f"{rotulo}: nada a gravar.")
        return
    if DRY_RUN:
        print(f"[DRY RUN] {rotulo}: {len(linhas)} linhas NAO gravadas. "
              f"Amostra: {linhas[:3]}")
        return
    for i in range(0, len(linhas), tamanho):
        sb.table("produtos").upsert(linhas[i:i + tamanho], on_conflict="sku").execute()
    print(f"{rotulo}: {len(linhas)} produtos")
