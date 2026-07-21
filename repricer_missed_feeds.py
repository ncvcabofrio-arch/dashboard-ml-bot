"""
CHECK DE NOTIFICAÇÕES PERDIDAS (missed_feeds) + CONFERÊNCIA DOS PEDIDOS.

O ML guarda por ~2 dias as notificações que ele TENTOU entregar e não conseguiu (não recebeu
HTTP 200 após 8 tentativas / 1h). Este script:
  1) Consulta esse histórico (vazio = nada escapou; com itens = o que escapou).
  2) Pros PEDIDOS perdidos, confere se eles estão na sua fila_pedidos — porque a função grava
     na fila ANTES da chamada lenta ao GitHub, então "perdido pra ótica do ML" quase sempre
     significa que o pedido FOI gravado, só que o ML não recebeu o 200 a tempo e ficou re-tentando.

NÃO altera nada — só lê. Usa o mesmo token/segredos do resto do robô.

Env:
  APP_ID  (padrão = ML_CLIENT_ID; id da sua aplicação no ML)
  TOPIC   (opcional: filtra um tópico só, ex.: items_prices)
"""
import os
import json
import repricer_sugestoes as rec
from ml_auth import obter_access

sb = rec.sb
APP_ID = (os.environ.get("APP_ID") or os.environ.get("ML_CLIENT_ID") or "").strip()
TOPIC = (os.environ.get("TOPIC") or "").strip()


def token():
    """Pega um access token de qualquer conta do robô (o missed_feeds é a nível de aplicação)."""
    for seller_id, refresh in rec.contas():
        try:
            a, sid, rf = obter_access(sb, seller_id, refresh)
            return a
        except Exception as e:
            print(f"  aviso: token da conta {seller_id} falhou: {e}", flush=True)
    raise SystemExit("não consegui token de nenhuma conta")


def main():
    if not APP_ID:
        raise SystemExit("defina APP_ID (ou o secret ML_CLIENT_ID) — é o id da sua aplicação no ML")
    access = token()

    msgs, offset, limit = [], 0, 50
    for _ in range(60):   # teto de segurança (até 3000)
        path = f"/missed_feeds?app_id={APP_ID}&limit={limit}&offset={offset}"
        if TOPIC:
            path += f"&topic={TOPIC}"
        st, d = rec.get(path, access)
        if st != 200:
            print(f"HTTP {st} ao consultar missed_feeds: {json.dumps(d, ensure_ascii=False)[:220]}", flush=True)
            break
        batch = (d.get("messages") if isinstance(d, dict) else d) or []
        if not isinstance(batch, list) or not batch:
            break
        msgs += batch
        if len(batch) < limit:
            break
        offset += limit

    escopo = f" [tópico {TOPIC}]" if TOPIC else ""
    print(f"\n=== missed_feeds da aplicação {APP_ID}{escopo} ===", flush=True)
    print(f"Notificações PERDIDAS nas últimas ~48h: {len(msgs)}", flush=True)

    if not msgs:
        print("\n✓ NADA se perdeu. O webhook está respondendo 200 em tudo — pegando 100%.", flush=True)
        return

    porTopico, porCodigo, porConta = {}, {}, {}
    for m in msgs:
        t = m.get("topic") or "?"
        porTopico[t] = porTopico.get(t, 0) + 1
        code = (m.get("response") or {}).get("http_code", "?")
        porCodigo[code] = porCodigo.get(code, 0) + 1
        u = m.get("user_id") or "?"
        porConta[u] = porConta.get(u, 0) + 1

    print("\nPor tópico:        ", json.dumps(porTopico, ensure_ascii=False), flush=True)
    print("Por código HTTP:   ", json.dumps(porCodigo, ensure_ascii=False),
          "  (o código que SEU servidor devolveu; ML só para de tentar com 200)", flush=True)
    print("Por conta:         ", json.dumps(porConta, ensure_ascii=False), flush=True)

    print("\nExemplos (até 10):", flush=True)
    for m in msgs[:10]:
        resp = m.get("response") or {}
        print(f"  {str(m.get('sent',''))[:19]} | {m.get('topic')} | {str(m.get('resource',''))[:44]} "
              f"| http {resp.get('http_code')} | tentativas {m.get('attempts')}", flush=True)

    # ---- CONFERÊNCIA: os pedidos "perdidos" pra ótica do ML estão na sua fila_pedidos? ----
    oids = []
    for m in msgs:
        if str(m.get("topic", "")).startswith("orders"):
            oid = str(m.get("resource", "")).rstrip("/").split("/")[-1]
            if oid and oid not in oids:
                oids.append(oid)
    if oids:
        print(f"\n--- Conferência: {len(oids)} pedido(s) perdido(s) pelo ML x sua fila_pedidos ---", flush=True)
        achados = set()
        erro_leitura = None
        try:
            r = sb.table("fila_pedidos").select("order_id").in_("order_id", oids).execute()
            achados = {str(x.get("order_id")) for x in (r.data or [])}
        except Exception as e:
            erro_leitura = str(e)
        if achados:
            falta = [o for o in oids if o not in achados]
            print(f"  NA FILA: {len(achados)}/{len(oids)}  |  FALTANDO: {len(falta)}", flush=True)
            if falta:
                print("  ⚠️ pedidos que NÃO estão na fila (esses SIM se perderam):", ", ".join(falta), flush=True)
            else:
                print("  ✓ TODOS os pedidos que o ML deu como perdidos ESTÃO na sua fila — não perdeu nenhum.", flush=True)
        else:
            # a chave do script provavelmente não vê a fila_pedidos (RLS). Entrega o SQL pro Editor.
            if erro_leitura:
                print(f"  (não li a fila_pedidos direto: {erro_leitura})", flush=True)
            print("  Não confirmei pela chave do script (a fila_pedidos tem RLS ligada).", flush=True)
            print("  COLE este SQL no SQL Editor do Supabase (ele ignora RLS) pra conferir na hora:\n", flush=True)
            vals = ",".join(f"('{o}')" for o in oids)
            print("  select v.oid as pedido,\n"
                  "         (select count(*) from public.fila_pedidos f where f.order_id = v.oid) as na_fila\n"
                  f"  from (values {vals}) as v(oid)\n"
                  "  order by na_fila;", flush=True)
            print("\n  (na_fila = 0 => aquele pedido NÃO está na fila; esse teria se perdido de verdade.)", flush=True)

    print("\nLeitura: 'perdida' aqui é da ótica do ML (ele não recebeu o 200 a tempo). A conferência "
          "acima diz se o pedido chegou na sua fila mesmo assim.", flush=True)


if __name__ == "__main__":
    main()
