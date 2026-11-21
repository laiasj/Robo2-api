import time
from collections import deque
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import aiohttp

app = FastAPI(title="Hub M2M Proxy Verifier")

# === CARTEIRA SOLANA E COBRANÇA EM USDC ===
WALLET_ADDRESS = "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_USDC_PER_CHECK = 0.0005

# === ESTADO EM TEMPO REAL ===
used_proofs: set[str] = set()
recent_events: deque[dict] = deque(maxlen=60)
_latency_samples: deque[float] = deque(maxlen=200)

daily_tracker = {
    "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "daily_transactions": 0,
}

stats = {
    "total_requests": 0,
    "approved_tx": 0,
    "denied_402": 0,
    "error_tx": 0,
    "active_proxies": 0,
    "dead_proxies": 0,
    "total_received_usdc": 0.0,
    "avg_latency_ms": 0.0,
}


class ProxyRequest(BaseModel):
    proxy_url: str
    target_url: str = "https://httpbin.org/ip"
    timeout: int = 4


def touch_daily_counter() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if daily_tracker["day"] != today:
        daily_tracker["day"] = today
        daily_tracker["daily_transactions"] = 0
    daily_tracker["daily_transactions"] += 1
    return daily_tracker["daily_transactions"]


def get_daily_count() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if daily_tracker["day"] != today:
        daily_tracker["day"] = today
        daily_tracker["daily_transactions"] = 0
    return daily_tracker["daily_transactions"]


def record_latency(ms: float) -> None:
    _latency_samples.append(ms)
    stats["avg_latency_ms"] = round(sum(_latency_samples) / len(_latency_samples), 2)


async def verify_solana_usdc_payment(tx_signature: str | None) -> tuple[bool, str]:
    """
    Retorna (ok, motivo):
    - (True, "APPROVED")
    - (False, "DENIED") se pagamento inválido/ausente/duplicado
    - (False, "ERROR") se falhar consulta RPC/rede
    """
    if not tx_signature or len(tx_signature.strip()) < 64:
        return False, "DENIED"

    sig = tx_signature.strip()
    if sig in used_proofs:
        return False, "DENIED"

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
                if resp.status >= 500:
                    return False, "ERROR"
                data = await resp.json()

        if "error" in data:
            return False, "ERROR"

        tx_result = data.get("result")
        if not tx_result:
            return False, "DENIED"

        if tx_result.get("meta", {}).get("err") is not None:
            return False, "DENIED"

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
                    return True, "APPROVED"

        return False, "DENIED"
    except Exception:
        return False, "ERROR"


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.post("/v1/verify-proxy")
async def verify_proxy(
    payload: ProxyRequest,
    x_payment_proof: str | None = Header(default=None, alias="X-Payment-Proof"),
):
    stats["total_requests"] += 1
    daily_now = touch_daily_counter()
    now_ts = int(time.time())

    paid, outcome = await verify_solana_usdc_payment(x_payment_proof)
    if not paid:
        if outcome == "ERROR":
            stats["error_tx"] += 1
            recent_events.appendleft({
                "ts": now_ts,
                "proxy": payload.proxy_url,
                "category": "ERRO",
                "status": "RPC_OR_PAYMENT_ERROR",
                "latency_ms": 0,
            })
            raise HTTPException(
                status_code=503,
                detail={"error": "Payment verification temporary error", "daily_transactions": daily_now},
            )

        stats["denied_402"] += 1
        recent_events.appendleft({
            "ts": now_ts,
            "proxy": payload.proxy_url,
            "category": "NEGADA",
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

    stats["approved_tx"] += 1
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

                recent_events.appendleft({
                    "ts": int(time.time()),
                    "proxy": payload.proxy_url,
                    "category": "APROVADA",
                    "status": f"ACTIVE ({resp.status})",
                    "latency_ms": latency_ms,
                })
                return {
                    "status": "active",
                    "http_code": resp.status,
                    "latency_ms": latency_ms,
                    "daily_transactions": daily_now,
                    "verified_at": int(time.time()),
                }

    except Exception as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        record_latency(latency_ms)
        stats["dead_proxies"] += 1
        stats["error_tx"] += 1

        recent_events.appendleft({
            "ts": int(time.time()),
            "proxy": payload.proxy_url,
            "category": "ERRO",
            "status": f"PROXY_ERROR ({type(exc).__name__})",
            "latency_ms": latency_ms,
        })
        return {
            "status": "dead_or_blocked",
            "error": type(exc).__name__,
            "latency_ms": latency_ms,
            "daily_transactions": daily_now,
            "verified_at": int(time.time()),
        }


@app.get("/api/telemetry")
async def telemetry():
    return {
        "wallet": WALLET_ADDRESS,
        "asset": "USDC (Solana)",
        "daily_transactions": get_daily_count(),
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
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <title>Painel M2M em Tempo Real</title>
      <style>
        body { font-family: Arial, sans-serif; background: #0e1015; color: #f1f5f9; margin: 20px; }
        .top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
        .wallet { font-size: 13px; color: #93c5fd; word-break: break-all; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .card { background: #171b24; padding: 14px; border-radius: 10px; border: 1px solid #283042; }
        .label { font-size: 12px; color: #94a3b8; }
        .val { font-size: 24px; font-weight: bold; margin-top: 6px; }
        .ok { color: #4ade80; }
        .neg { color: #fbbf24; }
        .err { color: #f87171; }
        table { width: 100%; border-collapse: collapse; background: #171b24; border-radius: 10px; overflow: hidden; }
        th, td { padding: 10px 12px; border-bottom: 1px solid #283042; text-align: left; font-size: 13px; }
        th { background: #1e2430; color: #cbd5e1; }
      </style>
    </head>
    <body>
      <div class="top">
        <h2 style="margin:0;">Monitoramento de Transações em Tempo Real</h2>
        <div class="wallet" id="wallet">Carregando carteira...</div>
      </div>
      <div class="grid">
        <div class="card"><div class="label">Transações Hoje (Dia)</div><div class="val" id="daily">0</div></div>
        <div class="card"><div class="label">Aprovadas</div><div class="val ok" id="approved">0</div></div>
        <div class="card"><div class="label">Negadas (402)</div><div class="val neg" id="denied">0</div></div>
        <div class="card"><div class="label">Com Erro</div><div class="val err" id="errors">0</div></div>
        <div class="card"><div class="label">USDC Acumulado</div><div class="val ok" id="usdc">$0</div></div>
        <div class="card"><div class="label">Latência Média</div><div class="val" id="lat">0 ms</div></div>
      </div>
      <table>
        <thead>
          <tr><th>Hora</th><th>Proxy</th><th>Tipo</th><th>Detalhe</th><th>Latência</th></tr>
        </thead>
        <tbody id="feed"></tbody>
      </table>
      <script>
        function fmtTime(ts) {
          if (!ts) return '-';
          return new Date(ts * 1000).toLocaleTimeString('pt-BR');
        }
        async function refresh() {
          try {
            const r = await fetch('/api/telemetry', { cache: 'no-store' });
            const data = await r.json();
            document.getElementById('wallet').innerText = 'Carteira: ' + data.wallet + ' (' + data.asset + ')';
            document.getElementById('daily').innerText = data.daily_transactions;
            document.getElementById('approved').innerText = data.stats.approved_tx;
            document.getElementById('denied').innerText = data.stats.denied_402;
            document.getElementById('errors').innerText = data.stats.error_tx;
            document.getElementById('usdc').innerText = '$' + data.stats.total_received_usdc;
            document.getElementById('lat').innerText = data.stats.avg_latency_ms + ' ms';

            const tbody = document.getElementById('feed');
            if (!data.recent_events.length) {
              tbody.innerHTML = '<tr><td colspan="5">Nenhuma transação registrada ainda.</td></tr>';
              return;
            }
            tbody.innerHTML = data.recent_events.map(ev => {
              const cls = ev.category === 'APROVADA' ? 'ok' : (ev.category === 'NEGADA' ? 'neg' : 'err');
              return `<tr>
                <td>${fmtTime(ev.ts)}</td>
                <td>${ev.proxy}</td>
                <td class="${cls}"><strong>${ev.category}</strong></td>
                <td>${ev.status}</td>
                <td>${ev.latency_ms} ms</td>
              </tr>`;
            }).join('');
          } catch (e) {
            console.error(e);
          }
        }
        refresh();
        setInterval(refresh, 1500);
      </script>
    </body>
    </html>
    """
