import asyncio
import base64
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ==============================================================================
# CONFIGURAÇÃO & VARIÁVEIS DE AMBIENTE (RENDER)
# ==============================================================================
APP_VERSION = "4.1.0"

PAY_TO_ADDRESS = (
    os.getenv("PAY_TO_ADDRESS")
    or os.getenv("SOLANA_PAY_TO")
    or os.getenv("RECEIVER_WALLET")
    or "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk"
).strip()

# Chave privada Base58 da carteira executora (configure SOMENTE nas Env Vars do Render)
EXECUTOR_PRIVATE_KEY = os.getenv("EXECUTOR_PRIVATE_KEY", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

# Parâmetros de gatilho e risco
MIN_FISSURE_SPREAD_PCT = float(os.getenv("MIN_FISSURE_SPREAD_PCT", "0.015"))
MIN_POSITIVE_EDGE_PCT = float(os.getenv("MIN_POSITIVE_EDGE_PCT", "0.02"))
ESTIMATED_COST_PCT = float(os.getenv("ESTIMATED_COST_PCT", "0.01"))
MIN_PROFIT_USDC = float(os.getenv("MIN_PROFIT_USDC", "0.10"))  # Mínimo $0.10 lucro líquido
POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "12.0"))
AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "false").lower() == "true"

TRACKED_ASSETS = [
    a.strip().upper()
    for a in os.getenv("TRACKED_ASSETS", "SOL,BTC,ETH,LINK").split(",")
    if a.strip()
]

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINTS: dict[str, tuple[str, int]] = {
    "SOL": ("So11111111111111111111111111111111111111112", 9),
    "BTC": ("9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E", 6),
    "ETH": ("7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", 8),
    "LINK": ("CWE8jPTUYhdCTZYWPTe1o5DFhfdmzWKc9WK618d62452", 6),
}

COINGECKO_IDS: dict[str, str] = {
    "SOL": "solana",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LINK": "chainlink",
}

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

# ==============================================================================
# ESTADO EM MEMÓRIA (LEVE E RÁPIDO)
# ==============================================================================
STATE: dict[str, Any] = {
    "version": APP_VERSION,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_update": None,
    "last_error": None,
    "signals": {},
    "routes": [],
    "execution_signal": {"positive": False, "best_route": None, "net_edge_pct": 0.0},
    "stats": {"total": 0, "approved": 0, "failed": 0, "rejected": 0, "profit_usdc": 0.0},
    "history": [],
}

http_client: httpx.AsyncClient | None = None


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _record_op(status: str, route: str, amount: float, detail: str) -> None:
    STATE["stats"]["total"] += 1
    if status == "APROVADA":
        STATE["stats"]["approved"] += 1
    elif status == "FALHA":
        STATE["stats"]["failed"] += 1
    else:
        STATE["stats"]["rejected"] += 1

    STATE["history"].insert(
        0,
        {
            "status": status,
            "route": route,
            "amount": round(amount, 4),
            "detail": detail,
            "time": _now_str(),
        },
    )
    del STATE["history"][30:]


def _load_keypair() -> Keypair | None:
    if not EXECUTOR_PRIVATE_KEY:
        return None
    try:
        return Keypair.from_base58_string(EXECUTOR_PRIVATE_KEY)
    except Exception as exc:
        STATE["last_error"] = f"Chave privada inválida: {exc}"
        return None


async def _rpc_call(method: str, params: list[Any]) -> dict[str, Any]:
    assert http_client is not None
    resp = await http_client.post(
        SOLANA_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data


# ==============================================================================
# ORÁCULO: COLETA RÁPIDA DE PREÇOS E SPREADS
# ==============================================================================
async def _fetch_asset_signal(client: httpx.AsyncClient, asset: str) -> dict[str, Any] | None:
    mint_info = MINTS.get(asset)
    cg_id = COINGECKO_IDS.get(asset)
    if not mint_info or not cg_id:
        return None

    mint, decimals = mint_info
    one_unit = 10**decimals

    try:
        cg_resp = await client.get(
            COINGECKO_PRICE_URL,
            params={"ids": cg_id, "vs_currencies": "usd"},
        )
        cg_resp.raise_for_status()
        ref_price = float(cg_resp.json()[cg_id]["usd"])

        jup_resp = await client.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": mint,
                "outputMint": USDC_MINT,
                "amount": str(one_unit),
                "slippageBps": "10",
            },
        )
        jup_resp.raise_for_status()
        out_usdc = int(jup_resp.json()["outAmount"]) / 1_000_000

        mid = (ref_price + out_usdc) / 2.0
        bid = min(ref_price, out_usdc)
        ask = max(ref_price, out_usdc)
        spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 0.0

        if spread_pct >= MIN_FISSURE_SPREAD_PCT:
            fissure = "ALTA"
            active = True
        elif spread_pct > 0:
            fissure = "MODERADA"
            active = False
        else:
            fissure = "NEUTRA"
            active = False

        return {
            "asset": asset,
            "mid": round(mid, 6),
            "bid": round(bid, 6),
            "ask": round(ask, 6),
            "spread_pct": round(spread_pct, 5),
            "fissure": fissure,
            "active": active,
            "updated_at": _now_str(),
        }
    except Exception as exc:
        STATE["last_error"] = f"{asset}: {exc}"
        return None


def _compute_routes() -> None:
    routes: list[dict[str, Any]] = []
    best_route = None
    best_net = -999.0

    for asset, sig in STATE["signals"].items():
        gross_edge = sig["spread_pct"]
        net_edge = round(gross_edge - ESTIMATED_COST_PCT, 5)
        executable = bool(sig["active"] and net_edge >= MIN_POSITIVE_EDGE_PCT)

        item = {
            "route": f"USDC -> {asset} -> USDC",
            "asset": asset,
            "fissure": sig["fissure"],
            "gross_edge_pct": gross_edge,
            "net_edge_pct": net_edge,
            "executable": executable,
        }
        routes.append(item)

        if executable and net_edge > best_net:
            best_net = net_edge
            best_route = item

    STATE["routes"] = routes
    STATE["execution_signal"] = {
        "positive": best_route is not None,
        "best_route": best_route,
        "net_edge_pct": best_net if best_route else 0.0,
    }


# ==============================================================================
# EXECUTOR ATÔMICO ON-CHAIN (JUPITER ROUND-TRIP + SIMULAÇÃO OBRIGATÓRIA)
# ==============================================================================
class ExecuteRequest(BaseModel):
    asset: str = Field(default="SOL", description="Ativo alvo: SOL, BTC, ETH ou LINK")
    notional_usdc: float = Field(default=100.0, gt=0, description="Valor base da operação em USDC")
    force: bool = Field(default=False, description="Ignora trava de fissura ALTA para teste")
    dry_run: bool = Field(default=True, description="True = apenas simula na rede sem gastar")


async def execute_arbitrage_onchain(req: ExecuteRequest) -> dict[str, Any]:
    asset = req.asset.upper().strip()
    route_label = f"USDC -> {asset} -> USDC"

    if asset not in MINTS:
        _record_op("RECUSADA", route_label, req.notional_usdc, "Ativo não suportado")
        return {"ok": False, "status": "RECUSADA", "reason": "Ativo não suportado"}

    sig = STATE["signals"].get(asset)
    if not req.force:
        if not sig or sig["fissure"] != "ALTA":
            _record_op("RECUSADA", route_label, req.notional_usdc, "Fissura não está ALTA")
            return {"ok": False, "status": "RECUSADA", "reason": "Fissura não está ALTA"}

    kp = _load_keypair()
    if kp is None:
        _record_op("RECUSADA", route_label, req.notional_usdc, "EXECUTOR_PRIVATE_KEY ausente no Render")
        return {
            "ok": False,
            "status": "RECUSADA",
            "reason": "Configure EXECUTOR_PRIVATE_KEY nas Environment Variables do Render",
        }

    assert http_client is not None
    target_mint, _ = MINTS[asset]
    amount_in_atomic = int(req.notional_usdc * 1_000_000)

    try:
        q1 = await http_client.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": USDC_MINT,
                "outputMint": target_mint,
                "amount": str(amount_in_atomic),
                "slippageBps": "15",
            },
        )
        q1.raise_for_status()
        quote1 = q1.json()
        mid_amount = int(quote1["outAmount"])

        q2 = await http_client.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": target_mint,
                "outputMint": USDC_MINT,
                "amount": str(mid_amount),
                "slippageBps": "15",
            },
        )
        q2.raise_for_status()
        quote2 = q2.json()
        final_out_usdc = int(quote2["outAmount"]) / 1_000_000
        expected_profit = round(final_out_usdc - req.notional_usdc, 4)

        if expected_profit < MIN_PROFIT_USDC and not req.force:
            msg = f"Lucro líquido insuficiente (${expected_profit} < ${MIN_PROFIT_USDC})"
            _record_op("RECUSADA", route_label, req.notional_usdc, msg)
            return {"ok": False, "status": "RECUSADA", "reason": msg, "expected_profit_usdc": expected_profit}

        swap_resp = await http_client.post(
            JUPITER_SWAP_URL,
            json={
                "quoteResponse": quote1,
                "userPublicKey": str(kp.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            },
        )
        swap_resp.raise_for_status()
        swap_tx_b64 = swap_resp.json()["swapTransaction"]

        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
        signed_tx = VersionedTransaction(raw_tx.message, [kp])
        signed_tx_b64 = base64.b64encode(bytes(signed_tx)).decode("ascii")

        # Simulação obrigatória no RPC antes de enviar
        sim_data = await _rpc_call(
            "simulateTransaction",
            [signed_tx_b64, {"encoding": "base64", "commitment": "confirmed"}],
        )
        sim_err = sim_data.get("result", {}).get("value", {}).get("err")
        if sim_err is not None:
            err_msg = f"Simulação falhou: {sim_err}"
            _record_op("FALHA", route_label, req.notional_usdc, err_msg)
            return {"ok": False, "status": "FALHA", "reason": err_msg}

        if req.dry_run:
            msg = f"Simulação OK | Lucro est.: ${expected_profit}"
            _record_op("APROVADA", route_label, req.notional_usdc, msg)
            return {
                "ok": True,
                "status": "SIMULADA_OK",
                "expected_profit_usdc": expected_profit,
                "detail": msg,
            }

        send_data = await _rpc_call(
            "sendTransaction",
            [
                signed_tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                },
            ],
        )
        tx_sig = str(send_data.get("result", ""))
        STATE["stats"]["profit_usdc"] = round(STATE["stats"]["profit_usdc"] + max(expected_profit, 0.0), 4)
        msg = f"TX enviada: {tx_sig[:12]}... | Lucro est.: ${expected_profit}"
        _record_op("APROVADA", route_label, req.notional_usdc, msg)
        return {
            "ok": True,
            "status": "ENVIADA",
            "tx_signature": tx_sig,
            "expected_profit_usdc": expected_profit,
        }

    except Exception as exc:
        err_msg = f"Erro na execução: {exc}"
        _record_op("FALHA", route_label, req.notional_usdc, err_msg)
        return {"ok": False, "status": "FALHA", "reason": err_msg}


# ==============================================================================
# LOOP EM SEGUNDO PLANO
# ==============================================================================
async def _oracle_loop() -> None:
    assert http_client is not None
    while True:
        start = time.monotonic()
        for asset in TRACKED_ASSETS:
            sig = await _fetch_asset_signal(http_client, asset)
            if sig:
                STATE["signals"][asset] = sig
                STATE["last_update"] = sig["updated_at"]
            await asyncio.sleep(0.35)

        _compute_routes()

        exec_sig = STATE["execution_signal"]
        if AUTO_EXECUTE and exec_sig["positive"] and exec_sig["best_route"]:
            best_asset = exec_sig["best_route"]["asset"]
            await execute_arbitrage_onchain(
                ExecuteRequest(asset=best_asset, notional_usdc=100.0, force=False, dry_run=False)
            )

        elapsed = time.monotonic() - start
        await asyncio.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0))
    task = asyncio.create_task(_oracle_loop())
    print(f"[Robo2 v{APP_VERSION}] Online | Carteira: {PAY_TO_ADDRESS} | AutoExecute: {AUTO_EXECUTE}")
    try:
        yield
    finally:
        task.cancel()
        await http_client.aclose()


app = FastAPI(title="Robo2 Signals & Executor", version=APP_VERSION, lifespan=lifespan)


# ==============================================================================
# ROTAS API & PAINEL
# ==============================================================================
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "wallet_configured": bool(EXECUTOR_PRIVATE_KEY),
        "auto_execute": AUTO_EXECUTE,
        "last_update": STATE["last_update"],
        "last_error": STATE["last_error"],
    }


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(STATE)


@app.post("/api/v1/execute")
async def api_execute(req: ExecuteRequest) -> JSONResponse:
    result = await execute_arbitrage_onchain(req)
    return JSONResponse(result)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    rows_signals = ""
    for asset, s in STATE["signals"].items():
        badge_color = "#16a34a" if s["fissure"] == "ALTA" else "#64748b"
        rows_signals += f"""
        <tr>
            <td><strong>{asset}</strong></td>
            <td>{s['mid']}</td>
            <td>{s['bid']}</td>
            <td>{s['ask']}</td>
            <td>{s['spread_pct']}%</td>
            <td><span style="padding:2px 8px;border-radius:999px;background:{badge_color};color:#fff;font-size:12px;">{s['fissure']}</span></td>
            <td>{s['updated_at']}</td>
        </tr>
        """
    if not rows_signals:
        rows_signals = '<tr><td colspan="7">Coletando primeiro ciclo...</td></tr>'

    rows_routes = ""
    for r in STATE["routes"]:
        status_txt = "ATIVA (EXECUTÁVEL)" if r["executable"] else "AGUARDANDO ALTA"
        rows_routes += f"""
        <tr>
            <td><strong>{r['route']}</strong></td>
            <td>{r['fissure']}</td>
            <td>{r['gross_edge_pct']}%</td>
            <td>{r['net_edge_pct']}%</td>
            <td>{status_txt}</td>
        </tr>
        """

    rows_hist = ""
    for h in STATE["history"]:
        rows_hist += f"""
        <tr>
            <td><strong>{h['status']}</strong></td>
            <td>{h['route']}</td>
            <td>${h['amount']}</td>
            <td>{h['detail']}</td>
            <td>{h['time']}</td>
        </tr>
        """
    if not rows_hist:
        rows_hist = '<tr><td colspan="5">Nenhuma execução registrada ainda.</td></tr>'

    signer_status = "CONFIGURADA" if EXECUTOR_PRIVATE_KEY else "PENDENTE (Env Var EXECUTOR_PRIVATE_KEY)"

    return f"""
    <!doctype html>
    <html lang="pt-BR">
    <head>
        <meta charset="utf-8"/>
        <meta http-equiv="refresh" content="6"/>
        <title>Robo2 Signals & Executor v{APP_VERSION}</title>
        <style>
            body {{ font-family: system-ui, sans-serif; background: #f8f5ef; color: #1f2937; margin: 24px; }}
            .card {{ background: #fff; border: 1px solid #e5dccb; border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
            th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #eee; }}
            .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
            .pill {{ background: #dcfce7; color: #166534; padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="card" style="display:flex;justify-content:space-between;align-items:center;">
            <div>
                <h2 style="margin:0;">Robo2 Signals & Executor (v{APP_VERSION})</h2>
                <small>Última leitura: {STATE['last_update'] or '--'} | Atualização a cada 6s</small>
            </div>
            <span class="pill">ONLINE</span>
        </div>

        <div class="grid">
            <div class="card"><small>CARTEIRA RECEBEDORA</small><br><strong style="font-size:12px;">{PAY_TO_ADDRESS}</strong></div>
            <div class="card"><small>ASSINATURA ON-CHAIN</small><br><strong>{signer_status}</strong></div>
            <div class="card"><small>OPERAÇÕES (APROV / FALHA / REC)</small><br><strong>{STATE['stats']['approved']} / {STATE['stats']['failed']} / {STATE['stats']['rejected']}</strong></div>
            <div class="card"><small>LUCRO ESTIMADO ACUMULADO</small><br><strong>${STATE['stats']['profit_usdc']} USDC</strong></div>
        </div>

        <div class="card">
            <h3>Sinais e Fissuras em Tempo Real</h3>
            <table>
                <thead><tr><th>ATIVO</th><th>MÉDIO</th><th>BID</th><th>ASK</th><th>SPREAD</th><th>FISSURA</th><th>ATUALIZADO</th></tr></thead>
                <tbody>{rows_signals}</tbody>
            </table>
        </div>

        <div class="card">
            <h3>Rotas de Arbitragem & Gatilho de Execução</h3>
            <table>
                <thead><tr><th>ROTA</th><th>FISSURA</th><th>EDGE BRUTO</th><th>EDGE LÍQUIDO</th><th>STATUS</th></tr></thead>
                <tbody>{rows_routes}</tbody>
            </table>
        </div>

        <div class="card">
            <h3>Histórico de Execuções (POST /api/v1/execute)</h3>
            <table>
                <thead><tr><th>STATUS</th><th>ROTA</th><th>VALOR</th><th>DETALHE</th><th>HORÁRIO</th></tr></thead>
                <tbody>{rows_hist}</tbody>
            </table>
        </div>
    </body>
    </html>
    """
