"""
Autenticacao compartilhada Mercado Livre (usada pelo puxador de VENDAS e pelo
de DEVOLUCOES). Guarda o access_token na tabela 'contas' e so renova quando
esta perto de expirar. Assim os dois puxadores nao ficam rotacionando o
refresh_token a cada rodada (o que causava colisao / invalid_grant).

Uso nos dois scripts:
    from ml_auth import obter_access
    access, sid, refresh = obter_access(sb, seller_id, refresh)

Pre-requisito: rodar 'contas_token_compartilhado.sql' no Supabase uma vez
(adiciona as colunas access_token e access_expira_em na tabela 'contas').
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.mercadolibre.com"
CLIENT_ID     = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]


def renovar_token(refresh_token):
    r = requests.post(API + "/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }, timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError("Falha ao renovar token: " + str(d))
    return d


def _valido(exp_str, margem_seg):
    if not exp_str:
        return False
    try:
        dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)) > timedelta(seconds=margem_seg)
    except Exception:
        return False


def obter_access(sb, seller_id, refresh_token, margem_seg=900):
    """Devolve (access_token, seller_id, refresh_token_atual).
    Reaproveita o access_token guardado na 'contas'; so renova se faltar
    menos que 'margem_seg' segundos (padrao 15 min) pra expirar."""
    # 1) tenta reaproveitar o token guardado
    if seller_id:
        try:
            rows = (sb.table("contas")
                    .select("access_token, access_expira_em, refresh_token")
                    .eq("seller_id", str(seller_id)).limit(1).execute().data) or []
        except Exception:
            rows = []
        if rows:
            r = rows[0]
            rt = r.get("refresh_token") or refresh_token
            if r.get("access_token") and _valido(r.get("access_expira_em"), margem_seg):
                return r["access_token"], str(seller_id), rt
            refresh_token = rt   # usa sempre o refresh mais recente

    # 2) precisa renovar
    d = renovar_token(refresh_token)
    access = d["access_token"]
    novo_refresh = d.get("refresh_token", refresh_token)
    sid = str(d.get("user_id") or seller_id)
    expira = datetime.now(timezone.utc) + timedelta(seconds=int(d.get("expires_in", 21600)) - 60)
    sb.table("contas").upsert({
        "seller_id": sid,
        "refresh_token": novo_refresh,
        "access_token": access,
        "access_expira_em": expira.isoformat(),
    }, on_conflict="seller_id").execute()
    return access, sid, novo_refresh
