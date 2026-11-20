import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from web3 import Web3

load_dotenv()

# =====================================================================
# 1. CONFIGURAÇÃO GERAL, SOLANA (JUPITER), ORÁCULO, x402 & WEBHOOK
# =====================================================================
APP_VERSION = "3.2.1"
SERVICE_NAME = "Robo2 Signals - Autonomous Data & Oracle API"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://robo2-api.onrender.com").rstrip("/")

DEFAULT_WALLET = "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk"
SOLANA_PAY_TO = os.getenv("SOLANA_PAY_TO", DEFAULT_WALLET).strip()
RECEIVER_WALLET = os.getenv("RECEIVER_WALLET", DEFAULT_WALLET).strip()
RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

PRICE_USDC = os.getenv("PRICE_USDC", "0.01").strip()
PRICE_LINK = os.getenv("PRICE_LINK", "0.0005").strip()
MIN_EXECUTABLE_SPREAD_PCT = float(os.getenv("MIN_EXECUTABLE_SPREAD_PCT", "0.015"))

# Parâmetros de rota Flash Loan (one-to-one) e memória adaptativa
FLASHLOAN_FEE_PCT = float(os.getenv("FLASHLOAN_FEE_PCT", "0.05"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.04"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.02"))
ADAPTIVE_STREAK_THRESHOLD = int(os.getenv("ADAPTIVE_STREAK_THRESHOLD", "3"))

# Configuração do Agente Webhook (executor rápido)
WEBHOOK_TARGET_URL = os.getenv("WEBHOOK_TARGET_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_COOLDOWN_SECONDS = int(os.getenv("WEBHOOK_COOLDOWN_SECONDS", "30"))

COLLECT_INTERVAL_SECONDS = int(os.getenv("COLLECT_INTERVAL_SECONDS", "15"))
TRACKED_ASSETS = [
    a.strip().upper()
    for a in os.getenv("TRACKED_ASSETS", "SOL,BTC,ETH,LINK").split(",")
    if a.strip()
]

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_SOL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SOLANA_MINTS: dict[str, tuple[str, int]] = {
    "SOL": (SOL_MINT, 9),
    "BTC": ("9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E", 6),
    "ETH": ("7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", 8),
    "LINK": ("CWE8Bde8odCT2LDqhZeWh7AKv6n23oA3Uqe3qhJX3h", 6),
}

COINGECKO_IDS: dict[str, str] = {
    "SOL": "solana",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LINK": "chainlink",
}

ACCEPTED_TOKENS: dict[str, dict[str, Any]] = {
    "USDC": {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower(),
        "decimals": 6,
        "min_amount": float(PRICE_USDC),
    },
    "LINK": {
        "address": "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196".lower(),
        "decimals": 18,
        "min_amount": float(PRICE_LINK),
    },
}

# =====================================================================
# 2. ESTADO CENTRAL, MÉTRICAS, ROTAS FLASH LOAN & MEMÓRIA ADAPTATIVA
# =====================================================================
w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 8}))
TRANSFER_TOPIC = w3.keccak(text="Transfer(address,address,uint256)").hex().lower()
USED_TX_HASHES: set[str] = set()
LAST_WEBHOOK_SENT_BY_ASSET: dict[str, int] = {}

ROBO2_STATE: dict[str, Any] = {
    "service": SERVICE_NAME,
    "source_mode": "solana-jupiter-flashloan-router-adaptive",
    "status": "starting",
    "started_at": int(time.time()),
    "last_update": 0,
    "last_error": None,
    "total_operations": 0,
    "approved_operations": 0,
    "failed_operations": 0,
    "rejected_operations": 0,
    "webhook_dispatches": 0,
    "last_webhook_status": "idle",
    "revenue_by_token": {"USDC": 0.0, "LINK": 0.0},
    "signals": {},
    "pair_rates": {},
    "flashloan_routes": [],
    "learned_routes": {},
    "observed_flashloans": [],
    "radar_items": [],
    "recent_settlements": [],
    "recent_operations": [],
}


class WebhookAgentReport(BaseModel):
    status: str  # "approved" | "rejected" | "failed"
    asset: str = "SOL"
    token: str | None = None
    amount: float = 0.0
    tx_hash: str | None = None
    detail: str = ""


class FlashLoanObservation(BaseModel):
    path: list[str]
    success: bool = True
    tx_signature: str | None = None
    source: str = "external-observer"


def record_operation(
    status: str,
    route: str,
    asset: str = "-",
    detail: str = "",
    token: str | None = None,
    amount: float = 0.0,
) -> None:
    now_ts = int(time.time())
    norm_status = status.strip().lower()
    ROBO2_STATE["total_operations"] += 1

    if norm_status == "approved":
        ROBO2_STATE["approved_operations"] += 1
    elif norm_status == "rejected":
        ROBO2_STATE["rejected_operations"] += 1
    else:
        norm_status = "failed"
        ROBO2_STATE["failed_operations"] += 1

    entry = {
        "status": norm_status,
        "route": route,
        "asset": asset,
        "token": token or "-",
        "amount": amount,
        "detail": detail,
        "time": now_ts,
    }
    ROBO2_STATE["recent_operations"].insert(0, entry)
    ROBO2_STATE["recent_operations"] = ROBO2_STATE["recent_operations"][:35]


# =====================================================================
# 3. COLETA SOLANA (JUPITER) + PARIDADES + CALCULADORA ONE-TO-ONE
# =====================================================================
def _normalize_symbol(symbol: str) -> str:
    clean = symbol.strip().upper()
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if clean.endswith(suffix) and len(clean) > len(suffix):
            clean = clean[: -len(suffix)]
            break
    return clean


def _build_quote(
    provider: str,
    asset: str,
    price_usd: float,
    spread_pct: float,
    now_ts: int,
    status: str = "live",
) -> dict[str, Any]:
    clean_spread = round(max(0.0, spread_pct), 5)
    mid = round(price_usd, 6)
    half_gap = mid * (clean_spread / 100.0) if clean_spread > 0 else mid * 0.0002
    bid = round(max(0.0, mid - half_gap), 6)
    ask = round(mid + half_gap, 6)
    fissure = (
        "ALTA"
        if clean_spread >= MIN_EXECUTABLE_SPREAD_PCT
        else "MODERADA"
        if clean_spread > 0
        else "NEUTRA"
    )
    return {
        "provider": provider,
        "asset": asset,
        "value": mid,
        "price_usd": mid,
        "bid": bid,
        "ask": ask,
        "spread_pct": clean_spread,
        "route_spread_pct": clean_spread,
        "executable_hint": clean_spread >= MIN_EXECUTABLE_SPREAD_PCT,
        "fissure": fissure,
        "timestamp": now_ts,
        "status": status,
    }


async def fetch_solana_jupiter_quote(client: httpx.AsyncClient, asset: str) -> dict[str, Any] | None:
    norm = _normalize_symbol(asset)
    now_ts = int(time.time())
    mint_info = SOLANA_MINTS.get(norm)
    headers = {"Accept": "application/json", "User-Agent": "Robo2Signals/3.2.1"}

    if mint_info:
        mint_addr, decimals = mint_info
        raw_amount = 10 ** decimals
        try:
            resp = await client.get(
                "https://quote-api.jup.ag/v6/quote",
                params={
                    "inputMint": mint_addr,
                    "outputMint": USDC_SOL_MINT,
                    "amount": str(raw_amount),
                    "slippageBps": 50,
                },
                headers=headers,
                timeout=7.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                price_usd = float(data.get("outAmount") or 0.0) / (10**6)
                price_impact = abs(float(data.get("priceImpactPct") or 0.0)) * 100.0
                route_plan = data.get("routePlan") or []
                spread_val = price_impact if price_impact > 0 else max(0.003, len(route_plan) * 0.004)
                if price_usd > 0:
                    return _build_quote("Jupiter (Solana)", norm, price_usd, spread_val, now_ts, "live")
        except Exception as exc:
            ROBO2_STATE["last_error"] = f"Jupiter {norm}: {exc}"

    cg_id = COINGECKO_IDS.get(norm)
    if cg_id:
        try:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd", "include_24hr_change": "true"},
                headers=headers,
                timeout=7.0,
            )
            if resp.status_code == 200:
                entry = resp.json().get(cg_id) or {}
                price_usd = float(entry.get("usd") or 0.0)
                if price_usd > 0:
                    change_24h = float(entry.get("usd_24h_change") or 0.0)
                    spread_est = min(abs(change_24h) * 0.001, 2.5)
                    return _build_quote("CoinGecko Fallback", norm, price_usd, spread_est, now_ts, "fallback")
            else:
                ROBO2_STATE["last_error"] = f"CoinGecko {norm}: HTTP {resp.status_code}"
        except Exception as exc:
            ROBO2_STATE["last_error"] = f"CoinGecko {norm}: {exc}"

    existing = ROBO2_STATE["signals"].get(norm)
    if existing:
        return {**existing, "timestamp": now_ts, "status": "cached"}

    return None


def compute_pair_rates(signals: dict[str, dict[str, Any]]) -> dict[str, float]:
    rates: dict[str, float] = {}
    usd_prices: dict[str, float] = {"USDC": 1.0}
    for sym, sig in signals.items():
        val = float(sig.get("value") or 0.0)
        if val > 0:
            usd_prices[sym] = val

    tokens = list(usd_prices.keys())
    for a in tokens:
        for b in tokens:
            if a != b and usd_prices[b] > 0:
                rates[f"{a}/{b}"] = round(usd_prices[a] / usd_prices[b], 8)
    return rates


def evaluate_one_to_one_path(path: list[str], pair_rates: dict[str, float], signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if len(path) < 3 or path[0] != path[-1]:
        return {"path": path, "valid_loop": False, "net_profit_pct": 0.0, "profitable": False}

    legs: list[dict[str, Any]] = []
    multiplier = 1.0
    spread_bonus_pct = 0.0

    for i in range(len(path) - 1):
        src = path[i]
        dst = path[i + 1]
        pair_key = f"{src}/{dst}"
        rate = pair_rates.get(pair_key, 1.0)
        sig_src = signals.get(src) or signals.get(dst) or {}
        leg_spread = float(sig_src.get("spread_pct") or 0.0)
        spread_bonus_pct += leg_spread
        multiplier *= rate
        legs.append({"from": src, "to": dst, "pair": pair_key, "rate": rate})

    gross_return_pct = round(spread_bonus_pct, 5)
    total_cost_pct = round(FLASHLOAN_FEE_PCT + ESTIMATED_SLIPPAGE_PCT, 5)
    net_profit_pct = round(gross_return_pct - total_cost_pct, 5)
    route_key = "->".join(path)
    learned = ROBO2_STATE["learned_routes"].get(route_key, {})
    is_adapted = bool(learned.get("adapted", False))

    return {
        "route_key": route_key,
        "path": path,
        "one_to_one": True,
        "legs": legs,
        "implied_loop_factor": round(multiplier, 8),
        "gross_return_pct": gross_return_pct,
        "estimated_cost_pct": total_cost_pct,
        "net_profit_pct": net_profit_pct,
        "profitable": net_profit_pct >= MIN_NET_PROFIT_PCT,
        "adapted_from_network": is_adapted,
        "observed_streak": learned.get("streak", 0),
    }


def build_one_to_one_routes(signals: dict[str, dict[str, Any]], pair_rates: dict[str, float]) -> list[dict[str, Any]]:
    active_assets = [a for a in ["SOL", "BTC", "ETH", "LINK"] if a in signals]
    candidate_paths: list[list[str]] = [
        ["SOL", "USDC", "ETH", "SOL"],
        ["SOL", "USDC", "BTC", "SOL"],
        ["SOL", "USDC", "LINK", "SOL"],
        ["USDC", "SOL", "USDC"],
        ["USDC", "ETH", "USDC"],
    ]

    for route_key, info in ROBO2_STATE["learned_routes"].items():
        if info.get("adapted"):
            parts = route_key.split("->")
            if len(parts) >= 3 and parts[0] == parts[-1] and parts not in candidate_paths:
                candidate_paths.append(parts)

    evaluated: list[dict[str, Any]] = []
    for p in candidate_paths:
        if all(tok == "USDC" or tok in active_assets for tok in p):
            evaluated.append(evaluate_one_to_one_path(p, pair_rates, signals))

    evaluated.sort(key=lambda r: (r["adapted_from_network"], r["net_profit_pct"]), reverse=True)
    return evaluated


# =====================================================================
# 4. AGENTE MONITOR DE FLASH LOANS NA REDE & MEMÓRIA ADAPTATIVA
# =====================================================================
def register_flashloan_observation(path: list[str], success: bool, tx_signature: str | None = None, source: str = "rpc-monitor") -> None:
    clean_path = [p.strip().upper() for p in path if p.strip()]
    if len(clean_path) < 3 or clean_path[0] != clean_path[-1]:
        return

    now_ts = int(time.time())
    route_key = "->".join(clean_path)
    obs = {
        "route_key": route_key,
        "path": clean_path,
        "success": success,
        "tx_signature": tx_signature,
        "source": source,
        "time": now_ts,
    }
    ROBO2_STATE["observed_flashloans"].insert(0, obs)
    ROBO2_STATE["observed_flashloans"] = ROBO2_STATE["observed_flashloans"][:30]

    mem = ROBO2_STATE["learned_routes"].get(
        route_key,
        {"route_key": route_key, "streak": 0, "total_hits": 0, "adapted": False, "last_seen": now_ts},
    )
    if success:
        mem["streak"] += 1
        mem["total_hits"] += 1
    else:
        mem["streak"] = 0

    mem["last_seen"] = now_ts
    if mem["streak"] >= ADAPTIVE_STREAK_THRESHOLD:
        mem["adapted"] = True

    ROBO2_STATE["learned_routes"][route_key] = mem


async def monitor_network_flash_loans(client: httpx.AsyncClient) -> None:
    if not SOLANA_RPC_URL:
        return
    try:
        resp = await client.post(
            SOLANA_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [SOL_MINT, {"limit": 5}],
            },
            timeout=6.0,
        )
        if resp.status_code == 200:
            result = resp.json().get("result") or []
            for idx, item in enumerate(result):
                if item.get("err") is None and idx == 0:
                    sig = item.get("signature")
                    recent_sigs = {o.get("tx_signature") for o in ROBO2_STATE["observed_flashloans"]}
                    if sig and sig not in recent_sigs:
                        register_flashloan_observation(
                            ["SOL", "USDC", "SOL"],
                            success=True,
                            tx_signature=sig,
                            source="solana-rpc",
                        )
                        break
    except Exception:
        pass


# =====================================================================
# 5. DISPARO DO AGENTE WEBHOOK & COLETA CONTÍNUA
# =====================================================================
async def dispatch_webhook_agent(client: httpx.AsyncClient, signal: dict[str, Any], best_route: dict[str, Any] | None) -> None:
    if not WEBHOOK_TARGET_URL:
        return
    if not signal.get("executable_hint"):
        return

    asset = signal["asset"]
    now_ts = int(time.time())
    last_sent = LAST_WEBHOOK_SENT_BY_ASSET.get(asset, 0)
    if (now_ts - last_sent) < WEBHOOK_COOLDOWN_SECONDS:
        return

    payload = {
        "event": "robo2.signal.fissure",
        "agent": "agente-webhook",
        "timestamp": now_ts,
        "wallet": SOLANA_PAY_TO,
        "signal": signal,
        "best_flashloan_route": best_route,
    }
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Robo2-Event": "robo2.signal.fissure",
    }
    if WEBHOOK_SECRET:
        sig = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        headers["X-Robo2-Signature"] = f"sha256={sig}"

    LAST_WEBHOOK_SENT_BY_ASSET[asset] = now_ts
    try:
        resp = await client.post(WEBHOOK_TARGET_URL, content=body_bytes, headers=headers, timeout=5.0)
        ROBO2_STATE["webhook_dispatches"] += 1
        ROBO2_STATE["last_webhook_status"] = f"HTTP {resp.status_code} ({asset})"
    except Exception as exc:
        ROBO2_STATE["last_webhook_status"] = f"Erro ({asset}): {exc}"
        record_operation("failed", "/webhook/dispatch", asset, f"Falha ao acionar Agente Webhook: {exc}")


def rebuild_radar_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for asset, sig in ROBO2_STATE["signals"].items():
        spread = sig.get("spread_pct", 0.0)
        items.append(
            {
                "id": f"sig-{asset.lower()}-{sig['timestamp']}",
                "asset": asset,
                "mid_price": sig["value"],
                "bid": sig["bid"],
                "ask": sig["ask"],
                "spread_pct": spread,
                "route_spread_pct": sig.get("route_spread_pct", spread),
                "executable_hint": sig.get("executable_hint", False),
                "fissure": sig.get("fissure", "NEUTRA"),
                "provider": sig.get("provider", "Jupiter (Solana)"),
                "timestamp": sig["timestamp"],
            }
        )
    return items


async def background_collector_loop() -> None:
    ROBO2_STATE["status"] = "online"
    async with httpx.AsyncClient() as client:
        while True:
            updated = False
            for asset in TRACKED_ASSETS:
                try:
                    snap = await fetch_solana_jupiter_quote(client, asset)
                    if snap:
                        ROBO2_STATE["signals"][snap["asset"]] = snap
                        updated = True
                except Exception as exc:
                    ROBO2_STATE["last_error"] = f"Loop {asset}: {exc}"
                await asyncio.sleep(0.8)

            await monitor_network_flash_loans(client)

            if updated:
                ROBO2_STATE["pair_rates"] = compute_pair_rates(ROBO2_STATE["signals"])
                ROBO2_STATE["flashloan_routes"] = build_one_to_one_routes(
                    ROBO2_STATE["signals"], ROBO2_STATE["pair_rates"]
                )
                ROBO2_STATE["radar_items"] = rebuild_radar_items()
                ROBO2_STATE["last_update"] = int(time.time())

                best_route = ROBO2_STATE["flashloan_routes"][0] if ROBO2_STATE["flashloan_routes"] else None
                for sig in ROBO2_STATE["signals"].values():
                    await dispatch_webhook_agent(client, sig, best_route)

            ROBO2_STATE["status"] = "online"
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_collector_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=SERVICE_NAME, version=APP_VERSION, lifespan=lifespan)


# =====================================================================
# 6. VALIDAÇÃO DE PAGAMENTO & REGISTRO DE OPERAÇÕES
# =====================================================================
def verify_onchain_payment(tx_hash: str) -> dict[str, Any]:
    if not tx_hash:
        return {"valid": False, "kind": "rejected", "reason": "Header de pagamento ausente"}

    tx_clean = tx_hash.strip().lower()
    if tx_clean in USED_TX_HASHES:
        return {"valid": False, "kind": "rejected", "reason": "Hash de transação já utilizado"}

    if not RECEIVER_WALLET:
        return {"valid": False, "kind": "failed", "reason": "Carteira recebedora não configurada"}

    try:
        receipt = w3.eth.get_transaction_receipt(tx_clean)
        if not receipt or receipt.status != 1:
            return {"valid": False, "kind": "rejected", "reason": "Transação não confirmada ou revertida"}

        target_wallet = RECEIVER_WALLET.lower()
        for log in receipt.logs:
            topics = [t.hex().lower() if hasattr(t, "hex") else str(t).lower() for t in log.topics]
            if len(topics) >= 3 and topics[0] == TRANSFER_TOPIC:
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() == target_wallet:
                    contract_addr = log.address.lower()
                    for symbol, info in ACCEPTED_TOKENS.items():
                        if contract_addr == info["address"]:
                            data_hex = log.data.hex() if hasattr(log.data, "hex") else str(log.data)
                            raw_val = int(data_hex, 16)
                            amount = raw_val / (10 ** info["decimals"])
                            if amount >= info["min_amount"]:
                                USED_TX_HASHES.add(tx_clean)
                                ROBO2_STATE["revenue_by_token"][symbol] = round(
                                    ROBO2_STATE["revenue_by_token"].get(symbol, 0.0) + amount, 6
                                )
                                settlement = {
                                    "token": symbol,
                                    "amount": amount,
                                    "tx": tx_clean,
                                    "time": int(time.time()),
                                }
                                ROBO2_STATE["recent_settlements"].insert(0, settlement)
                                ROBO2_STATE["recent_settlements"] = ROBO2_STATE["recent_settlements"][:25]
                                return {"valid": True, "kind": "approved", **settlement}

        return {"valid": False, "kind": "rejected", "reason": "Transferência abaixo do mínimo ou token não aceito"}
    except Exception as exc:
        return {"valid": False, "kind": "failed", "reason": f"Falha na consulta RPC: {exc}"}


def _payment_required_response(resource_path: str, base_url: str) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content={
            "x402Version": 1,
            "error": "Payment Required",
            "payTo": SOLANA_PAY_TO,
            "receiver": RECEIVER_WALLET,
            "resource": f"{base_url}{resource_path}",
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": PRICE_USDC,
                    "asset": "USDC",
                    "payTo": SOLANA_PAY_TO,
                    "resource": f"{base_url}{resource_path}",
                },
                {
                    "scheme": "exact",
                    "network": "base-mainnet",
                    "maxAmountRequired": PRICE_USDC,
                    "asset": "USDC",
                    "contract": ACCEPTED_TOKENS["USDC"]["address"],
                    "payTo": RECEIVER_WALLET,
                    "resource": f"{base_url}{resource_path}",
                },
                {
                    "scheme": "exact",
                    "network": "base-mainnet",
                    "maxAmountRequired": PRICE_LINK,
                    "asset": "LINK",
                    "contract": ACCEPTED_TOKENS["LINK"]["address"],
                    "payTo": RECEIVER_WALLET,
                    "resource": f"{base_url}{resource_path}",
                },
            ],
            "instructions": "Envie pagamento em USDC ou LINK e inclua o comprovante no header X-Payment-Tx.",
        },
    )


# =====================================================================
# 7. PAINEL EM TEMPO REAL (PRESERVADO)
# =====================================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Robo2 Signals — Painel em Tempo Real</title>
  <style>
    :root {
      --bg: #faf7f2;
      --card: #fffdf9;
      --card-soft: #f4ede2;
      --ink: #2c2623;
      --muted: #6f655f;
      --gold: #b8924a;
      --gold-soft: #efe3c8;
      --lilac: #8b7ac4;
      --lilac-soft: #efeafd;
      --ok: #2f7a57;
      --ok-soft: #e4f3ec;
      --warn: #9b6b19;
      --warn-soft: #fbf1dc;
      --err: #a33b3b;
      --err-soft: #fae5e5;
      --line: #e5dac9;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 22px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.45;
    }
    .wrap {
      max-width: 1080px;
      margin: 0 auto;
      display: grid;
      gap: 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-top: 3px solid var(--gold);
      border-radius: 14px;
      padding: 18px 20px;
      box-shadow: 0 4px 16px rgba(44, 38, 35, 0.04);
    }
    .header-row {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }
    h1 { margin: 0; font-size: 1.32rem; }
    h2 { margin: 0 0 12px 0; font-size: 1.02rem; display: flex; align-items: center; gap: 8px; }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 4px 11px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 700;
      background: var(--ok-soft);
      color: var(--ok);
      border: 1px solid #bfe2d1;
    }
    .grid-4 {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 11px;
      margin-top: 14px;
    }
    .kpi {
      background: var(--card-soft);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 11px 13px;
    }
    .kpi-label { font-size: 0.74rem; color: var(--muted); text-transform: uppercase; font-weight: 600; }
    .kpi-val { font-size: 1.05rem; font-weight: 700; margin-top: 3px; word-break: break-all; }
    .tag-lilac {
      background: var(--lilac-soft);
      color: #5b4a96;
      border: 1px solid #d7cef5;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 700;
    }
    .badge-ok { background: var(--ok-soft); color: var(--ok); padding: 2px 8px; border-radius: 999px; font-size: 0.74rem; font-weight: 700; }
    .badge-warn { background: var(--warn-soft); color: var(--warn); padding: 2px 8px; border-radius: 999px; font-size: 0.74rem; font-weight: 700; }
    .badge-err { background: var(--err-soft); color: var(--err); padding: 2px 8px; border-radius: 999px; font-size: 0.74rem; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
    th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); }
    th { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; background: var(--card-soft); }
    code { background: var(--gold-soft); padding: 2px 6px; border-radius: 6px; font-size: 0.8rem; }
    .footer-note { font-size: 0.8rem; color: var(--muted); }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="header-row">
        <div>
          <h1>Robo2 Signals — Painel em Tempo Real</h1>
          <div class="footer-note" id="clock-info">Sincronizando Oráculo Live &amp; Agente Webhook...</div>
        </div>
        <span class="pill" id="status-badge">ONLINE</span>
      </div>

      <div class="grid-4">
        <div class="kpi">
          <div class="kpi-label">Receita USDC</div>
          <div class="kpi-val" id="rev-usdc">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Receita LINK</div>
          <div class="kpi-val" id="rev-link">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Total de Operações</div>
          <div class="kpi-val" id="ops-total">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Aprovadas / Falhas / Recusadas</div>
          <div class="kpi-val" id="ops-breakdown">0 / 0 / 0</div>
        </div>
      </div>

      <div class="grid-4" style="margin-top: 10px;">
        <div class="kpi" style="grid-column: span 2;">
          <div class="kpi-label">Carteira Recebedora</div>
          <div class="kpi-val" id="wallet-addr">-</div>
        </div>
        <div class="kpi" style="grid-column: span 2;">
          <div class="kpi-label">Agente Webhook &amp; Rotas</div>
          <div class="kpi-val" style="font-size: 0.84rem; font-weight: 600;">
            Disparos: <span id="wh-count">0</span> (<span id="wh-status">idle</span>)<br/>
            <code>/radar/itens</code> · <code>/api/v1/oracle/feed/{asset}</code> · <code>/api/v1/webhook/agente</code>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Sinais e Fissuras em Tempo Real <span class="tag-lilac">ORACLE LIVE</span></h2>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Ativo</th>
              <th>Médio</th>
              <th>Bid</th>
              <th>Ask</th>
              <th>Spread</th>
              <th>Fissura</th>
              <th>Atualizado</th>
            </tr>
          </thead>
          <tbody id="signals-body">
            <tr><td colspan="7">Aguardando primeiro ciclo de leitura...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2>Histórico de Operações (Aprovadas, Falhadas e Recusadas)</h2>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Rota / Ativo</th>
              <th>Valor</th>
              <th>Detalhe</th>
              <th>Horário</th>
            </tr>
          </thead>
          <tbody id="ops-body">
            <tr><td colspan="5">Nenhuma operação registrada ainda.</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2>Liquidações Pagas Recentes</h2>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Token</th>
              <th>Valor</th>
              <th>Tx Hash</th>
              <th>Horário</th>
            </tr>
          </thead>
          <tbody id="settlements-body">
            <tr><td colspan="4">Nenhuma liquidação registrada ainda.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    function fmtTime(ts) {
      if (!ts) return "--";
      return new Date(ts * 1000).toLocaleTimeString("pt-BR");
    }

    function statusBadge(st) {
      if (st === "approved") return '<span class="badge-ok">APROVADA</span>';
      if (st === "rejected") return '<span class="badge-warn">RECUSADA</span>';
      return '<span class="badge-err">FALHADA</span>';
    }

    async function refreshDashboard() {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById("status-badge").textContent = (data.status || "online").toUpperCase();
        document.getElementById("rev-usdc").textContent = data.revenue_by_token?.USDC ?? 0;
        document.getElementById("rev-link").textContent = data.revenue_by_token?.LINK ?? 0;
        document.getElementById("ops-total").textContent = data.total_operations ?? 0;
        document.getElementById("ops-breakdown").textContent =
          (data.approved_operations ?? 0) + " / " +
          (data.failed_operations ?? 0) + " / " +
          (data.rejected_operations ?? 0);
        document.getElementById("wallet-addr").textContent = data.receiver_wallet || "-";
        document.getElementById("wh-count").textContent = data.webhook_dispatches ?? 0;
        document.getElementById("wh-status").textContent = data.last_webhook_status || "idle";
        document.getElementById("clock-info").textContent =
          "Última leitura: " + fmtTime(data.last_update) + " · Atualização automática a cada 5s";

        const sigs = Object.values(data.signals || {});
        const sigBody = document.getElementById("signals-body");
        if (sigs.length === 0) {
          sigBody.innerHTML = "<tr><td colspan='7'>Aguardando primeiro ciclo de leitura...</td></tr>";
        } else {
          sigBody.innerHTML = sigs.map(s => {
            const fissura = s.fissure || (s.spread_pct >= 0.015 ? "ALTA" : (s.spread_pct > 0 ? "MODERADA" : "NEUTRA"));
            return `<tr>
              <td><strong>${s.asset}</strong></td>
              <td>${s.value}</td>
              <td>${s.bid}</td>
              <td>${s.ask}</td>
              <td>${s.spread_pct}%</td>
              <td><span class="tag-lilac">${fissura}</span></td>
              <td>${fmtTime(s.timestamp)}</td>
            </tr>`;
          }).join("");
        }

        const ops = data.recent_operations || [];
        const opsBody = document.getElementById("ops-body");
        if (ops.length === 0) {
          opsBody.innerHTML = "<tr><td colspan='5'>Nenhuma operação registrada ainda.</td></tr>";
        } else {
          opsBody.innerHTML = ops.map(op => `<tr>
            <td>${statusBadge(op.status)}</td>
            <td><code>${op.route}</code> (${op.asset})</td>
            <td>${op.amount > 0 ? (op.amount + " " + op.token) : "-"}</td>
            <td>${op.detail || "-"}</td>
            <td>${fmtTime(op.time)}</td>
          </tr>`).join("");
        }

        const settlements = data.recent_settlements || [];
        const setBody = document.getElementById("settlements-body");
        if (settlements.length === 0) {
          setBody.innerHTML = "<tr><td colspan='4'>Nenhuma liquidação registrada ainda.</td></tr>";
        } else {
          setBody.innerHTML = settlements.map(item => `<tr>
            <td><strong>${item.token}</strong></td>
            <td>${item.amount}</td>
            <td><code>${(item.tx || "").slice(0, 18)}...</code></td>
            <td>${fmtTime(item.time)}</td>
          </tr>`).join("");
        }
      } catch (e) {
        console.error("Erro ao atualizar painel:", e);
      }
    }

    refreshDashboard();
    setInterval(refreshDashboard, 5000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(content=DASHBOARD_HTML)


@app.head("/")
async def dashboard_head() -> Response:
    return Response(status_code=200)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "service": ROBO2_STATE["service"],
        "source_mode": ROBO2_STATE["source_mode"],
        "status": ROBO2_STATE["status"],
        "last_error": ROBO2_STATE["last_error"],
        "receiver_wallet": RECEIVER_WALLET,
        "solana_pay_to": SOLANA_PAY_TO,
        "last_update": ROBO2_STATE["last_update"],
        "total_operations": ROBO2_STATE["total_operations"],
        "approved_operations": ROBO2_STATE["approved_operations"],
        "failed_operations": ROBO2_STATE["failed_operations"],
        "rejected_operations": ROBO2_STATE["rejected_operations"],
        "webhook_dispatches": ROBO2_STATE["webhook_dispatches"],
        "last_webhook_status": ROBO2_STATE["last_webhook_status"],
        "revenue_by_token": ROBO2_STATE["revenue_by_token"],
        "pair_rates": ROBO2_STATE["pair_rates"],
        "flashloan_routes": ROBO2_STATE["flashloan_routes"],
        "learned_routes": ROBO2_STATE["learned_routes"],
        "observed_flashloans": ROBO2_STATE["observed_flashloans"],
        "signals": ROBO2_STATE["signals"],
        "recent_operations": ROBO2_STATE["recent_operations"],
        "recent_settlements": ROBO2_STATE["recent_settlements"],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": ROBO2_STATE["status"],
        "source_mode": ROBO2_STATE["source_mode"],
        "last_update": ROBO2_STATE["last_update"],
        "last_error": ROBO2_STATE["last_error"],
        "tracked_assets": TRACKED_ASSETS,
        "cached_assets": list(ROBO2_STATE["signals"].keys()),
        "webhook_configured": bool(WEBHOOK_TARGET_URL),
    }


@app.get("/radar/itens")
async def radar_itens(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    items = ROBO2_STATE["radar_items"] or rebuild_radar_items()
    return {
        "service": SERVICE_NAME,
        "count": len(items[:limit]),
        "last_update": ROBO2_STATE["last_update"],
        "items": items[:limit],
    }


# =====================================================================
# 8. ROTAS DE FLASH LOAN ONE-TO-ONE, OBSERVADOR & AGENTE WEBHOOK
# =====================================================================
@app.get("/api/v1/flashloan/routes")
async def flashloan_routes() -> dict[str, Any]:
    return {
        "one_to_one_required": True,
        "flashloan_fee_pct": FLASHLOAN_FEE_PCT,
        "estimated_slippage_pct": ESTIMATED_SLIPPAGE_PCT,
        "min_net_profit_pct": MIN_NET_PROFIT_PCT,
        "adaptive_streak_threshold": ADAPTIVE_STREAK_THRESHOLD,
        "pair_rates": ROBO2_STATE["pair_rates"],
        "routes": ROBO2_STATE["flashloan_routes"],
        "learned_routes": ROBO2_STATE["learned_routes"],
        "recent_observations": ROBO2_STATE["observed_flashloans"],
    }


@app.post("/api/v1/flashloan/observe")
async def flashloan_observe(obs: FlashLoanObservation) -> JSONResponse:
    clean_path = [p.strip().upper() for p in obs.path if p.strip()]
    if len(clean_path) < 3 or clean_path[0] != clean_path[-1]:
        return JSONResponse(
            status_code=400,
            content={"error": "Rota inválida: deve iniciar e terminar na mesma moeda (one-to-one), ex.: ['SOL','USDC','SOL']"},
        )

    register_flashloan_observation(clean_path, success=obs.success, tx_signature=obs.tx_signature, source=obs.source)
    ROBO2_STATE["pair_rates"] = compute_pair_rates(ROBO2_STATE["signals"])
    ROBO2_STATE["flashloan_routes"] = build_one_to_one_routes(ROBO2_STATE["signals"], ROBO2_STATE["pair_rates"])
    route_key = "->".join(clean_path)
    return JSONResponse(status_code=200, content={"ok": True, "learned": ROBO2_STATE["learned_routes"].get(route_key)})


@app.get("/api/v1/webhook/status")
async def webhook_status() -> dict[str, Any]:
    return {
        "agent": "agente-webhook",
        "configured": bool(WEBHOOK_TARGET_URL),
        "cooldown_seconds": WEBHOOK_COOLDOWN_SECONDS,
        "min_executable_spread_pct": MIN_EXECUTABLE_SPREAD_PCT,
        "dispatches": ROBO2_STATE["webhook_dispatches"],
        "last_status": ROBO2_STATE["last_webhook_status"],
    }


@app.post("/api/v1/webhook/agente")
async def webhook_agente_callback(
    report: WebhookAgentReport,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> JSONResponse:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        record_operation("rejected", "/api/v1/webhook/agente", report.asset.upper(), "Segredo do webhook inválido")
        return JSONResponse(status_code=401, content={"error": "Invalid webhook secret"})

    clean_asset = report.asset.strip().upper()
    clean_status = report.status.strip().lower()
    if clean_status not in {"approved", "rejected", "failed"}:
        clean_status = "failed"

    if clean_status == "approved" and report.token and report.amount > 0:
        tok = report.token.strip().upper()
        ROBO2_STATE["revenue_by_token"][tok] = round(
            ROBO2_STATE["revenue_by_token"].get(tok, 0.0) + report.amount, 6
        )
        if report.tx_hash:
            ROBO2_STATE["recent_settlements"].insert(
                0,
                {
                    "token": tok,
                    "amount": report.amount,
                    "tx": report.tx_hash,
                    "time": int(time.time()),
                },
            )
            ROBO2_STATE["recent_settlements"] = ROBO2_STATE["recent_settlements"][:25]

    record_operation(
        clean_status,
        "/api/v1/webhook/agente",
        clean_asset,
        report.detail or f"Retorno do Agente Webhook ({clean_status})",
        token=report.token.upper() if report.token else None,
        amount=report.amount,
    )

    return JSONResponse(status_code=200, content={"ok": True, "recorded_status": clean_status})


# =====================================================================
# 9. MANIFESTOS DE DESCOBERTA (x402, AGENT.JSON, AI-PLUGIN.JSON)
# =====================================================================
@app.get("/.well-known/x402", tags=["discovery", "x402"])
async def well_known_x402(request: Request):
    base_url = str(request.base_url).rstrip("/") if request.base_url else PUBLIC_BASE_URL
    return JSONResponse(
        content={
            "x402Version": 1,
            "name": SERVICE_NAME,
            "version": APP_VERSION,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": PRICE_USDC,
                    "asset": "USDC",
                    "payTo": SOLANA_PAY_TO,
                    "resource": f"{base_url}/radar/itens",
                    "description": "Sinais e fissuras monitoradas na Solana pelo Robo2.",
                    "mimeType": "application/json",
                },
                {
                    "scheme": "exact",
                    "network": "base-mainnet",
                    "maxAmountRequired": PRICE_LINK,
                    "asset": "LINK",
                    "contract": ACCEPTED_TOKENS["LINK"]["address"],
                    "payTo": RECEIVER_WALLET,
                    "resource": f"{base_url}/api/v1/oracle/feed/SOL",
                    "description": "Dados de preço, spread e fissuras para oráculos e agentes M2M.",
                    "mimeType": "application/json",
                },
            ],
            "endpoints": {
                "data": f"{base_url}/radar/itens",
                "oracle": f"{base_url}/api/v1/oracle/feed/SOL",
                "m2m": f"{base_url}/api/v1/m2m/feed/SOL",
                "flashloanRoutes": f"{base_url}/api/v1/flashloan/routes",
                "webhookAgent": f"{base_url}/api/v1/webhook/agente",
                "agentManifest": f"{base_url}/.well-known/agent.json",
                "openapi": f"{base_url}/openapi.json",
            },
        }
    )


@app.get("/.well-known/agent.json", tags=["discovery", "agents"])
async def well_known_agent_json(request: Request):
    base_url = str(request.base_url).rstrip("/") if request.base_url else PUBLIC_BASE_URL
    return JSONResponse(
        content={
            "schema_version": "1.0",
            "name": SERVICE_NAME,
            "version": APP_VERSION,
            "description": "Provedor autônomo de sinais, fissuras e feeds para oráculos com cobrança M2M via x402 em USDC e LINK.",
            "url": base_url,
            "capabilities": {
                "autonomous_discovery": True,
                "bot_to_bot_payment": True,
                "oracle_data_feed": True,
                "solana_jupiter_quotes": True,
                "flashloan_one_to_one_router": True,
                "webhook_agent_dispatch": True,
            },
            "payment": {
                "protocol": "x402",
                "solana_pay_to": SOLANA_PAY_TO,
                "evm_receiver": RECEIVER_WALLET,
                "price_usdc": PRICE_USDC,
                "price_link": PRICE_LINK,
                "payment_endpoint": f"{base_url}/radar/itens",
            },
            "resources": [
                {
                    "path": "/radar/itens",
                    "method": "GET",
                    "auth_or_payment": "x402",
                    "summary": "Retorna sinais e fissuras coletadas.",
                },
                {
                    "path": "/api/v1/oracle/feed/{asset}",
                    "method": "GET",
                    "auth_or_payment": "x402",
                    "summary": "Fornece dados estruturados de preço, bid/ask, spread e fissura para oráculos.",
                },
            ],
            "discovery": {
                "x402": f"{base_url}/.well-known/x402",
                "openapi": f"{base_url}/openapi.json",
            },
        }
    )


@app.get("/.well-known/ai-plugin.json", tags=["discovery", "agents"])
async def well_known_ai_plugin(request: Request):
    base_url = str(request.base_url).rstrip("/") if request.base_url else PUBLIC_BASE_URL
    return JSONResponse(
        content={
            "schema_version": "v1",
            "name_for_human": SERVICE_NAME,
            "name_for_model": "robo2_signals",
            "description_for_human": "Sinais, fissuras e dados para oráculos na Solana com pagamento em USDC e LINK.",
            "description_for_model": (
                f"API de sinais e dados para oráculos ({APP_VERSION}). Requer pagamento via protocolo x402 "
                f"em USDC ou LINK para acessar {base_url}/radar/itens e {base_url}/api/v1/oracle/feed/{{asset}}."
            ),
            "auth": {"type": "x402", "assets": ["USDC", "LINK"]},
            "api": {"type": "openapi", "url": f"{base_url}/openapi.json"},
            "logo_url": f"{base_url}/static/logo.png",
            "contact_email": "contato@lentenoir.com",
            "legal_info_url": base_url,
        }
    )


# =====================================================================
# 10. ROTAS DE DADOS PARA ORÁCULOS & M2M (USDC + LINK)
# =====================================================================
@app.get("/api/v1/m2m/feed/{asset}")
@app.get("/api/v1/oracle/feed/{asset}")
async def get_m2m_and_oracle_feed(
    request: Request,
    asset: str,
    x_payment_tx: str | None = Header(default=None, alias="X-Payment-Tx"),
    x_payment: str | None = Header(default=None, alias="X-PAYMENT"),
) -> JSONResponse:
    base_url = str(request.base_url).rstrip("/") if request.base_url else PUBLIC_BASE_URL
    clean_asset = asset.strip().upper()
    route_path = request.url.path
    proof = x_payment_tx or x_payment

    if not proof:
        record_operation("rejected", route_path, clean_asset, "Sem comprovante de pagamento (402)")
        return _payment_required_response(f"/api/v1/oracle/feed/{asset}", base_url)

    payment = await asyncio.to_thread(verify_onchain_payment, proof)
    if not payment["valid"]:
        op_status = payment.get("kind", "rejected")
        record_operation(op_status, route_path, clean_asset, payment["reason"])
        return JSONResponse(
            status_code=402,
            content={"error": "Payment Rejected", "detail": payment["reason"]},
        )

    signal = ROBO2_STATE["signals"].get(_normalize_symbol(clean_asset))
    if not signal:
        async with httpx.AsyncClient() as client:
            signal = await fetch_solana_jupiter_quote(client, clean_asset)

    if not signal:
        record_operation(
            "failed",
            route_path,
            clean_asset,
            "Pagamento confirmado, mas ativo indisponível na coleta",
            token=payment["token"],
            amount=payment["amount"],
        )
        return JSONResponse(
            status_code=404,
            content={"error": f"Ativo {clean_asset} indisponível no momento."},
        )

    record_operation(
        "approved",
        route_path,
        clean_asset,
        f"Feed entregue ({signal['fissure']})",
        token=payment["token"],
        amount=payment["amount"],
    )

    best_route = ROBO2_STATE["flashloan_routes"][0] if ROBO2_STATE["flashloan_routes"] else None

    return JSONResponse(
        status_code=200,
        content={
            **signal,
            "best_flashloan_route": best_route,
            "oracle": {
                "asset": signal["asset"],
                "price_usd": signal["value"],
                "bid": signal["bid"],
                "ask": signal["ask"],
                "spread_pct": signal["spread_pct"],
                "route_spread_pct": signal["route_spread_pct"],
                "executable_hint": signal["executable_hint"],
                "fissure": signal["fissure"],
                "updated_at": signal["timestamp"],
            },
            "settlement": {
                "paid_with": payment["token"],
                "amount": payment["amount"],
                "tx_hash": payment["tx"],
            },
        },
    )
