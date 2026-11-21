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
APP_VERSION = "4.3.1"

PAY_TO_ADDRESS = (
    os.getenv("PAY_TO_ADDRESS")
    or os.getenv("SOLANA_PAY_TO")
    or os.getenv("RECEIVER_WALLET")
    or "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk"
).strip()

EXECUTOR_PRIVATE_KEY = os.getenv("EXECUTOR_PRIVATE_KEY", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

KORA_RPC_URL = (os.getenv("KORA_RPC_URL") or os.getenv("GASLESS_SPONSOR_URL") or "").strip()
KORA_API_KEY = (os.getenv("KORA_API_KEY") or os.getenv("GASLESS_SPONSOR_API_KEY") or "").strip()

MIN_FISSURE_SPREAD_PCT = float(os.getenv("MIN_FISSURE_SPREAD_PCT", "0.015"))
MIN_POSITIVE_EDGE_PCT = float(os.getenv("MIN_POSITIVE_EDGE_PCT", "0.02"))
ESTIMATED_COST_PCT = float(os.getenv("ESTIMATED_COST_PCT", "0.01"))
MIN_PROFIT_USDC = float(os.getenv("MIN_PROFIT_USDC", "0.10"))
DEFAULT_NOTIONAL_USDC = float(os.getenv("DEFAULT_NOTIONAL_USDC", "100.0"))
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

STATE: dict[str, Any] = {
    "version": APP_VERSION,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_update": None,
    "last_error": None,
    "signals": {},
    "routes": [],
    "execution_signal": {"positive": False, "best_route": None, "net_edge_pct": 0.0},
    "stats": {
        "total": 0,
        "approved": 0,
        "failed": 0,
        "rejected": 0,
        "profit_usdc": 0.0,
        "sponsored_txs": 0,
    },
    "history": [],
}

http_client: httpx.AsyncClient | None = None


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _set_error(msg: str) -> None:
    STATE["last_error"] = msg
    print(f"[Robo2 v{APP_VERSION}][ERRO] {msg}", flush=True)


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
        _set_error(f"Chave privada inválida: {exc}")
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


async def _dispatch_transaction(signed_tx_b64: str) -> tuple[str, bool]:
    assert http_client is not None
    if KORA_RPC_URL:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if KORA_API_KEY:
            headers["x-api-key"] = KORA_API_KEY
            headers["Authorization"] = f"Bearer {KORA_API_KEY}"

        resp = await http_client.post(
            KORA_RPC_URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "signAndSendTransaction",
                "params": {"transaction": signed_tx_b64},
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(str(payload["error"]))

        res = payload.get("result", {})
        tx_sig = str(res.get("signature") or res.get("txSignature") or "") if isinstance(res, dict) else str(res)
        return tx_sig, True

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
    return str(send_data.get("result", "")), False


async def _fetch_asset_signal(client: httpx.AsyncClient, asset: str) -> dict[str, Any] | None:
    mint_info = MINTS.get(asset)
    cg_id = COINGECKO_IDS.get(asset)
    if not mint_info or not cg_id:
        return None

    mint, decimals = mint_info
    one_unit = 10**decimals

    try:
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

        ref_price = out_usdc
        try:
            cg_resp = await client.get(
                COINGECKO_PRICE_URL,
                params={"ids": cg_id, "vs_currencies": "usd"},
            )
            cg_resp.raise_for_status()
            cg_val = cg_resp.json().get(cg_id, {}).get("usd")
            if cg_val is not None:
                ref_price = float(cg_val)
        except Exception as cg_exc:
            print(f"[Robo2 v{APP_VERSION}][AVISO] CoinGecko {asset} falhou ({cg_exc}), usando Jupiter", flush=True)

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
        _set_error(f"Oráculo {asset}: {exc}")
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


class ExecuteRequest(BaseModel):
    asset: str = Field(default="SOL", description="Ativo alvo: SOL, BTC, ETH ou LINK")
    notional_usdc: float = Field(default=DEFAULT_NOTIONAL_USDC, gt=0, description="Valor base em USDC")
    force: bool = Field(default=False, description="Ignora trava de fissura ALTA para teste")
    dry_run: bool = Field(default=True, description="True = apenas simula na rede sem gastar")


async def execute_arbitrage_onchain(req: ExecuteRequest) -> dict[str, Any]:
    asset = req.asset.upper().strip()
    route_label = f"USDC -> {asset} -> USDC"

    if asset not in MINTS:
        _record_op("RECUSADA", route_label, req.notional_usdc, "Ativo não suportado")
        return {"ok": False, "status": "RECUSADA", "reason": "Ativo não suportado"}

    sig = STATE["signals"].get(asset)
    if not req.force and (not sig or sig["fissure"] != "ALTA"):
        _record_op("RECUSADA", route_label, req.notional_usdc, "Fissura não está ALTA")
        return {"ok": False, "status": "RECUSADA", "reason": "Fissura não está ALTA"}

    kp = _load_keypair()
    if kp is None:
        _record_op("RECUSADA", route_label, req.notional_usdc, "EXECUTOR_PRIVATE_KEY ausente")
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
            return {"ok": True, "status": "SIMULADA_OK", "expected_profit_usdc": expected_profit, "detail": msg}

        tx_sig, sponsored = await _dispatch_transaction(signed_tx_b64)
        if sponsored:
            STATE["stats"]["sponsored_txs"] += 1

        realized_profit = max(expected_profit, 0.0)
        STATE["stats"]["profit_usdc"] = round(STATE["stats"]["profit_usdc"] + realized_profit, 4)
        modo_txt = "KORA" if sponsored else "RPC"
        msg = f"[{modo_txt}] TX: {tx_sig[:12]}... | Lucro est.: ${expected_profit}"
        _record_op("APROVADA", route_label, req.notional_usdc, msg)
        return {
            "ok": True,
            "status": "ENVIADA",
            "sponsored_gas": sponsored,
            "tx_signature": tx_sig,
            "expected_profit_usdc": expected_profit,
        }

    except Exception as exc:
        err_msg = f"Erro na execução: {exc}"
        _set_error(err_msg)
        _record_op("FALHA", route_label, req.notional_usdc, err_msg)
        return {"ok": False, "status": "FALHA", "reason": err_msg}


async def _oracle_loop() -> None:
    assert http_client is not None
    while True:
        start = time.monotonic()
        updated_any = False
        for asset in TRACKED_ASSETS:
            sig = await _fetch_asset_signal(http_client, asset)
            if sig:
                STATE["signals"][asset] = sig
                STATE["last_update"] = sig["updated_at"]
                updated_any = True
            await asyncio.sleep(0.4)

        _compute_routes()
        if updated_any:
            print(
                f"[Robo2 v{APP_VERSION}] Ciclo OK ({STATE['last_update']}) | "
                f"Ativos: {list(STATE['signals'].keys())}",
                flush=True,
            )

        exec_sig = STATE["execution_signal"]
        if AUTO_EXECUTE and exec_sig["positive"] and exec_sig["best_route"]:
            best_asset = exec_sig["best_route"]["asset"]
            await execute_arbitrage_onchain(
                ExecuteRequest(
                    asset=best_asset,
                    notional_usdc=DEFAULT_NOTIONAL_USDC,
                    force=False,
                    dry_run=False,
                )
            )

        elapsed = time.monotonic() - start
        await asyncio.sleep(max(2.0, POLL_INTERVAL_SEC - elapsed))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0))
    task = asyncio.create_task(_oracle_loop())
    print(
        f"[Robo2 v{APP_VERSION}] Online | Carteira: {PAY_TO_ADDRESS} | "
        f"Kora: {bool(KORA_RPC_URL)} | AutoExecute: {AUTO_EXECUTE}",
        flush=True,
    )
    try:
        yield
    finally:
        task.cancel()
        await http_client.aclose()


app = FastAPI(title="Robo2 Signals & Executor", version=APP_VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "wallet_configured": bool(EXECUTOR_PRIVATE_KEY),
        "kora_configured": bool(KORA_RPC_URL),
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
    if not rows_routes:
        rows_routes = '<tr><td colspan="5">Nenhuma rota calculada ainda.</td></tr>'

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

    signer_status = "CONFIGURADA" if EXECUTOR_PRIVATE_KEY else "PENDENTE (EXECUTOR_PRIVATE_KEY)"
    kora_status = "ATIVO (KORA)" if KORA_RPC_URL else "RPC PADRÃO"
    err_banner = (
        f'<div class="err"><strong>Último erro:</strong> {STATE["last_error"]}</div>'
        if STATE["last_error"]
        else ""
    )

    return f"""
    <!doctype html>
    <html lang="pt-BR">
    <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <meta http-equiv="refresh" content="6"/>
        <title>Robo2 Signals & Executor v{APP_VERSION}</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: system-ui, sans-serif; background: #f8f5ef; color: #1f2937; margin: 14px; }}
            .card {{ background: #fff; border: 1px solid #e5dccb; border-radius: 10px; padding: 14px; margin-bottom: 14px; }}
            .err {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 13px; }}
            .table-wrap {{ width: 100%; overflow-x: auto; }}
            table {{ width: 100%; min-width: 520px; border-collapse: collapse; font-size: 13px; }}
            th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #eee; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }}
            .pill {{ background: #dcfce7; color: #166534; padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; white-space: nowrap; }}
            .addr {{ word-break: break-all; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="card" style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
            <div>
                <h2 style="margin:0;font-size:18px;">Robo2 Signals & Executor (v{APP_VERSION})</h2>
                <small>Última leitura: {STATE['last_update'] or '--'} | Fee Payer: {kora_status} | 6s</small>
            </div>
            <span class="pill">ONLINE</span>
        </div>
        {err_banner}
        <div class="grid">
            <div class="card"><small>CARTEIRA RECEBEDORA</small><br><strong class="addr">{PAY_TO_ADDRESS}</strong></div>
            <div class="card"><small>ASSINATURA ON-CHAIN</small><br><strong>{signer_status}</strong></div>
            <div class="card"><small>OPERAÇÕES (APROV / FALHA / REC)</small><br><strong>{STATE['stats']['approved']} / {STATE['stats']['failed']} / {STATE['stats']['rejected']}</strong></div>
            <div class="card"><small>LUCRO ESTIMADO ACUMULADO</small><br><strong>${STATE['stats']['profit_usdc']} USDC</strong></div>
        </div>
        <div class="card">
            <h3>Sinais e Fissuras em Tempo Real</h3>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>ATIVO</th><th>MÉDIO</th><th>BID</th><th>ASK</th><th>SPREAD</th><th>FISSURA</th><th>ATUALIZADO</th></tr></thead>
                    <tbody>{rows_signals}</tbody>
                </table>
            </div>
        </div>
        <div class="card">
            <h3>Rotas de Arbitragem & Gatilho de Execução</h3>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>ROTA</th><th>FISSURA</th><th>EDGE BRUTO</th><th>EDGE LÍQUIDO</th><th>STATUS</th></tr></thead>
                    <tbody>{rows_routes}</tbody>
                </table>
            </div>
        </div>
        <div class="card">
            <h3>Histórico de Execuções (POST /api/v1/execute)</h3>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>STATUS</th><th>ROTA</th><th>VALOR</th><th>DETALHE</th><th>HORÁRIO</th></tr></thead>
                    <tbody>{rows_hist}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
