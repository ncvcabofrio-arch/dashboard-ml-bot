"""
CHECK DE NOTIFICAÇÕES PERDIDAS (missed_feeds) — confirma se o webhook está pegando TUDO.

O ML guarda por ~2 dias as notificações que ele TENTOU entregar e não conseguiu (não recebeu
HTTP 200 após 8 tentativas / 1h). Este script consulta esse histórico:
  - VAZIO  -> prova de que nada está escapando (o webhook responde 200 em tudo).
  - COM ITENS -> são exatamente as que se perderam; mostra por tópico e por código HTTP,
    pra sabermos o que corrigir (e se algum tópico foi desativado por falha).

NÃO altera nada — só lê. Usa o mesmo token/segredos do resto do robô.

Env:
  APP_ID  (padrão = ML_CLIENT_ID; é o id da sua aplicação no ML)
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

    # o que se perdeu, por tópico e pelo código que o SEU servidor devolveu
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

    print("\nLeitura: essas são as que ESCAPARAM. Se aparecerem tópicos que te interessam, vale "
          "investigar por que o servidor não respondeu 200 nelas (e reinscrever o tópico se ele "
          "tiver sido desativado por falha).", flush=True)


if __name__ == "__main__":
    main()
