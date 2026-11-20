import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse
from web3 import Web3

load_dotenv()

# =====================================================================
# 1. CONFIGURAÇÕES GERAIS E VARIÁVEIS DE AMBIENTE
# =====================================================================
RECEIVER_WALLET = os.getenv("RECEIVER_WALLET", "0xSuaCarteiraAqui").strip().lower()
SOLANA_RECIPIENT = os.getenv(
    "SOLANA_RECIPIENT", "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk"
).strip()
RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org").strip()
COLLECT_INTERVAL_SECONDS = int(os.getenv("COLLECT_INTERVAL_SECONDS", "20"))
TRACKED_ASSETS = [
    a.strip().upper()
    for a in os.getenv("TRACKED_ASSETS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
    if a.strip()
]

ACCEPTED_TOKENS: dict[str, dict[str, Any]] = {
    "USDC": {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower(),
        "decimals": 6,
        "min_amount": float(os.getenv("PRICE_USDC", "0.01")),
    },
    "LINK": {
        "address": "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196".lower(),
        "decimals": 18,
        "min_amount": float(os.getenv("PRICE_LINK", "0.0005")),
    },
}

# =====================================================================
# 2. ESTADO DO ROBO2 & CONEXÃO ON-CHAIN
# =====================================================================
w3 = Web3(Web3.HTTPProvider(RPC_URL))
TRANSFER_TOPIC = w3.keccak(text="Transfer(address,address,uint256)").hex().lower()
USED_TX_HASHES: set[str] = set()

ROBO2_STATE: dict[str, Any] = {
    "service": "Robo2 Signals",
    "status": "starting",
    "started_at": int(time.time()),
    "last_update": 0,
    "total_paid_requests": 0,
    "revenue_by_token": {"USDC": 0.0, "LINK": 0.0},
    "signals": {},
    "radar_items": [],
    "recent_settlements": [],
}


# =====================================================================
# 3. COLETA EM TEMPO REAL E GERAÇÃO DE SINAIS / RADAR
# =====================================================================
async def fetch_market_snapshot(client: httpx.AsyncClient, symbol: str) -> dict[str, Any] | None:
    clean_symbol = symbol.strip().upper()
    url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={clean_symbol}"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code != 200:
            return None
        raw = resp.json()
        bid = float(raw["bidPrice"])
        ask = float(raw["askPrice"])
        if bid <= 0 or ask <= 0:
            return None
        mid = round((bid + ask) / 2, 6)
        spread_pct = round(((ask - bid) / bid) * 100, 5)
        now_ts = int(time.time())

        return {
            "provider": "Robo2 Signals",
            "asset": clean_symbol,
            "value": mid,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "timestamp": now_ts,
            "status": "live",
        }
    except Exception:
        return None


def rebuild_radar_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for asset, sig in ROBO2_STATE["signals"].items():
        spread = sig.get("spread_pct", 0.0)
        fissure_level = "ALTA" if spread >= 0.01 else "MODERADA" if spread > 0 else "NEUTRA"
        items.append(
            {
                "id": f"sig-{asset.lower()}-{sig['timestamp']}",
                "asset": asset,
                "mid_price": sig["value"],
                "bid": sig["bid"],
                "ask": sig["ask"],
                "spread_pct": spread,
                "fissure": fissure_level,
                "timestamp": sig["timestamp"],
            }
        )
    return items


async def background_collector_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            updated = False
            for asset in TRACKED_ASSETS:
                snap = await fetch_market_snapshot(client, asset)
                if snap:
                    ROBO2_STATE["signals"][snap["asset"]] = snap
                    updated = True
            if updated:
                ROBO2_STATE["radar_items"] = rebuild_radar_items()
                ROBO2_STATE["last_update"] = int(time.time())
            ROBO2_STATE["status"] = "online"
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_collector_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Robo2 Signals & M2M Oracle", version="2.1.0", lifespan=lifespan)


# =====================================================================
# 4. VALIDAÇÃO DE PAGAMENTO ON-CHAIN (USDC + LINK)
# =====================================================================
def verify_onchain_payment(tx_hash: str) -> dict[str, Any]:
    if not tx_hash:
        return {"valid": False, "reason": "Header de pagamento ausente"}

    tx_clean = tx_hash.strip().lower()
    if tx_clean in USED_TX_HASHES:
        return {"valid": False, "reason": "Hash de transação já utilizado"}

    if not RECEIVER_WALLET.startswith("0x") or len(RECEIVER_WALLET) != 42:
        return {"valid": False, "reason": "Carteira recebedora EVM não configurada"}

    try:
        receipt = w3.eth.get_transaction_receipt(tx_clean)
        if not receipt or receipt.status != 1:
            return {"valid": False, "reason": "Transação não confirmada ou revertida"}

        for log in receipt.logs:
            topics = [t.hex().lower() if hasattr(t, "hex") else str(t).lower() for t in log.topics]
            if len(topics) >= 3 and topics[0] == TRANSFER_TOPIC:
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() == RECEIVER_WALLET:
                    contract_addr = log.address.lower()
                    for symbol, info in ACCEPTED_TOKENS.items():
                        if contract_addr == info["address"]:
                            data_hex = log.data.hex() if hasattr(log.data, "hex") else str(log.data)
                            raw_val = int(data_hex, 16)
                            amount = raw_val / (10 ** info["decimals"])
                            if amount >= info["min_amount"]:
                                USED_TX_HASHES.add(tx_clean)
                                ROBO2_STATE["total_paid_requests"] += 1
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
                                return {"valid": True, **settlement}

        return {"valid": False, "reason": "Transferência abaixo do mínimo ou token não aceito"}
    except Exception as exc:
        return {"valid": False, "reason": f"Falha na consulta RPC: {exc}"}


# =====================================================================
# 5. PAINEL EM TEMPO REAL (ESTÉTICA CLARA, BEGE, LILÁS PASTEL E DOURADO)
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
      --line: #e5dac9;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.45;
    }
    .wrap {
      max-width: 1040px;
      margin: 0 auto;
      display: grid;
      gap: 18px;
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
    h1 {
      margin: 0;
      font-size: 1.35rem;
      letter-spacing: 0.2px;
    }
    h2 {
      margin: 0 0 12px 0;
      font-size: 1.02rem;
      color: var(--ink);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 11px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--ok-soft);
      color: var(--ok);
      border: 1px solid #bfe2d1;
    }
    .meta-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }
    .kpi {
      background: var(--card-soft);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 11px 13px;
    }
    .kpi-label {
      font-size: 0.76rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }
    .kpi-val {
      font-size: 1.08rem;
      font-weight: 700;
      margin-top: 3px;
      word-break: break-all;
    }
    .tag-lilac {
      background: var(--lilac-soft);
      color: #5b4a96;
      border: 1px solid #d7cef5;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }
    th, td {
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
    }
    th {
      color: var(--muted);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      background: var(--card-soft);
    }
    code {
      background: var(--gold-soft);
      padding: 2px 6px;
      border-radius: 6px;
      font-size: 0.82rem;
    }
    .footer-note {
      font-size: 0.8rem;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="header-row">
        <div>
          <h1>Robo2 Signals — Monitor em Tempo Real</h1>
          <div class="footer-note" id="clock-info">Sincronizando fluxo ao vivo...</div>
        </div>
        <span class="pill" id="status-badge">ONLINE</span>
      </div>

      <div class="meta-grid">
        <div class="kpi">
          <div class="kpi-label">Requisições Pagas</div>
          <div class="kpi-val" id="paid-count">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Receita USDC</div>
          <div class="kpi-val" id="rev-usdc">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Receita LINK</div>
          <div class="kpi-val" id="rev-link">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Carteira Recebedora</div>
          <div class="kpi-val" id="wallet-addr">-</div>
        </div>
      </div>

      <p class="footer-note" style="margin-top: 12px; margin-bottom: 0;">
        Rotas ativas: <code>/radar/itens</code> · <code>/api/v1/m2m/feed/{asset}</code> · <code>/.well-known/x402</code> · <code>/.well-known/agent.json</code>
      </p>
    </div>

    <div class="card">
      <h2>Sinais e Fissuras em Tempo Real <span class="tag-lilac">LIVE</span></h2>
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
      <h2>Liquidações M2M Recentes</h2>
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
      if (!ts) return "-";
      return new Date(ts * 1000).toLocaleTimeString("pt-BR");
    }

    async function refreshDashboard() {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById("status-badge").textContent = (data.status || "online").toUpperCase();
        document.getElementById("paid-count").textContent = data.total_paid_requests ?? 0;
        document.getElementById("rev-usdc").textContent = data.revenue_by_token?.USDC ?? 0;
        document.getElementById("rev-link").textContent = data.revenue_by_token?.LINK ?? 0;
        document.getElementById("wallet-addr").textContent = data.receiver_wallet || "-";
        document.getElementById("clock-info").textContent =
          "Última leitura: " + fmtTime(data.last_update) + " · Atualização automática a cada 5s";

        const sigs = Object.values(data.signals || {});
        const sigBody = document.getElementById("signals-body");
        if (sigs.length === 0) {
          sigBody.innerHTML = "<tr><td colspan='7'>Aguardando primeiro ciclo de leitura...</td></tr>";
        } else {
          sigBody.innerHTML = sigs.map(s => {
            const fissura = s.spread_pct >= 0.01 ? "ALTA" : (s.spread_pct > 0 ? "MODERADA" : "NEUTRA");
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


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "service": ROBO2_STATE["service"],
        "status": ROBO2_STATE["status"],
        "receiver_wallet": RECEIVER_WALLET,
        "last_update": ROBO2_STATE["last_update"],
        "total_paid_requests": ROBO2_STATE["total_paid_requests"],
        "revenue_by_token": ROBO2_STATE["revenue_by_token"],
        "signals": ROBO2_STATE["signals"],
        "recent_settlements": ROBO2_STATE["recent_settlements"],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": ROBO2_STATE["status"],
        "last_update": ROBO2_STATE["last_update"],
        "tracked_assets": TRACKED_ASSETS,
        "cached_assets": list(ROBO2_STATE["signals"].keys()),
    }


@app.get("/radar/itens")
async def radar_itens(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    items = ROBO2_STATE["radar_items"] or rebuild_radar_items()
    return {
        "service": "Robo2 Signals",
        "count": len(items[:limit]),
        "last_update": ROBO2_STATE["last_update"],
        "items": items[:limit],
    }


@app.get("/.well-known/x402")
@app.get("/.well-known/x402.json")
async def well_known_x402() -> dict[str, Any]:
    return {
        "protocol": "x402",
        "version": "1.0",
        "provider": "Robo2 Signals",
        "description": "Sinais e fissuras de oportunidade em tempo real via M2M.",
        "network": "base-mainnet",
        "receiver": RECEIVER_WALLET,
        "solana_recipient": SOLANA_RECIPIENT,
        "endpoint_template": "/api/v1/m2m/feed/{asset}",
        "radar_endpoint": "/radar/itens",
        "payment_header": "X-Payment-Tx",
        "accepted_tokens": ACCEPTED_TOKENS,
    }


@app.get("/.well-known/agent.json")
async def well_known_agent() -> dict[str, Any]:
    return {
        "name": "Robo2 Signals",
        "description": "Sinais e fissuras de oportunidade em tempo real via M2M.",
        "endpoints": {
            "dashboard": "/",
            "radar": "/radar/itens",
            "m2m_feed": "/api/v1/m2m/feed/{asset}",
            "x402": "/.well-known/x402",
        },
        "payments": {
            "evm_receiver": RECEIVER_WALLET,
            "solana_recipient": SOLANA_RECIPIENT,
            "accepted_tokens": ACCEPTED_TOKENS,
        },
    }


# =====================================================================
# 6. ROTA M2M DE PORTA DUPLA (USDC + LINK VIA COMPROVANTE ON-CHAIN)
# =====================================================================
@app.get("/api/v1/m2m/feed/{asset}")
async def get_m2m_feed(
    asset: str,
    x_payment_tx: str | None = Header(default=None, alias="X-Payment-Tx"),
    x_payment: str | None = Header(default=None, alias="X-PAYMENT"),
) -> JSONResponse:
    proof = x_payment_tx or x_payment

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
                        "min_amount": ACCEPTED_TOKENS["USDC"]["min_amount"],
                    },
                    {
                        "token": "LINK",
                        "contract": ACCEPTED_TOKENS["LINK"]["address"],
                        "min_amount": ACCEPTED_TOKENS["LINK"]["min_amount"],
                    },
                ],
                "instructions": "Envie USDC ou LINK para receiver e inclua o hash no header X-Payment-Tx.",
            },
        )

    payment = verify_onchain_payment(proof)
    if not payment["valid"]:
        return JSONResponse(
            status_code=402,
            content={"error": "Payment Rejected", "detail": payment["reason"]},
        )

    clean_asset = asset.strip().upper()
    signal = ROBO2_STATE["signals"].get(clean_asset)
    if not signal:
        async with httpx.AsyncClient() as client:
            signal = await fetch_market_snapshot(client, clean_asset)

    if not signal:
        return JSONResponse(
            status_code=404,
            content={"error": f"Ativo {clean_asset} indisponível no momento."},
        )

    return JSONResponse(
        status_code=200,
        content={
            **signal,
            "settlement": {
                "paid_with": payment["token"],
                "amount": payment["amount"],
                "tx_hash": payment["tx"],
            },
        },
    )
