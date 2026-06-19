#!/usr/bin/env python3
"""
Robô de Conciliação UMA
Lê a planilha de despesas, compara data de vencimento + valor com o Trinks
e atualiza as despesas Não Pagas para Pago com a data de pagamento da planilha.

Estratégia anti-rate-limit: busca TODOS os lançamentos das datas necessárias
de uma vez só no início, depois faz o match em memória — sem chamadas por item.
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
TRINKS_UNIDADE_ID = "79650"  # Centro
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
    "boleto":               344209,  # Boleto não cadastrado no Trinks — usa PIX
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
    """GET com retry automático em caso de 429."""
    for i in range(tentativas):
        r = requests.get(url, headers=hdrs(), params=params, timeout=30)
        if r.status_code == 200:
            return r
        if r.status_code == 429:
            espera = 5 * (i + 1)
            print(f"  429 rate limit — aguardando {espera}s...")
            time.sleep(espera)
        else:
            return r
    return r


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


def patch_com_retry(lancamento_id, data_pgto, forma_id, valor, tentativas=3):
    """PATCH com retry automático em caso de 429."""
    payload = {
        "statusPagamento":  1,
        "dataPagamento":    f"{data_pgto}T12:00:00",
        "formaPagamentoId": forma_id,
        "valor":            valor,
    }
    for i in range(tentativas):
        r = requests.patch(f"{BASE_URL}/v1/lancamentos/{lancamento_id}",
                           headers=hdrs(), json=payload, timeout=30)
        if r.status_code in (200, 204):
            return r.status_code, r.text
        if r.status_code == 429:
            espera = 5 * (i + 1)
            print(f"  429 rate limit no PATCH — aguardando {espera}s...")
            time.sleep(espera)
        else:
            return r.status_code, r.text
    return r.status_code, r.text


def enviar_telegram(mensagem):
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
            continue  # sem data de pagamento = ainda não pago

        venc_iso = parse_data(venc)
        pgto_iso = parse_data(pgto)
        valor    = parse_valor(valor_str)

        if not venc_iso or not pgto_iso or valor is None:
            continue

        despesas.append({
            "linha":     i,
            "venc_iso":  venc_iso,
            "pgto_iso":  pgto_iso,
            "descricao": descricao,
            "forma":     forma,
            "valor":     valor,
        })
    return despesas


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Robô Conciliação UMA — {datetime.now().strftime('%d/%m/%Y %H:%M')} ===\n")

    client   = autenticar_sheets()
    aba      = client.open_by_key(SHEETS_ID).worksheets()[0]
    despesas = ler_despesas(aba)
    print(f"Despesas com data de pagamento na planilha: {len(despesas)}")

    if not despesas:
        print("Nada a processar.")
        enviar_telegram(f"<b>📋 Conciliação UMA — {datetime.now().strftime('%d/%m/%Y')}</b>\n\nNenhuma despesa para processar.")
        return

    # ── busca em lote: 1 chamada por dia único (sem duplicar por item) ─────────
    datas_unicas = sorted({d["venc_iso"] for d in despesas})
    # inclui ±3 dias de margem para cada data
    datas_buscar = set()
    for iso in datas_unicas:
        base = datetime.strptime(iso, "%Y-%m-%d")
        for delta in range(-3, 4):
            datas_buscar.add((base + timedelta(days=delta)).strftime("%Y-%m-%d"))

    print(f"Buscando lançamentos para {len(datas_buscar)} datas no Trinks...")
    cache = {}  # dia → lista de lancamentos
    for dia in sorted(datas_buscar):
        cache[dia] = buscar_lancamentos_dia(dia)
        time.sleep(0.4)

    # índice por valor para match rápido
    # lancamentos_por_valor: valor_arredondado → [lancamento, ...]
    todos_lancamentos = {}
    for lancamentos in cache.values():
        for l in lancamentos:
            todos_lancamentos[l["id"]] = l

    print(f"Total lançamentos carregados: {len(todos_lancamentos)}")

    # ── match e atualização ───────────────────────────────────────────────────
    atualizados     = []
    ja_pagos        = []
    nao_encontrados = []
    erros           = []

    for item in despesas:
        venc_iso  = item["venc_iso"]
        pgto_iso  = item["pgto_iso"]
        valor     = item["valor"]
        forma_key = item["forma"].lower().strip()
        desc      = item["descricao"]

        # resolve formaPagamentoId
        forma_id = FORMA_ID.get(forma_key)
        if not forma_id:
            for k, v in FORMA_ID.items():
                if k in forma_key or forma_key in k:
                    forma_id = v
                    break
        if not forma_id:
            forma_id = 344209  # PIX fallback

        # busca em memória: mesmo vencimento ± 3 dias e valor igual
        base = datetime.strptime(venc_iso, "%Y-%m-%d")
        dias_janela = {(base + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(-3, 4)}

        candidatos = [
            l for l in todos_lancamentos.values()
            if l.get("dataVencimento", "")[:10] in dias_janela
            and abs(l.get("valor", 0) - valor) < 0.05
        ]

        if not candidatos:
            nao_encontrados.append(f"venc={venc_iso} | R${valor:.2f} | {desc}")
            continue

        nao_pagos_list = [l for l in candidatos if l.get("statusPagamento") == 2]

        if not nao_pagos_list:
            for l in candidatos:
                ja_pagos.append(f"ID {l['id']} | R${valor:.2f} | {desc}")
            continue

        lancamento = nao_pagos_list[0]
        status_code, resp_text = patch_com_retry(lancamento["id"], pgto_iso, forma_id, valor)
        time.sleep(0.5)

        if status_code in (200, 204):
            # atualiza cache local para não processar duas vezes
            todos_lancamentos[lancamento["id"]]["statusPagamento"] = 1
            atualizados.append(f"ID {lancamento['id']} | {desc} | R${valor:.2f} | Pago em {pgto_iso}")
        else:
            erros.append(f"ID {lancamento['id']} | {desc} | {status_code}: {resp_text[:80]}")

    # ── log ───────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"✅ ATUALIZADOS ({len(atualizados)}):")
    for x in atualizados:
        print(f"  {x}")
    print(f"\n⏭  JÁ ESTAVAM PAGOS ({len(ja_pagos)}):")
    for x in ja_pagos:
        print(f"  {x}")
    print(f"\n🔍 NÃO ENCONTRADOS ({len(nao_encontrados)}):")
    for x in nao_encontrados:
        print(f"  {x}")
    if erros:
        print(f"\n❌ ERROS ({len(erros)}):")
        for x in erros:
            print(f"  {x}")
    print("\nConcluído.")

    # ── Telegram ──────────────────────────────────────────────────────────────
    hoje = datetime.now().strftime("%d/%m/%Y")
    if atualizados:
        detalhes = "\n".join(f"• {x}" for x in atualizados)
        msg = (
            f"<b>✅ Conciliação UMA — {hoje}</b>\n\n"
            f"<b>{len(atualizados)} despesa(s) marcadas como Pago:</b>\n"
            f"{detalhes}"
        )
        if nao_encontrados:
            msg += f"\n\n🔍 {len(nao_encontrados)} não encontrada(s) no Trinks"
        if erros:
            msg += f"\n❌ {len(erros)} erro(s)"
    else:
        msg = (
            f"<b>📋 Conciliação UMA — {hoje}</b>\n\n"
            f"Nenhuma despesa nova para atualizar.\n"
            f"Já pagas: {len(ja_pagos)} | Não encontradas: {len(nao_encontrados)}"
        )
        if erros:
            msg += f"\n❌ {len(erros)} erro(s)"

    enviar_telegram(msg)


if __name__ == "__main__":
    main()
