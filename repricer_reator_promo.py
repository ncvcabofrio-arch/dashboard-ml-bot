"""
REATOR DE PROMOÇÕES — atualiza o painel "quase em tempo real" a partir das
notificações do Mercado Livre. SOMENTE LEITURA no ML; não aplica nada.

Como funciona:
  1) O webhook (Edge Function) já grava TODA notificação (menos pedidos) em
     repricer_notificacoes, com o item_id extraído do resource.
  2) Este reator lê as notificações de PROMOÇÃO ainda não reagidas
     (public_offers, public_candidates, catalog_item_competition_status, items_prices),
     pega os anúncios distintos, e REPROCESSA só esses — reaproveitando a MESMA
     lógica do repricer_sugestoes (processar_item) — atualizando o repricer_sugestoes
     daquele anúncio (campo 'ofertas' do box, ação sugerida, etc.).
  3) Marca as notificações desses anúncios como processado=true.

Roda de X em X minutos (agendado). O repricer_sugestoes COMPLETO, rodando poucas
vezes ao dia, é a rede de segurança (pega o que a notificação perder).

Env: ML_* e SUPABASE_* (os mesmos secrets). Opcional: MAX_ITENS_REATOR (default 600).
"""
import os
import repricer_sugestoes as rec
from ml_auth import obter_access

# tópicos que mexem no que o box mostra
NOTIF_TOPICS = ["public_offers", "public_candidates",
                "catalog_item_competition_status", "items_prices"]
# teto de anúncios por rodada (o resto fica pra próxima; nunca marca o que não processou)
MAX_ITENS = int(os.environ.get("MAX_ITENS_REATOR", "600"))
# quantas notificações pendentes ler por rodada (bem acima do teto de itens)
LIMITE_NOTIF = int(os.environ.get("LIMITE_NOTIF_REATOR", "6000"))


def _chunks(lst, n=100):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def pendentes():
    """Notificações de promoção ainda não reagidas (mais antigas primeiro)."""
    try:
        rows = (rec.sb.table("repricer_notificacoes")
                .select("item_id,user_id,topic,received")
                .eq("processado", False)
                .in_("topic", NOTIF_TOPICS)
                .order("received")
                .limit(LIMITE_NOTIF).execute().data) or []
        return rows
    except Exception as e:
        print(f"erro lendo repricer_notificacoes: {e}", flush=True)
        return []


def marcar_processadas(item_ids, tudo_sem_item=False):
    """Marca como processado=true as notificações de promoção desses anúncios.
    Se tudo_sem_item=True, marca também as pendentes SEM item_id (não dá pra usar)."""
    for lote in _chunks(list(item_ids), 100):
        try:
            (rec.sb.table("repricer_notificacoes").update({"processado": True})
             .eq("processado", False).in_("topic", NOTIF_TOPICS)
             .in_("item_id", lote).execute())
        except Exception as e:
            print(f"  aviso: falha ao marcar lote: {e}", flush=True)
    if tudo_sem_item:
        try:
            (rec.sb.table("repricer_notificacoes").update({"processado": True})
             .eq("processado", False).in_("topic", NOTIF_TOPICS)
             .is_("item_id", "null").execute())
        except Exception as e:
            print(f"  aviso: falha ao marcar sem-item: {e}", flush=True)


def gravar_item(sid, item_id, sug):
    """Substitui a linha do anúncio no repricer_sugestoes (apaga a não-aplicada e
    insere a nova). Se sug=None (anúncio sem promoção agora), só apaga — o box esvazia."""
    try:
        (rec.sb.table("repricer_sugestoes").delete()
         .neq("status", "aplicada").eq("seller_id", str(sid))
         .eq("item_id", item_id).execute())
    except Exception as e:
        print(f"  aviso: falha ao limpar {item_id}: {e}", flush=True)
    if sug:
        try:
            rec.sb.table("repricer_sugestoes").insert(sug).execute()
        except Exception as e:
            print(f"  aviso: falha ao gravar {item_id}: {e}", flush=True)


def main():
    rows = pendentes()
    if not rows:
        print("Reator: nada pendente — painel já está em dia.", flush=True)
        return
    # anúncios distintos (preservando a ordem de chegada), com o seller de cada um
    seller_de = {}
    ordem = []
    for r in rows:
        iid = r.get("item_id")
        if not iid:
            continue
        if iid not in seller_de:
            seller_de[iid] = str(r.get("user_id") or "")
            ordem.append(iid)
    if not ordem:
        # só vieram notificações sem item_id utilizável — marca e sai
        marcar_processadas([], tudo_sem_item=True)
        print("Reator: só notificações sem item_id — marcadas, nada a atualizar.", flush=True)
        return

    total_pend = len(ordem)
    ordem = ordem[:MAX_ITENS]                      # teto por rodada
    print(f"Reator: {total_pend} anúncio(s) com mudança; processando {len(ordem)} nesta rodada.", flush=True)

    rec.preload()                                  # custos/grupos (uma vez)
    tokens = {str(sid): rt for sid, rt in rec.contas() if sid}

    # agrupa por conta (um access por conta)
    por_conta = {}
    for iid in ordem:
        por_conta.setdefault(seller_de.get(iid, ""), []).append(iid)

    feitos, com_promo = [], 0
    for sid, iids in por_conta.items():
        refresh = tokens.get(str(sid))
        if not refresh:
            print(f"  conta {sid} sem token — pulando {len(iids)} anúncio(s).", flush=True)
            continue
        try:
            access, sid_ok, refresh = obter_access(rec.sb, sid, refresh)
        except Exception as e:
            print(f"  conta {sid}: token falhou ({e}) — pulando.", flush=True)
            continue
        for iid in iids:
            try:
                sug = rec.processar_item(iid, access, sid_ok, {})
            except Exception as e:
                print(f"  erro ao processar {iid}: {e}", flush=True)
                continue                            # não marca: tenta de novo na próxima
            gravar_item(sid_ok, iid, sug)
            feitos.append(iid)
            if sug:
                com_promo += 1

    # marca processadas só as que REALMENTE reprocessei (+ as inúteis sem item_id)
    if feitos:
        marcar_processadas(feitos, tudo_sem_item=True)
    print(f"Reator: {len(feitos)} anúncio(s) atualizados ({com_promo} com promoção, "
          f"{len(feitos) - com_promo} sem). Nada foi aplicado no ML.", flush=True)


if __name__ == "__main__":
    main()
