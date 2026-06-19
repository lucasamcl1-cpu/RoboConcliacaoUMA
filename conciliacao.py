#!/usr/bin/env python3
"""
Robô de Conciliação UMA
- Busca lançamentos em lote para evitar rate limit
- Match por data + valor; fallback por data + descrição
- Atualiza valor se divergente, corrige data se já pago com data errada
- Envia resumo detalhado no Telegram
"""

import os
import time
import json
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

# ── configuração ──────────────────────────────────────────────────────────────
TRINKS_TOKEN      = os.environ.get("TRINKS_TOKEN", "")
SHEETS_ID         = "1FenTpiGxXxFmyRAQQcWK-k0eIh8k5Yrw01wjUu0U9rE"
TRINKS_UNIDADE_ID = "79650"
BASE_URL          = "https://api.trinks.com"

GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE = "credentials.json"

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8683343053:AAE6CCxOpsWrUxBoPROVvTlWBPnI3xzvFAM")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-5144739527")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

FORMA_ID = {
    "pix":                  344209,
    "parcelamento próprio": 344210,
    "parcelamento proprio": 344210,
    "cartão de crédito":    1817043,
    "cartao de credito":    1817043,
    "cartão de débito":     1817044,
    "cartao de debito":     1817044,
    "doc/ted":              344207,
    "boleto":               344209,
}

# ── helpers ───────────────────────────────────────────────────────────────────
def parse_data(s):
    s = s.strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_valor(s):
    s = s.strip().replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return abs(float(s))
    except ValueError:
        return None


def hdrs():
    return {
        "X-Api-Key":         TRINKS_TOKEN,
        "estabelecimentoId": TRINKS_UNIDADE_ID,
        "Content-Type":      "application/json",
    }


def get_com_retry(url, params, tentativas=3):
    for i in range(tentativas):
        r = requests.get(url, headers=hdrs(), params=params, timeout=30)
        if r.status_code == 200:
            return r
        if r.status_code == 429:
            espera = 5 * (i + 1)
            print(f"  429 rate limit GET — aguardando {espera}s...")
            time.sleep(espera)
        else:
            return r
    return r


def patch_com_retry(lancamento_id, payload, tentativas=3):
    for i in range(tentativas):
        r = requests.patch(f"{BASE_URL}/v1/lancamentos/{lancamento_id}",
                           headers=hdrs(), json=payload, timeout=30)
        if r.status_code in (200, 204):
            return r.status_code, r.text
        if r.status_code == 429:
            espera = 5 * (i + 1)
            print(f"  429 rate limit PATCH — aguardando {espera}s...")
            time.sleep(espera)
        else:
            return r.status_code, r.text
    return r.status_code, r.text


def buscar_lancamentos_dia(dia):
    todos = []
    page  = 1
    while True:
        r = get_com_retry(f"{BASE_URL}/v1/lancamentos", {
            "dataInicio": f"{dia}T00:00:00",
            "dataFim":    f"{dia}T23:59:59",
            "tipo":       2,
            "page":       page,
            "pageSize":   50,
        })
        if r.status_code != 200:
            break
        data  = r.json()
        itens = data.get("data", [])
        if not itens:
            break
        todos.extend(itens)
        if page >= (data.get("totalPages") or 1):
            break
        page += 1
        time.sleep(0.3)
    return todos


def desc_similar(desc_planilha, desc_trinks):
    """Verifica se as descrições são idênticas (ignorando maiúsculas e espaços extras)."""
    a = desc_planilha.lower().strip()
    b = (desc_trinks or "").lower().strip()
    return a == b


def resolver_forma_id(forma_key, lancamento=None):
    forma_id = FORMA_ID.get(forma_key)
    if not forma_id:
        for k, v in FORMA_ID.items():
            if k in forma_key or forma_key in k:
                forma_id = v
                break
    if not forma_id and lancamento:
        forma_id = lancamento.get("formaPagamentoId")
    return forma_id or 344209


def enviar_telegram(mensagem):
    # Telegram limita mensagens a 4096 chars
    if len(mensagem) > 4000:
        mensagem = mensagem[:4000] + "\n..."
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       mensagem,
            "parse_mode": "HTML",
        }, timeout=10)
        if not r.ok:
            print(f"Telegram erro {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"Telegram exception: {e}")


# ── planilha ──────────────────────────────────────────────────────────────────
def autenticar_sheets():
    if GOOGLE_CREDENTIALS_JSON:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def ler_despesas(aba):
    rows = aba.get("A1:K1000") or []
    despesas = []
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < 11:
            row.append("")
        venc      = row[1].strip()
        pgto      = row[2].strip()
        descricao = row[5].strip()
        forma     = row[9].strip()
        valor_str = row[10].strip()
        if not pgto:
            continue
        venc_iso = parse_data(venc)
        pgto_iso = parse_data(pgto)
        valor    = parse_valor(valor_str)
        if not venc_iso or not pgto_iso or valor is None:
            continue
        despesas.append({
            "linha": i, "venc_iso": venc_iso, "pgto_iso": pgto_iso,
            "descricao": descricao, "forma": forma, "valor": valor,
        })
    return despesas


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Robô Conciliação UMA — {datetime.now().strftime('%d/%m/%Y %H:%M')} ===\n")

    client   = autenticar_sheets()
    aba      = client.open_by_key(SHEETS_ID).worksheets()[0]
    despesas = ler_despesas(aba)
    print(f"Despesas com data de pagamento: {len(despesas)}")

    if not despesas:
        msg = f"<b>📋 Conciliação UMA — {datetime.now().strftime('%d/%m/%Y')}</b>\n\nNenhuma despesa para processar."
        print(msg)
        enviar_telegram(msg)
        return

    # ── busca em lote por data ────────────────────────────────────────────────
    datas_buscar = set()
    for d in despesas:
        base = datetime.strptime(d["venc_iso"], "%Y-%m-%d")
        for delta in range(-3, 4):
            datas_buscar.add((base + timedelta(days=delta)).strftime("%Y-%m-%d"))

    print(f"Buscando lançamentos para {len(datas_buscar)} datas no Trinks...")
    todos_lancamentos = {}
    for dia in sorted(datas_buscar):
        for l in buscar_lancamentos_dia(dia):
            todos_lancamentos[l["id"]] = l
        time.sleep(0.4)
    print(f"Total lançamentos carregados: {len(todos_lancamentos)}")

    # ── resultados ────────────────────────────────────────────────────────────
    atualizados      = []  # novos: não pago → pago
    corrigidos_data  = []  # já pago mas data errada → corrigida
    corrigidos_valor = []  # valor diferente → atualizado
    ja_ok            = []  # já pago, data correta, tudo certo
    nao_encontrados  = []
    erros            = []

    ids_ja_processados = set()

    for item in despesas:
        venc_iso  = item["venc_iso"]
        pgto_iso  = item["pgto_iso"]
        valor     = item["valor"]
        forma_key = item["forma"].lower().strip()
        desc      = item["descricao"]

        forma_id = resolver_forma_id(forma_key)

        base = datetime.strptime(venc_iso, "%Y-%m-%d")
        dias_janela = {(base + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(-3, 4)}

        lancamentos_janela = [
            l for l in todos_lancamentos.values()
            if l.get("dataVencimento", "")[:10] in dias_janela
            and l["id"] not in ids_ja_processados
        ]

        # 1. tenta match exato por valor
        candidatos = [l for l in lancamentos_janela if abs(l.get("valor", 0) - valor) < 0.05]

        # 2. fallback: match por descrição (aceita valor diferente)
        candidato_desc = None
        if not candidatos:
            por_desc = [l for l in lancamentos_janela if desc_similar(desc, l.get("descricao", ""))]
            if por_desc:
                candidato_desc = por_desc[0]

        # ── não encontrado ────────────────────────────────────────────────────
        if not candidatos and not candidato_desc:
            nao_encontrados.append(f"venc={venc_iso} | R${valor:.2f} | {desc}")
            continue

        # ── match por descrição (valor diferente) ─────────────────────────────
        if not candidatos and candidato_desc:
            l = candidato_desc
            valor_trinks = l.get("valor", 0)
            fid = resolver_forma_id(forma_key, l)
            payload = {
                "statusPagamento":  1,
                "dataPagamento":    f"{pgto_iso}T12:00:00",
                "formaPagamentoId": fid,
                "valor":            valor,
            }
            sc, rt = patch_com_retry(l["id"], payload)
            time.sleep(0.5)
            ids_ja_processados.add(l["id"])
            if sc in (200, 204):
                todos_lancamentos[l["id"]]["statusPagamento"] = 1
                msg = f"ID {l['id']} | {desc} | R${valor_trinks:.2f} → R${valor:.2f} | Pago em {pgto_iso}"
                corrigidos_valor.append(msg)
                print(f"  ✏️  valor corrigido: {msg}")
            else:
                erros.append(f"ID {l['id']} | {desc} | {sc}: {rt[:80]}")
            continue

        # ── match por valor ───────────────────────────────────────────────────
        nao_pagos_list = [l for l in candidatos if l.get("statusPagamento") == 2]
        pagos_list     = [l for l in candidatos if l.get("statusPagamento") == 1]

        if nao_pagos_list:
            l   = nao_pagos_list[0]
            fid = resolver_forma_id(forma_key, l)
            sc, rt = patch_com_retry(l["id"], {
                "statusPagamento":  1,
                "dataPagamento":    f"{pgto_iso}T12:00:00",
                "formaPagamentoId": fid,
                "valor":            valor,
            })
            time.sleep(0.5)
            ids_ja_processados.add(l["id"])
            if sc in (200, 204):
                todos_lancamentos[l["id"]]["statusPagamento"] = 1
                atualizados.append(f"ID {l['id']} | {desc} | R${valor:.2f} | Pago em {pgto_iso}")
            else:
                erros.append(f"ID {l['id']} | {desc} | {sc}: {rt[:80]}")

        elif pagos_list:
            l = pagos_list[0]
            ids_ja_processados.add(l["id"])
            data_trinks = (l.get("dataPagamento") or "")[:10]

            if data_trinks != pgto_iso:
                # já pago mas data diferente — corrige
                fid = resolver_forma_id(forma_key, l)
                sc, rt = patch_com_retry(l["id"], {
                    "statusPagamento":  1,
                    "dataPagamento":    f"{pgto_iso}T12:00:00",
                    "formaPagamentoId": fid,
                    "valor":            valor,
                })
                time.sleep(0.5)
                if sc in (200, 204):
                    msg = f"ID {l['id']} | {desc} | R${valor:.2f} | data {data_trinks} → {pgto_iso}"
                    corrigidos_data.append(msg)
                    print(f"  📅  data corrigida: {msg}")
                else:
                    erros.append(f"ID {l['id']} | {desc} | {sc}: {rt[:80]}")
            else:
                ja_ok.append(f"ID {l['id']} | R${valor:.2f} | {desc}")

    # ── log ───────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"✅ ATUALIZADOS ({len(atualizados)}):")
    for x in atualizados: print(f"  {x}")
    print(f"\n✏️  VALOR CORRIGIDO ({len(corrigidos_valor)}):")
    for x in corrigidos_valor: print(f"  {x}")
    print(f"\n📅  DATA CORRIGIDA ({len(corrigidos_data)}):")
    for x in corrigidos_data: print(f"  {x}")
    print(f"\n⏭  JÁ OK ({len(ja_ok)}):")
    for x in ja_ok: print(f"  {x}")
    print(f"\n🔍 NÃO ENCONTRADOS ({len(nao_encontrados)}):")
    for x in nao_encontrados: print(f"  {x}")
    if erros:
        print(f"\n❌ ERROS ({len(erros)}):")
        for x in erros: print(f"  {x}")
    print("\nConcluído.")

    # ── Telegram ──────────────────────────────────────────────────────────────
    hoje = datetime.now().strftime("%d/%m/%Y")
    linhas = [f"<b>📋 Conciliação UMA — {hoje}</b>\n"]

    if atualizados:
        linhas.append(f"<b>✅ {len(atualizados)} marcada(s) como Pago:</b>")
        for x in atualizados:
            linhas.append(f"• {x}")

    if corrigidos_valor:
        linhas.append(f"\n<b>✏️ {len(corrigidos_valor)} valor(es) corrigido(s):</b>")
        for x in corrigidos_valor:
            linhas.append(f"• {x}")

    if corrigidos_data:
        linhas.append(f"\n<b>📅 {len(corrigidos_data)} data(s) corrigida(s):</b>")
        for x in corrigidos_data:
            linhas.append(f"• {x}")

    if not atualizados and not corrigidos_valor and not corrigidos_data:
        linhas.append("Nenhuma despesa nova para atualizar.")

    if ja_ok:
        linhas.append(f"\n⏭ {len(ja_ok)} já estavam corretas")

    if nao_encontrados:
        linhas.append(f"\n<b>🔍 {len(nao_encontrados)} não encontrada(s) no Trinks:</b>")
        for x in nao_encontrados:
            linhas.append(f"• {x}")

    if erros:
        linhas.append(f"\n<b>❌ {len(erros)} erro(s):</b>")
        for x in erros:
            linhas.append(f"• {x}")

    enviar_telegram("\n".join(linhas))


if __name__ == "__main__":
    main()
