from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
import os
import base64
import urllib.request
import xml.etree.ElementTree as ET
import json

app = FastAPI(
    title="Robo2 Signals API",
    description="API de sinais e manchetes estruturadas em tempo real para agentes de IA (Tecnologia, Economia e Cripto) com pagamento por requisição via x402 em USDC (Solana).",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"]
)

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS", "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk")
PRECO_USDC = float(os.getenv("PRECO_USDC", "0.01"))
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
REDE_PAGAMENTO = "solana"
MOEDA_PAGAMENTO = "USDC"

TARGETS = [
    {
        "id": "tech_theverge",
        "categoria": "tecnologia",
        "fonte": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml"
    },
    {
        "id": "tech_techcrunch",
        "categoria": "tecnologia",
        "fonte": "TechCrunch",
        "url": "https://techcrunch.com/feed/"
    },
    {
        "id": "econ_cnbc",
        "categoria": "economia",
        "fonte": "CNBC Top News",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"
    },
    {
        "id": "crypto_btc_usd",
        "categoria": "economia",
        "fonte": "Coinbase Spot BTC-USD",
        "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    }
]


def validar_pagamento_solana_usdc(assinatura_tx: str) -> tuple[bool, str]:
    assinatura = assinatura_tx.strip()
    if assinatura.startswith("{"):
        try:
            payload = json.loads(assinatura)
            assinatura = (
                payload.get("signature")
                or payload.get("txHash")
                or payload.get("payload", {}).get("signature")
                or ""
            ).strip()
        except Exception:
            return False, "Formato de comprovante JSON inválido."

    if len(assinatura) < 40:
        return False, "Assinatura de transação Solana inválida ou curta demais."

    rpc_body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            assinatura,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}
        ]
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            SOLANA_RPC_URL,
            data=rpc_body,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resultado = json.loads(resp.read().decode("utf-8")).get("result")

        if not resultado:
            return False, "Transação não encontrada ou ainda não confirmada na Solana."

        meta = resultado.get("meta") or {}
        if meta.get("err") is not None:
            return False, "Transação falhou na blockchain Solana."

        pre_balances = {
            b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in meta.get("preTokenBalances", [])
            if b.get("mint") == USDC_MINT_SOLANA and b.get("owner") == PAY_TO_ADDRESS
        }
        post_balances = {
            b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in meta.get("postTokenBalances", [])
            if b.get("mint") == USDC_MINT_SOLANA and b.get("owner") == PAY_TO_ADDRESS
        }

        recebido = sum(
            post_balances.get(idx, 0.0) - pre_balances.get(idx, 0.0)
            for idx in post_balances
        )

        if recebido + 1e-9 >= PRECO_USDC:
            return True, f"Confirmado {recebido:.6f} USDC na Solana (tx: {assinatura})"

        return False, f"Valor recebido insuficiente ({recebido:.6f} USDC; mínimo {PRECO_USDC} USDC)."
    except Exception as e:
        return False, f"Erro ao consultar rede Solana: {str(e)}"


def extrair_itens_feed(url: str, limite: int = 5):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.0)",
            "Accept": "*/*"
        }
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    if raw.lstrip().startswith("{"):
        data = json.loads(raw).get("data", {})
        return [{
            "titulo": f"{data.get('base', 'BTC')}-{data.get('currency', 'USD')}: {data.get('amount')}",
            "link": url,
            "tipo": "cotacao"
        }]

    root = ET.fromstring(raw)
    itens = []

    for item in root.findall(".//item")[:limite]:
        titulo = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if titulo:
            itens.append({
                "titulo": titulo,
                "link": link,
                "publicado_em": pub_date,
                "tipo": "manchete"
            })

    if not itens:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:limite]:
            titulo = (entry.findtext("atom:title", default="", namespaces=ns)).strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            updated = (entry.findtext("atom:updated", default="", namespaces=ns)).strip()
            if titulo:
                itens.append({
                    "titulo": titulo,
                    "link": link,
                    "publicado_em": updated,
                    "tipo": "manchete"
                })

    return itens


def montar_manifesto_x402(resource_url: str):
    return {
        "x402Version": 1,
        "error": "Payment Required",
        "accepts": [
            {
                "scheme": "exact",
                "network": REDE_PAGAMENTO,
                "asset": USDC_MINT_SOLANA,
                "currency": MOEDA_PAGAMENTO,
                "payTo": PAY_TO_ADDRESS,
                "maxAmountRequired": str(int(round(PRECO_USDC * 1_000_000))),
                "amountUSDC": f"{PRECO_USDC:.2f}",
                "resource": resource_url,
                "description": "Sinais estruturados em tempo real de Tecnologia, Economia e cotação BTC-USD para agentes de IA.",
                "mimeType": "application/json"
            }
        ]
    }


@app.get("/")
def root():
    return {
        "status": "online",
        "servico": "Robo2 Signals API",
        "versao": "2.0.0",
        "protocolo_pagamento": "x402",
        "moeda": MOEDA_PAGAMENTO,
        "rede": REDE_PAGAMENTO,
        "recebedor": PAY_TO_ADDRESS,
        "preco_por_requisicao_usdc": f"{PRECO_USDC:.2f}",
        "rotas": {
            "status_gratuito": "/radar",
            "dados_pagos_x402": "/radar/itens",
            "manifesto_x402": "/.well-known/x402",
            "especificacao_api": "/openapi.json"
        }
    }


@app.get("/.well-known/x402", summary="Manifesto de descoberta x402 para agentes e catálogos")
def well_known_x402(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "version": 1,
        "provider": "Robo2 Signals API",
        "network": REDE_PAGAMENTO,
        "currency": MOEDA_PAGAMENTO,
        "payTo": PAY_TO_ADDRESS,
        "endpoints": [
            {
                "path": "/radar/itens",
                "method": "GET",
                "priceUSDC": f"{PRECO_USDC:.2f}",
                "description": "Manchetes de Tecnologia (The Verge, TechCrunch), Economia (CNBC) e cotação BTC-USD em tempo real."
            }
        ],
        "openapi": f"{base}/openapi.json"
    }


@app.get("/radar")
def verificar_radar():
    resultados = []
    for alvo in TARGETS:
        try:
            req = urllib.request.Request(
                alvo["url"],
                headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.0)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                conteudo = resp.read()
                resultados.append({
                    "id": alvo["id"],
                    "categoria": alvo["categoria"],
                    "status": "online",
                    "bytes": len(conteudo)
                })
        except Exception as e:
            resultados.append({
                "id": alvo["id"],
                "categoria": alvo["categoria"],
                "status": "erro",
                "detalhe": str(e)
            })
    return {
        "registrado_em": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "resultados": resultados
    }


@app.get("/radar/itens", summary="Retorna sinais estruturados mediante pagamento x402 em USDC (Solana)")
def listar_itens_radar(
    request: Request,
    categoria: str | None = Query(default=None, description="Filtrar por 'tecnologia' ou 'economia'"),
    limite_por_fonte: int = Query(default=5, ge=1, le=20, description="Quantidade de itens por fonte")
):
    comprovante_pagamento = (
        request.headers.get("X-PAYMENT")
        or request.headers.get("PAYMENT-SIGNATURE")
        or request.headers.get("X-Payment-Proof")
    )

    if not comprovante_pagamento:
        manifesto = montar_manifesto_x402(str(request.url))
        header_b64 = base64.b64encode(json.dumps(manifesto).encode("utf-8")).decode("utf-8")
        return JSONResponse(
            status_code=402,
            content=manifesto,
            headers={"PAYMENT-REQUIRED": header_b64}
        )

    pago_ok, detalhe_tx = validar_pagamento_solana_usdc(comprovante_pagamento)
    if not pago_ok:
        manifesto = montar_manifesto_x402(str(request.url))
        manifesto["motivo_recusa"] = detalhe_tx
        return JSONResponse(status_code=402, content=manifesto)

    alvos_filtrados = [
        t for t in TARGETS
        if not categoria or t["categoria"].lower() == categoria.lower()
    ]

    coleta = []
    total_geral = 0

    for alvo in alvos_filtrados:
        try:
            itens = extrair_itens_feed(alvo["url"], limite=limite_por_fonte)
            total_geral += len(itens)
            coleta.append({
                "id": alvo["id"],
                "fonte": alvo["fonte"],
                "categoria": alvo["categoria"],
                "status": "online",
                "total_itens": len(itens),
                "itens": itens
            })
        except Exception as e:
            coleta.append({
                "id": alvo["id"],
                "fonte": alvo["fonte"],
                "categoria": alvo["categoria"],
                "status": "erro",
                "detalhe": str(e),
                "total_itens": 0,
                "itens": []
            })

    coleta.sort(key=lambda x: x.get("total_itens", 0), reverse=True)

    return JSONResponse(
        status_code=200,
        headers={"X-PAYMENT-RESPONSE": detalhe_tx},
        content={
            "registrado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pagamento_validado": detalhe_tx,
            "filtro_categoria": categoria or "todas",
            "total_itens_retornados": total_geral,
            "fontes": coleta
        }
    )
