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
  4) NOVO: sincroniza STATUS/ESTOQUE a partir do tópico 'items' — mantém a fila
     Pausados do painel em dia em tempo real (pausa real entra; voltou a ativo sai
     e vira ativo; sem estoque fica fora).
Roda de X em X minutos (agendado). O repricer_sugestoes COMPLETO, rodando poucas
vezes ao dia, é a rede de segurança (pega o que a notificação perder).
Env: ML_* e SUPABASE_* (os mesmos secrets). Opcional: MAX_ITENS_REATOR (default 600).
"""
import os
from datetime import datetime, timezone
import repricer_sugestoes as rec
from ml_auth import obter_access
# tópicos que mexem no que o box mostra
NOTIF_TOPICS = ["public_offers", "public_candidates",
                "catalog_item_competition_status", "items_prices"]
# tópicos que mexem no STATUS do anúncio (ativo/pausado/estoque)
STATUS_TOPICS = ["items"]
# teto de anúncios por rodada (o resto fica pra próxima; nunca marca o que não processou)
MAX_ITENS = int(os.environ.get("MAX_ITENS_REATOR", "600"))
# quantas notificações pendentes ler por rodada (bem acima do teto de itens)
LIMITE_NOTIF = int(os.environ.get("LIMITE_NOTIF_REATOR", "6000"))
def _agora():
    return datetime.now(timezone.utc).isoformat()
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
def reagir_promocoes(tokens):
    rows = pendentes()
    if not rows:
        print("Reator: nada pendente de promoção.", flush=True)
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
# ============================ SINCRONIZAÇÃO DE STATUS ============================
def pendentes_status():
    try:
        rows = (rec.sb.table("repricer_notificacoes")
                .select("item_id,user_id,topic,received")
                .eq("processado", False).in_("topic", STATUS_TOPICS)
                .order("received").limit(LIMITE_NOTIF).execute().data) or []
        return rows
    except Exception as e:
        print(f"erro lendo notificações de status: {e}", flush=True)
        return []
def marcar_status(item_ids, tudo_sem_item=False):
    for lote in _chunks(list(item_ids), 100):
        try:
            (rec.sb.table("repricer_notificacoes").update({"processado": True})
             .eq("processado", False).in_("topic", STATUS_TOPICS)
             .in_("item_id", lote).execute())
        except Exception as e:
            print(f"  aviso: falha ao marcar status: {e}", flush=True)
    if tudo_sem_item:
        try:
            (rec.sb.table("repricer_notificacoes").update({"processado": True})
             .eq("processado", False).in_("topic", STATUS_TOPICS)
             .is_("item_id", "null").execute())
        except Exception as e:
            print(f"  aviso: falha ao marcar status sem-item: {e}", flush=True)
def _detalhe_itens(iids, access, attrs="id,status,sub_status,seller_sku,seller_custom_field,title"):
    out = {}
    for i in range(0, len(iids), 20):
        chunk = iids[i:i + 20]
        st, d = rec.get(f"/items?ids={','.join(chunk)}&attributes={attrs}", access)
        if isinstance(d, list):
            for r in d:
                b = r.get("body") if isinstance(r, dict) else None
                if isinstance(b, dict) and b.get("id"):
                    out[b["id"]] = b
    return out
def sincronizar_status(tokens):
    """Mantém a fila Pausados (repricer_status_ml) em dia em tempo real.
    Pausa real (paused e NÃO out_of_stock) -> entra. Voltou a ativo -> sai e vira ativo
    (linha no repricer_log). Sem estoque -> fica fora. Só mexe no que mudou de estado."""
    rows = pendentes_status()
    if not rows:
        return
    seller_de, ordem = {}, []
    for r in rows:
        iid = r.get("item_id")
        if not iid:
            continue
        if iid not in seller_de:
            seller_de[iid] = str(r.get("user_id") or "")
            ordem.append(iid)
    if not ordem:
        marcar_status([], tudo_sem_item=True)
        return
    ordem = ordem[:MAX_ITENS]
    por_conta = {}
    for iid in ordem:
        por_conta.setdefault(seller_de.get(iid, ""), []).append(iid)
    feitos, virou_ativo, virou_pausa = [], 0, 0
    for sid, iids in por_conta.items():
        refresh = tokens.get(str(sid))
        if not refresh:
            continue
        try:
            access, sid_ok, refresh = obter_access(rec.sb, sid, refresh)
        except Exception as e:
            print(f"  status conta {sid}: token falhou ({e})", flush=True)
            continue
        det = _detalhe_itens(iids, access)
        # o que já está na fila Pausados desta conta (pra detectar mudança de estado)
        existentes = set()
        try:
            r = (rec.sb.table("repricer_status_ml").select("item_id")
                 .eq("seller_id", str(sid_ok)).in_("item_id", iids).execute().data) or []
            for x in r:
                existentes.add(x["item_id"])
        except Exception:
            pass
        add, rem, logs = [], [], []
        for iid in iids:
            b = det.get(iid)
            if not b:
                feitos.append(iid)          # item sumiu/erro — marca pra não repetir
                continue
            status = (b.get("status") or "").lower()
            ss = b.get("sub_status") or []
            sku = b.get("seller_sku") or b.get("seller_custom_field")
            if status == "paused" and "out_of_stock" not in ss:
                add.append({"seller_id": str(sid_ok), "item_id": iid, "sku": sku, "titulo": b.get("title")})
                virou_pausa += 1
            elif iid in existentes:            # estava em Pausados e não está mais
                rem.append(iid)
                if status == "active":
                    logs.append({"seller_id": str(sid_ok), "item_id": iid, "sku": sku,
                                 "titulo": b.get("title"), "acao": "ativo", "aplicado": False,
                                 "modo": "insight", "ts": _agora()})
                    virou_ativo += 1
            feitos.append(iid)
        for row in add:
            try:
                rec.sb.table("repricer_status_ml").upsert(row, on_conflict="seller_id,item_id").execute()
            except Exception as e:
                print(f"  aviso upsert pausado {row['item_id']}: {e}", flush=True)
        for lote in _chunks(rem, 100):
            try:
                (rec.sb.table("repricer_status_ml").delete()
                 .eq("seller_id", str(sid_ok)).in_("item_id", lote).execute())
            except Exception as e:
                print(f"  aviso remover pausado: {e}", flush=True)
        for row in logs:
            try:
                rec.sb.table("repricer_log").insert(row).execute()
            except Exception as e:
                print(f"  aviso log ativo {row['item_id']}: {e}", flush=True)
    if feitos:
        marcar_status(feitos, tudo_sem_item=True)
    print(f"Reator status: {len(feitos)} item(ns) — {virou_ativo} voltaram ativos, "
          f"{virou_pausa} pausados de verdade.", flush=True)
def main():
    tokens = {str(sid): rt for sid, rt in rec.contas() if sid}
    reagir_promocoes(tokens)
    try:
        sincronizar_status(tokens)
    except Exception as e:
        print(f"Reator status: erro geral ({e}) — segue sem sincronizar status.", flush=True)
if __name__ == "__main__":
    main()
