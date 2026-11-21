import asyncio
import time
from collections import deque
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import aiohttp

app = FastAPI(title="Hub M2M Proxy Verifier")

# === CARTEIRA SOLANA E COBRANÇA EM USDC ===
WALLET_ADDRESS = "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_USDC_PER_CHECK = 0.0005  # $0.0005 por validação de proxy

# === ESTADO EM MEMÓRIA PARA LIQUIDAÇÃO E MONITORAMENTO EM TEMPO REAL ===
used_proofs: set[str] = set()
recent_events: deque[dict] = deque(maxlen=50)
stats = {
    "total_requests": 0,
    "paid_requests": 0,
    "rejected_402": 0,
    "active_proxies": 0,
    "dead_proxies": 0,
    "total_received_usdc": 0.0,
    "avg_latency_ms": 0.0,
}
_latency_samples: deque[float] = deque(maxlen=200)


class ProxyRequest(BaseModel):
    proxy_url: str
    target_url: str = "https://httpbin.org/ip"
    timeout: int = 4


def record_latency(ms: float) -> None:
    _latency_samples.append(ms)
    stats["avg_latency_ms"] = round(sum(_latency_samples) / len(_latency_samples), 2)


async def verify_solana_usdc_payment(tx_signature: str | None) -> bool:
    """
    Consulta a assinatura da transação na RPC da Solana e confirma se houve
    transferência de USDC para WALLET_ADDRESS com valor >= MIN_USDC_PER_CHECK.
    """
    if not tx_signature:
        return False

    sig = tx_signature.strip()
    if len(sig) < 64 or sig in used_proofs:
        return False

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            sig,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                SOLANA_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                data = await resp.json()

        tx_result = data.get("result")
        if not tx_result or tx_result.get("meta", {}).get("err") is not None:
            return False

        meta = tx_result["meta"]
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])

        pre_map: dict[tuple[int, str], float] = {}
        for item in pre_balances:
            if item.get("mint") == USDC_MINT_SOLANA and item.get("owner") == WALLET_ADDRESS:
                amt = float(item.get("uiTokenAmount", {}).get("uiAmount") or 0.0)
                pre_map[(item["accountIndex"], USDC_MINT_SOLANA)] = amt

        for item in post_balances:
            if item.get("mint") == USDC_MINT_SOLANA and item.get("owner") == WALLET_ADDRESS:
                idx = item["accountIndex"]
                post_amt = float(item.get("uiTokenAmount", {}).get("uiAmount") or 0.0)
                pre_amt = pre_map.get((idx, USDC_MINT_SOLANA), 0.0)
                if (post_amt - pre_amt) >= MIN_USDC_PER_CHECK:
                    used_proofs.add(sig)
                    stats["total_received_usdc"] = round(
                        stats["total_received_usdc"] + (post_amt - pre_amt), 6
                    )
                    return True

        return False
    except Exception:
        return False


@app.post("/v1/verify-proxy")
async def verify_proxy(
    payload: ProxyRequest,
    x_payment_proof: str | None = Header(default=None, alias="X-Payment-Proof"),
):
    stats["total_requests"] += 1

    paid = await verify_solana_usdc_payment(x_payment_proof)
    if not paid:
        stats["rejected_402"] += 1
        recent_events.appendleft({
            "ts": int(time.time()),
            "proxy": payload.proxy_url,
            "status": "402_PAYMENT_REQUIRED",
            "latency_ms": 0,
        })
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Payment Required",
                "network": "solana-mainnet",
                "asset": "USDC",
                "wallet": WALLET_ADDRESS,
                "min_amount_usdc": MIN_USDC_PER_CHECK,
                "header": "X-Payment-Proof",
            },
        )

    stats["paid_requests"] += 1
    start = time.perf_counter()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                payload.target_url,
                proxy=payload.proxy_url,
                timeout=aiohttp.ClientTimeout(total=payload.timeout),
            ) as resp:
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                record_latency(latency_ms)
                stats["active_proxies"] += 1

                result = {
                    "status": "active",
                    "http_code": resp.status,
                    "latency_ms": latency_ms,
                    "verified_at": int(time.time()),
                }
                recent_events.appendleft({
                    "ts": result["verified_at"],
                    "proxy": payload.proxy_url,
                    "status": "ACTIVE",
                    "latency_ms": latency_ms,
                })
                return result

    except Exception as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        record_latency(latency_ms)
        stats["dead_proxies"] += 1

        recent_events.appendleft({
            "ts": int(time.time()),
            "proxy": payload.proxy_url,
            "status": f"DEAD ({type(exc).__name__})",
            "latency_ms": latency_ms,
        })
        return {
            "status": "dead_or_blocked",
            "error": type(exc).__name__,
            "latency_ms": latency_ms,
            "verified_at": int(time.time()),
        }


@app.get("/api/telemetry")
async def telemetry():
    return {
        "wallet": WALLET_ADDRESS,
        "asset": "USDC (Solana)",
        "stats": stats,
        "recent_events": list(recent_events),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8" />
      <title>Monitoramento M2M — Proxy Verifier</title>
      <style>
        body { font-family: Arial, sans-serif; background: #111; color: #eee; margin: 24px; }
        .grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
        .card { background: #1c1c1c; padding: 16px; border-radius: 8px; border: 1px solid #2e2e2e; }
        .label { font-size: 12px; color: #aaa; }
        .val { font-size: 22px; font-weight: bold; margin-top: 6px; }
        table { width: 100%; border-collapse: collapse; background: #1c1c1c; }
        th, td { padding: 10px; border-bottom: 1px solid #2e2e2e; text-align: left; font-size: 13px; }
      </style>
    </head>
    <body>
      <h2>Painel em Tempo Real — Proxy Verifier (Hub)</h2>
      <div id="wallet" style="margin-bottom:16px; color:#9ad;"></div>
      <div class="grid">
        <div class="card"><div class="label">Requisições Totais</div><div class="val" id="total">0</div></div>
        <div class="card"><div class="label">Pagas / Validadas</div><div class="val" id="paid">0</div></div>
        <div class="card"><div class="label">Bloqueadas (402)</div><div class="val" id="rej">0</div></div>
        <div class="card"><div class="label">Saldo Acumulado</div><div class="val" id="usdc">$0</div></div>
        <div class="card"><div class="label">Latência Média</div><div class="val" id="lat">0 ms</div></div>
      </div>
      <table>
        <thead><tr><th>Proxy</th><th>Status</th><th>Latência</th></tr></thead>
        <tbody id="feed"></tbody>
      </table>
      <script>
        async function refresh() {
          const r = await fetch('/api/telemetry');
          const data = await r.json();
          document.getElementById('wallet').innerText = 'Carteira: ' + data.wallet + ' (' + data.asset + ')';
          document.getElementById('total').innerText = data.stats.total_requests;
          document.getElementById('paid').innerText = data.stats.paid_requests;
          document.getElementById('rej').innerText = data.stats.rejected_402;
          document.getElementById('usdc').innerText = '$' + data.stats.total_received_usdc;
          document.getElementById('lat').innerText = data.stats.avg_latency_ms + ' ms';
          const tbody = document.getElementById('feed');
          tbody.innerHTML = data.recent_events.map(ev =>
            `<tr><td>${ev.proxy}</td><td>${ev.status}</td><td>${ev.latency_ms} ms</td></tr>`
          ).join('');
        }
        refresh();
        setInterval(refresh, 1500);
      </script>
    </body>
    </html>
    """
