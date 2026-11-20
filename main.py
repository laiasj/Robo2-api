import os
import time
import asyncio
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# 1. CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE (RENDER)
# =====================================================================
RECEIVER_WALLET = os.getenv("RECEIVER_WALLET", "0xSuaCarteiraAqui").lower()
RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")
COLLECT_INTERVAL_SECONDS = int(os.getenv("COLLECT_INTERVAL_SECONDS", "30"))
TRACKED_ASSETS = os.getenv("TRACKED_ASSETS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")

# Tokens com liquidez real aceitos no pagamento M2M (Rede Base Mainnet)
ACCEPTED_TOKENS = {
    "USDC": {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower(),
        "decimals": 6,
        "min_amount": float(os.getenv("PRICE_USDC", "0.01"))
    },
    "LINK": {
        "address": "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196".lower(),
        "decimals": 18,
        "min_amount": float(os.getenv("PRICE_LINK", "0.0005"))
    }
}

# =====================================================================
# 2. ESTADO EM MEMÓRIA DO ROBO2 & CONEXÃO BLOCKCHAIN
# =====================================================================
w3 = Web3(Web3.HTTPProvider(RPC_URL))
TRANSFER_TOPIC = w3.keccak(text="Transfer(address,address,uint256)").hex().lower()
USED_TX_HASHES = set()

ROBO2_STATE = {
    "status": "starting",
    "last_update": 0,
    "total_paid_requests": 0,
    "revenue_by_token": {"USDC": 0.0, "LINK": 0.0},
    "signals": {},
    "recent_settlements": []
}


# =====================================================================
# 3. MOTOR DE COLETA EM SEGUNDO PLANO (HOOK / SCHEDULER DO ROBO2)
# =====================================================================
async def fetch_market_snapshot(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Coleta dados brutos e calcula spread/preço médio padronizado."""
    clean_symbol = symbol.strip().upper()
    url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={clean_symbol}"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code != 200:
            return None
        raw = resp.json()
        bid = float(raw["bidPrice"])
        ask = float(raw["askPrice"])
        mid = round((bid + ask) / 2, 6)
        spread_pct = round(((ask - bid) / bid) * 100, 5) if bid > 0 else 0.0

        return {
            "provider": "Robo2-M2M",
            "asset": clean_symbol,
            "value": mid,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "timestamp": int(time.time()),
            "status": "verified"
        }
    except Exception:
        return None


async def background_collector_loop():
    """Mantém o cache do Robo2 sempre quente para entrega imediata aos agentes."""
    async with httpx.AsyncClient() as client:
        while True:
            for asset in TRACKED_ASSETS:
                signal = await fetch_market_snapshot(client, asset)
                if signal:
                    ROBO2_STATE["signals"][signal["asset"]] = signal
            ROBO2_STATE["status"] = "online"
            ROBO2_STATE["last_update"] = int(time.time())
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_collector_loop())
    yield
    task.cancel()


app = FastAPI(title="Robo2 M2M Oracle & Payment Gateway", version="2.0.0", lifespan=lifespan)


# =====================================================================
# 4. VALIDAÇÃO DE PAGAMENTO ON-CHAIN (USDC x402 + TOKEN LÍQUIDO)
# =====================================================================
def verify_onchain_payment(tx_hash: str) -> dict:
    if not tx_hash:
        return {"valid": False, "reason": "Header de pagamento ausente"}

    tx_clean = tx_hash.strip().lower()
    if tx_clean in USED_TX_HASHES:
        return {"valid": False, "reason": "Hash de transação já utilizado"}

    try:
        receipt = w3.eth.get_transaction_receipt(tx_clean)
        if not receipt or receipt.status != 1:
            return {"valid": False, "reason": "Transação não confirmada ou falhou na rede"}

        for log in receipt.logs:
            topics = [t.hex().lower() for t in log.topics]
            if len(topics) >= 3 and topics[0] == TRANSFER_TOPIC:
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() == RECEIVER_WALLET:
                    contract_addr = log.address.lower()
                    for symbol, info in ACCEPTED_TOKENS.items():
                        if contract_addr == info["address"]:
                            raw_val = int(log.data.hex(), 16)
                            amount = raw_val / (10 ** info["decimals"])
                            if amount >= info["min_amount"]:
                                USED_TX_HASHES.add(tx_clean)
                                ROBO2_STATE["total_paid_requests"] += 1
                                ROBO2_STATE["revenue_by_token"][symbol] = round(
                                    ROBO2_STATE["revenue_by_token"].get(symbol, 0.0) + amount, 6
                                )
                                ROBO2_STATE["recent_settlements"].insert(0, {
                                    "token": symbol,
                                    "amount": amount,
                                    "tx": tx_clean,
                                    "time": int(time.time())
                                })
                                ROBO2_STATE["recent_settlements"] = ROBO2_STATE["recent_settlements"][:20]
                                return {"valid": True, "token": symbol, "amount": amount, "tx": tx_clean}

        return {"valid": False, "reason": "Valor abaixo do mínimo ou token não aceito"}
    except Exception as e:
        return {"valid": False, "reason": f"Erro na verificação RPC: {str(e)}"}


# =====================================================================
# 5. PAINEL DE CONTROLE WEB & DESCOBERTA DE PROTOCOLO
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    signals_rows = "".join(
        f"<tr><td><b>{k}</b></td><td>{v['value']}</td><td>{v['bid']}</td><td>{v['ask']}</td><td>{v['spread_pct']}%</td><td>{v['timestamp']}</td></tr>"
        for k, v in ROBO2_STATE["signals"].items()
    ) or "<tr><td colspan='6'>Coletando primeiros sinais...</td></tr>"

    settlements_rows = "".join(
        f"<tr><td>{s['token']}</td><td>{s['amount']}</td><td>{s['tx'][:18]}...</td><td>{s['time']}</td></tr>"
        for s in ROBO2_STATE["recent_settlements"]
    ) or "<tr><td colspan='4'>Nenhum pagamento M2M registrado ainda.</td></tr>"

    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="refresh" content="15">
        <title>Robo2 M2M Gateway</title>
        <style>
            body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 2rem; }}
            .card {{ background: #1e293b; padding: 1.5rem; border-radius: 10px; margin-bottom: 1.5rem; }}
            h1, h2 {{ margin-top: 0; color: #38bdf8; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; }}
            th, td {{ text-align: left; padding: 0.6rem; border-bottom: 1px solid #334155; }}
            .badge {{ background: #10b981; color: #052e16; padding: 0.25rem 0.6rem; border-radius: 6px; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Robo2 — Painel M2M & Oráculo <span class="badge">{ROBO2_STATE['status'].upper()}</span></h1>
            <p><b>Carteira Recebedora:</b> {RECEIVER_WALLET}</p>
            <p><b>Requisições Pagas:</b> {ROBO2_STATE['total_paid_requests']} |
               <b>Receita USDC:</b> {ROBO2_STATE['revenue_by_token']['USDC']} |
               <b>Receita LINK:</b> {ROBO2_STATE['revenue_by_token']['LINK']}</p>
            <p><b>Endpoint M2M:</b> <code>GET /api/v1/m2m/feed/{{ASSET}}</code> (Header: <code>X-Payment-Tx</code>)</p>
        </div>

        <div class="card">
            <h2>Sinais Ativos em Cache</h2>
            <table>
                <thead><tr><th>Ativo</th><th>Preço Médio</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Timestamp</th></tr></thead>
                <tbody>{signals_rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>Últimas Liquidações M2M</h2>
            <table>
                <thead><tr><th>Token</th><th>Valor</th><th>Tx Hash</th><th>Timestamp</th></tr></thead>
                <tbody>{settlements_rows}</tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    return {
        "status": ROBO2_STATE["status"],
        "last_update": ROBO2_STATE["last_update"],
        "assets_cached": list(ROBO2_STATE["signals"].keys())
    }


@app.get("/.well-known/x402.json")
async def x402_discovery():
    """Manifesto legível por máquinas para descoberta automática de preço e tokens."""
    return {
        "protocol": "x402",
        "version": "1.0",
        "provider": "Robo2-M2M",
        "network": "base-mainnet",
        "receiver": RECEIVER_WALLET,
        "endpoint_template": "/api/v1/m2m/feed/{asset}",
        "payment_header": "X-Payment-Tx",
        "accepted_tokens": ACCEPTED_TOKENS
    }


# =====================================================================
# 6. ROTA PRINCIPAL M2M (PORTA DUPLA: USDC x402 + TOKEN NATIVO LÍQUIDO)
# =====================================================================
@app.get("/api/v1/m2m/feed/{asset}")
async def get_m2m_feed(
    asset: str,
    x_payment_tx: str | None = Header(default=None, alias="X-Payment-Tx"),
    x_payment: str | None = Header(default=None, alias="X-PAYMENT")
):
    proof = x_payment_tx or x_payment

    # Sem recibo: devolve HTTP 402 com contrato exato de USDC e LINK para pagamento automático
    if not proof:
        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment Required",
                "protocol": "x402",
                "network": "base-mainnet",
                "receiver": RECEIVER_WALLET,
                "accepted_payments": [
                    {
                        "token": "USDC",
                        "contract": ACCEPTED_TOKENS["USDC"]["address"],
                        "min_amount": ACCEPTED_TOKENS["USDC"]["min_amount"]
                    },
                    {
                        "token": "LINK",
                        "contract": ACCEPTED_TOKENS["LINK"]["address"],
                        "min_amount": ACCEPTED_TOKENS["LINK"]["min_amount"]
                    }
                ],
                "instructions": "Transfira USDC ou LINK para o receiver e reenvie com o header X-Payment-Tx: <tx_hash>"
            }
        )

    # Valida a transação direto na blockchain via RPC
    payment = verify_onchain_payment(proof)
    if not payment["valid"]:
        return JSONResponse(
            status_code=402,
            content={"error": "Payment Rejected", "detail": payment["reason"]}
        )

    clean_asset = asset.strip().upper()
    signal = ROBO2_STATE["signals"].get(clean_asset)

    # Se o ativo pedido não estiver no cache periódico, busca sob demanda na hora
    if not signal:
        async with httpx.AsyncClient() as client:
            signal = await fetch_market_snapshot(client, clean_asset)

    if not signal:
        return JSONResponse(status_code=404, content={"error": f"Ativo {clean_asset} indisponível"})

    return JSONResponse(
        status_code=200,
        content={
            **signal,
            "settlement": {
                "paid_with": payment["token"],
                "amount": payment["amount"],
                "tx_hash": payment["tx"]
            }
        }
    )

# =====================================================================
# BLOCO ISOLADO: RECEBIMENTO EM TOKENS LÍQUIDOS (USDC + LINK)
# Cole APENAS este trecho no final do seu main.py original.
# Não altera o seu painel (/), nem /radar/itens, nem o seu hook.
# =====================================================================
import os
import time
from fastapi import Header
from fastapi.responses import JSONResponse
from web3 import Web3

RECEIVER_WALLET_EVM = os.getenv("RECEIVER_WALLET", "0xSuaCarteiraAqui").lower()
RPC_URL_BASE = os.getenv("RPC_URL", "https://mainnet.base.org")

TOKENS_ACEITOS_M2M = {
    "USDC": {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower(),
        "decimals": 6,
        "min_amount": float(os.getenv("PRICE_USDC", "0.01"))
    },
    "LINK": {
        "address": "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196".lower(),
        "decimals": 18,
        "min_amount": float(os.getenv("PRICE_LINK", "0.0005"))
    }
}

_w3_oracle = Web3(Web3.HTTPProvider(RPC_URL_BASE))
_HASHES_USADOS = set()
_TRANSFER_TOPIC = _w3_oracle.keccak(text="Transfer(address,address,uint256)").hex().lower()

def _validar_tx_token(tx_hash: str) -> dict:
    tx_clean = tx_hash.strip().lower()
    if tx_clean in _HASHES_USADOS:
        return {"valid": False, "reason": "Hash já utilizado"}
    try:
        receipt = _w3_oracle.eth.get_transaction_receipt(tx_clean)
        if not receipt or receipt.status != 1:
            return {"valid": False, "reason": "Transação não confirmada"}
        for log in receipt.logs:
            topics = [t.hex().lower() for t in log.topics]
            if len(topics) >= 3 and topics[0] == _TRANSFER_TOPIC:
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() == RECEIVER_WALLET_EVM:
                    for symbol, info in TOKENS_ACEITOS_M2M.items():
                        if log.address.lower() == info["address"]:
                            amount = int(log.data.hex(), 16) / (10 ** info["decimals"])
                            if amount >= info["min_amount"]:
                                _HASHES_USADOS.add(tx_clean)
                                return {"valid": True, "token": symbol, "amount": amount, "tx": tx_clean}
        return {"valid": False, "reason": "Valor insuficiente ou token não aceito"}
    except Exception as e:
        return {"valid": False, "reason": str(e)}

@app.get("/api/v1/m2m/feed/{asset}")
async def m2m_token_feed(
    asset: str,
    x_payment_tx: str | None = Header(default=None, alias="X-Payment-Tx"),
    x_payment: str | None = Header(default=None, alias="X-PAYMENT")
):
    proof = x_payment_tx or x_payment
    if not proof:
        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment Required",
                "protocol": "x402",
                "network": "base-mainnet",
                "receiver": RECEIVER_WALLET_EVM,
                "accepted_tokens": TOKENS_ACEITOS_M2M,
                "header": "X-Payment-Tx"
            }
        )

    check = _validar_tx_token(proof)
    if not check["valid"]:
        return JSONResponse(status_code=402, content={"error": "Payment Rejected", "detail": check["reason"]})

    return JSONResponse(
        status_code=200,
        content={
            "service": "Robo2 Signals",
            "asset": asset.upper(),
            "radar_endpoint": "/radar/itens",
            "timestamp": int(time.time()),
            "settlement": {
                "token": check["token"],
                "amount": check["amount"],
                "tx_hash": check["tx"]
            }
        }
    )
