"""
DIAGNÓSTICO DA SAÍDA (sair de promoções): descobre POR QUE o item continua ATIVO em
promoções depois do 'trocar'. NÃO é fase nova — só investiga, sem mudar nada na produção.

Usa EXATAMENTE a lógica de saída de produção (ap.remover_participacao / ap.participacoes),
então o que ele mostrar é o que o aplicador realmente faz.

Dois modos (env APLICAR):
  APLICAR=0 (padrão, SEGURO): só LISTA as participações ATIVAS e MOSTRA o DELETE exato que
            o robô enviaria pra cada uma. NÃO envia nada. Se faltar offer_id numa campanha que
            o ML exige, ele grita — essa é a causa clássica de "200 sem remover".
  APLICAR=1: ENVIA o DELETE de verdade (mesma função da produção) e CAPTURA o que o ML
            responde (HTTP + corpo) pra CADA promoção, depois RE-LISTA pra ver o que saiu.

Env:
  ITEM_ID   (padrão MLB5281709396 — o que tinha 4 promoções sobrando)
  SELLER_ID (padrão 177795203)
  APLICAR   (0/1)
"""
import os
import json
import repricer_sugestoes as rec
import repricer_promo_aplicar as ap
from ml_auth import obter_access

sb = rec.sb
ITEM = (os.environ.get("ITEM_ID") or "MLB5281709396").strip()
SELLER = (os.environ.get("SELLER_ID") or "177795203").strip()
APLICAR = (os.environ.get("APLICAR", "0").strip() == "1")


def token():
    for seller_id, refresh in rec.contas():
        if not SELLER or str(seller_id) == SELLER:
            a, sid, rf = obter_access(sb, seller_id, refresh)
            return a
    raise SystemExit(f"sem token pra conta {SELLER}")


def listar(access):
    return rec.participacoes_ativas(ITEM, SELLER, access)


def url_delete(p):
    """Monta a MESMA URL de DELETE que ap.remover_participacao montaria, marcando se
    está faltando o offer_id que o ML exige (causa de '200 sem remover')."""
    ptipo = (p.get("type") or "").upper()
    if ptipo in ap.TIPOS_SO_TIPO:                       # PRICE_DISCOUNT / LIGHTNING / DOD: só o tipo
        return f"/seller-promotions/items/{ITEM}?promotion_type={ptipo}&app_version=v2"
    qs = f"?promotion_type={ptipo}&promotion_id={p.get('promotion_id')}&app_version=v2"
    if ptipo in ap.TIPOS_COM_OFFER:
        if p.get("offer_id"):
            qs += f"&offer_id={p['offer_id']}"
        else:
            qs += "   <<< SEM offer_id — o ML EXIGE pra este tipo; sem ele o DELETE vira 200 sem remover"
    return f"/seller-promotions/items/{ITEM}{qs}"


def main():
    if not ITEM:
        raise SystemExit("defina ITEM_ID")
    access = token()
    antes = listar(access)
    print(f"ITEM {ITEM} | conta {SELLER} | {len(antes)} participação(ões) ATIVA(S) agora:", flush=True)
    for p in antes:
        print(f"  - {p.get('name')}  | type={p.get('type')}  pid={p.get('promotion_id')}  "
              f"offer_id={p.get('offer_id')}  status={p.get('status')}", flush=True)
        print(f"      DELETE {url_delete(p)}", flush=True)

    if not APLICAR:
        print("\n[DRY] APLICAR=0 — NÃO enviei nenhum DELETE (só mostrei o que enviaria).", flush=True)
        print("Rode de novo com APLICAR=1 pra testar a saída de verdade e ver a resposta do ML.", flush=True)
        return

    print("\n--- ENVIANDO DELETE de verdade (mesma função da produção) ---", flush=True)
    for p in antes:
        sc, body = ap.remover_participacao(ITEM, p, access)
        print(f"  DELETE [{p.get('type')}] {p.get('name')}  ->  HTTP {sc}  | "
              f"{json.dumps(body, ensure_ascii=False)[:240]}", flush=True)
    # catch-all bulk (mesma coisa que a produção faz depois)
    scb, resumob, bodyb = ap.remover_todas(ITEM, access)
    print(f"  BULK remover_todas -> {resumob} | {json.dumps(bodyb, ensure_ascii=False)[:200]}", flush=True)

    depois = listar(access)
    print(f"\nDEPOIS: {len(depois)} ativa(s):", flush=True)
    for p in depois:
        print(f"  - {p.get('name')}  | type={p.get('type')}  pid={p.get('promotion_id')}  "
              f"offer_id={p.get('offer_id')}", flush=True)
    saiu = len(antes) - len(depois)
    print(f"\nRESUMO: de {len(antes)} ativas, saíram {saiu}, sobraram {len(depois)}.", flush=True)
    if depois:
        print("As que SOBRARAM são as que o DELETE não removeu — olhe o HTTP/corpo delas acima "
              "pra ver se o ML recusou, ignorou (200 sem remover) ou pediu autorização.", flush=True)


if __name__ == "__main__":
    main()
