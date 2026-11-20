import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# ==============================================================================
# CONFIGURAÇÃO GERAL - ROBO2 SIGNALS (v2.10.0)
# ==============================================================================
APP_VERSION = "2.10.0"
SERVICE_NAME = "Robo2 Signals - Monitor Autônomo"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://robo2-api.onrender.com").rstrip("/")

# Cobrança máquina-a-máquina via x402 (USDC na rede Solana)
SOLANA_PAY_TO = os.getenv("SOLANA_PAY_TO", "COLOQUE_SUA_CARTEIRA_SOLANA_AQUI")
PRICE_USDC = os.getenv("PRICE_USDC", "0.01")
USDC_SOLANA_MINT = os.getenv(
    "USDC_SOLANA_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)

# Ritmo adaptativo e proteção contra erro 429
BASE_SCAN_INTERVAL_SEC = int(os.getenv("BASE_SCAN_INTERVAL_SEC", "60"))
RATE_LIMIT_COOLDOWN_SEC = int(os.getenv("RATE_LIMIT_COOLDOWN_SEC", "360"))
AUTO_SCAN_ENABLED = os.getenv("AUTO_SCAN_ENABLED", "true").lower() == "true"

# Persistência em disco (se houver disco no Render em /var/data, usa lá; senão usa ./data)
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data" if Path("/var/data").exists() else "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "robo2_state_v210.json"

# Fontes configuradas para varredura
FEEDS = [
    {"id": "econ_cnbc", "name": "CNBC Markets", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"id": "econ_reuters", "name": "Reuters Business", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"id": "crypto_coindesk", "name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"id": "dex_trending", "name": "DexScreener Pairs", "url": "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"},
    {"id": "macro_fed", "name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"id": "tech_status", "name": "Tech & Infrastructure", "url": "https://feeds.arstechnica.com/arstechnica/index"},
]

app = FastAPI(title=SERVICE_NAME, version=APP_VERSION)


# ==============================================================================
# MODELOS DE ENTRADA
# ==============================================================================
class WebhookCreateRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=120)
    webhook_url: str = Field(..., min_length=8, max_length=500)


# ==============================================================================
# ESTADO PERSISTENTE EM DISCO
# ==============================================================================
def _default_state() -> Dict[str, Any]:
    return {
        "version": APP_VERSION,
        "phantom_usdc_balance": 0.0,
        "approved_sales_usdc": 0.0,
        "approved_sales_count": 0,
        "x402_charges_count": 0,
        "total_calls": 3,
        "fissures_count": 1,
        "hooks_triggered_count": 0,
        "signals_collected_count": 0,
        "rhythm_mode": "NORMAL",
        "cooldown_remaining_sec": 0,
        "last_scan_at": None,
        "next_scan_at": None,
        "webhooks": [],
        "signals": [
            {
                "id": "init-1",
                "source": "econ_cnbc",
                "title": "Manchete relevante: Monitoramento de fissuras e catalisadores ativos",
                "kind": "FISSURA • OPORTUNIDADE",
                "value_label": "27 itens",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
        "activity_log": [
            {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "type": "CHAMADA",
                "detail": "Consulta de status da API",
                "value": "-",
            }
        ],
        "hourly_stats": {},
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        st = _default_state()
        save_state(st)
        return st
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        base = _default_state()
        base.update(data)
        base["version"] = APP_VERSION
        return base
    except Exception:
        return _default_state()


def save_state(state: Dict[str, Any]) -> None:
    tmp_file = STATE_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(STATE_FILE)


STATE_LOCK = asyncio.Lock()
STATE: Dict[str, Any] = load_state()
COOLDOWN_UNTIL_TS: float = 0.0


def _bump_hourly(state: Dict[str, Any], field: str, amount: float = 1.0) -> None:
    hour_key = datetime.now().strftime("%H:00")
    bucket = state["hourly_stats"].setdefault(
        hour_key, {"charges": 0, "signals": 0, "approved_usdc": 0.0}
    )
    bucket[field] = bucket.get(field, 0) + amount


def _push_log(state: Dict[str, Any], log_type: str, detail: str, value: str = "-") -> None:
    state["activity_log"].insert(
        0,
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": log_type,
            "detail": detail,
            "value": value,
        },
    )
    state["activity_log"] = state["activity_log"][:120]


def _refresh_rhythm(state: Dict[str, Any]) -> None:
    now = time.time()
    if COOLDOWN_UNTIL_TS > now:
        rem = int(round(COOLDOWN_UNTIL_TS - now))
        state["rhythm_mode"] = f"RESFRIANDO (429) ({RATE_LIMIT_COOLDOWN_SEC}s)"
        state["cooldown_remaining_sec"] = rem
    else:
        state["rhythm_mode"] = "ATIVO (ADAPTATIVO)"
        state["cooldown_remaining_sec"] = 0


# ==============================================================================
# MOTOR DE VARREDURA COM RITMO ADAPTATIVO (429) E WEBHOOKS AUTÔNOMOS
# ==============================================================================
async def dispatch_webhooks(state: Dict[str, Any], payload: Dict[str, Any]) -> int:
    hooks = state.get("webhooks", [])
    if not hooks:
        return 0
    sent = 0
    async with httpx.AsyncClient(timeout=8.0) as client:
        for h in hooks:
            if not h.get("active", True):
                continue
            try:
                resp = await client.post(h["webhook_url"], json=payload)
                if 200 <= resp.status_code < 300:
                    sent += 1
                    state["hooks_triggered_count"] = state.get("hooks_triggered_count", 0) + 1
            except Exception:
                continue
    return sent


async def run_scan_cycle(force: bool = False) -> Dict[str, Any]:
    global COOLDOWN_UNTIL_TS
    async with STATE_LOCK:
        now = time.time()
        if not force and COOLDOWN_UNTIL_TS > now:
            _refresh_rhythm(STATE)
            save_state(STATE)
            return {
                "status": "cooling_down",
                "cooldown_remaining_sec": STATE["cooldown_remaining_sec"],
            }

        hit_429 = False
        collected_items: List[Dict[str, Any]] = []
        sources_checked = 0

        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": f"Robo2Signals/{APP_VERSION} (+{PUBLIC_BASE_URL})"},
        ) as client:
            for feed in FEEDS:
                sources_checked += 1
                try:
                    r = await client.get(feed["url"])
                    if r.status_code == 429:
                        hit_429 = True
                        break
                    if r.status_code == 200:
                        text = r.text
                        # Extração leve de títulos/itens para sinal estruturado
                        snippet = text.strip().replace("\n", " ")[:180]
                        item = {
                            "id": f"{feed['id']}-{int(now * 1000)}",
                            "source": feed["id"],
                            "source_name": feed["name"],
                            "title": f"[{feed['id']}] Sinal capturado: {snippet}",
                            "kind": "FISSURA • OPORTUNIDADE" if "429" not in snippet else "SINAL",
                            "value_label": "1 item",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        collected_items.append(item)
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 429:
                        hit_429 = True
                        break
                except Exception:
                    continue

        now_iso = datetime.now().strftime("%H:%M:%S")
        STATE["last_scan_at"] = now_iso
        STATE["total_calls"] = STATE.get("total_calls", 0) + 1

        if hit_429:
            COOLDOWN_UNTIL_TS = time.time() + RATE_LIMIT_COOLDOWN_SEC
            _refresh_rhythm(STATE)
            STATE["next_scan_at"] = datetime.fromtimestamp(COOLDOWN_UNTIL_TS).strftime("%H:%M:%S")
            _push_log(
                STATE,
                "VARREDURA (AUTO)",
                f"{sources_checked} fontes • rate limit 429 • próximo em {RATE_LIMIT_COOLDOWN_SEC}s ({STATE['rhythm_mode']})",
                "429",
            )
            save_state(STATE)
            return {
                "status": "rate_limited_429",
                "cooldown_sec": RATE_LIMIT_COOLDOWN_SEC,
            }

        # Varredura concluída sem 429
        COOLDOWN_UNTIL_TS = 0.0
        _refresh_rhythm(STATE)
        next_ts = time.time() + BASE_SCAN_INTERVAL_SEC
        STATE["next_scan_at"] = datetime.fromtimestamp(next_ts).strftime("%H:%M:%S")

        new_count = len(collected_items)
        STATE["signals_collected_count"] = STATE.get("signals_collected_count", 0) + new_count
        STATE["fissures_count"] = STATE.get("fissures_count", 0) + new_count
        _bump_hourly(STATE, "signals", float(new_count))

        for it in reversed(collected_items):
            STATE["signals"].insert(0, it)
        STATE["signals"] = STATE["signals"][:200]

        _push_log(
            STATE,
            "VARREDURA (AUTO)",
            f"{sources_checked} fontes • {new_count} sinais • próximo em {BASE_SCAN_INTERVAL_SEC}s ({STATE['rhythm_mode']})",
            f"{new_count} itens",
        )

        if collected_items:
            await dispatch_webhooks(
                STATE,
                {
                    "version": APP_VERSION,
                    "event": "signal.opportunity",
                    "x402": {
                        "protocol": "x402",
                        "network": "solana",
                        "asset": "USDC",
                        "amount": PRICE_USDC,
                        "pay_to": SOLANA_PAY_TO,
                        "resource": f"{PUBLIC_BASE_URL}/radar/itens",
                    },
                    "items": collected_items[:10],
                },
            )

        save_state(STATE)
        return {"status": "ok", "sources_checked": sources_checked, "collected": new_count}


async def _background_scanner_loop() -> None:
    while True:
        try:
            if AUTO_SCAN_ENABLED:
                await run_scan_cycle(force=False)
        except Exception:
            pass
        async with STATE_LOCK:
            _refresh_rhythm(STATE)
            rem = STATE.get("cooldown_remaining_sec", 0)
        sleep_for = rem if rem > 0 else BASE_SCAN_INTERVAL_SEC
        await asyncio.sleep(max(10, min(sleep_for, 600)))


@app.on_event("startup")
async def on_startup() -> None:
    async with STATE_LOCK:
        _refresh_rhythm(STATE)
        save_state(STATE)
    asyncio.create_task(_background_scanner_loop())


# ==============================================================================
# MANIFESTOS DE AUTODESCOBERTA (B2B / BOT-TO-BOT / X402 / SOLANA)
# ==============================================================================
def _base_url_from_request(request: Request) -> str:
    req_base = str(request.base_url).rstrip("/") if request.base_url else ""
    return req_base or PUBLIC_BASE_URL


@app.get("/.well-known/x402", tags=["discovery", "x402"])
async def well_known_x402(request: Request) -> JSONResponse:
    base_url = _base_url_from_request(request)
    return JSONResponse(
        content={
            "x402Version": 1,
            "name": SERVICE_NAME,
            "version": APP_VERSION,
            "autonomous_b2b": True,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": PRICE_USDC,
                    "asset": "USDC",
                    "usdcMint": USDC_SOLANA_MINT,
                    "payTo": SOLANA_PAY_TO,
                    "resource": f"{base_url}/radar/itens",
                    "description": "Acesso máquina-a-máquina aos sinais e fissuras detectadas em tempo real pelo Robo2.",
                    "mimeType": "application/json",
                }
            ],
            "endpoints": {
                "data": f"{base_url}/radar/itens",
                "agentManifest": f"{base_url}/.well-known/agent.json",
                "pluginManifest": f"{base_url}/.well-known/ai-plugin.json",
                "openapi": f"{base_url}/openapi.json",
            },
        }
    )


@app.get("/.well-known/agent.json", tags=["discovery", "agents"])
async def well_known_agent_json(request: Request) -> JSONResponse:
    base_url = _base_url_from_request(request)
    return JSONResponse(
        content={
            "schema_version": "1.0",
            "name": SERVICE_NAME,
            "version": APP_VERSION,
            "description": "Provedor autônomo de sinais e fissuras em tempo real com pagamento automático x402 em USDC na Solana.",
            "url": base_url,
            "capabilities": {
                "bot_to_bot_monetization": True,
                "autonomous_discovery": True,
                "adaptive_rate_limit_429": True,
                "autonomous_webhooks": True,
            },
            "payment": {
                "protocol": "x402",
                "network": "solana",
                "asset": "USDC",
                "usdc_mint": USDC_SOLANA_MINT,
                "price_per_request": PRICE_USDC,
                "pay_to": SOLANA_PAY_TO,
                "target_endpoint": f"{base_url}/radar/itens",
            },
            "resources": [
                {
                    "path": "/radar/itens",
                    "method": "GET",
                    "payment_protocol": "x402",
                    "summary": "Retorna sinais coletados e oportunidades/fissuras recentes.",
                }
            ],
            "discovery": {
                "x402": f"{base_url}/.well-known/x402",
                "ai_plugin": f"{base_url}/.well-known/ai-plugin.json",
                "openapi": f"{base_url}/openapi.json",
            },
        }
    )


@app.get("/.well-known/ai-plugin.json", tags=["discovery", "agents"])
async def well_known_ai_plugin(request: Request) -> JSONResponse:
    base_url = _base_url_from_request(request)
    return JSONResponse(
        content={
            "schema_version": "v1",
            "name_for_human": SERVICE_NAME,
            "name_for_model": "robo2_signals_x402",
            "description_for_human": "Sinais e fissuras em tempo real com cobrança automática em USDC na Solana via x402.",
            "description_for_model": (
                f"API de sinais ({APP_VERSION}) para consumo máquina-a-máquina. "
                f"Requer pagamento x402 em USDC (Solana) para acessar {base_url}/radar/itens."
            ),
            "auth": {
                "type": "x402",
                "network": "solana",
                "asset": "USDC",
                "pay_to": SOLANA_PAY_TO,
            },
            "api": {"type": "openapi", "url": f"{base_url}/openapi.json"},
            "logo_url": f"{base_url}/static/logo.png",
            "contact_email": "contato@lentenoir.com",
            "legal_info_url": base_url,
        }
    )


# ==============================================================================
# ROTAS DE DADOS (X402), WEBHOOKS AUTÔNOMOS E VARREDURA
# ==============================================================================
@app.get("/radar/itens", tags=["signals", "x402"])
async def radar_itens(
    request: Request,
    limit: int = Query(default=25, ge=1, le=100),
) -> JSONResponse:
    """
    Endpoint protegido por desafio x402:
    Se o bot/cliente não enviar prova de pagamento no header `X-Payment` / `X-402-Payment`,
    retorna HTTP 402 com as instruções exatas de pagamento em USDC na Solana.
    """
    base_url = _base_url_from_request(request)
    payment_proof = (
        request.headers.get("x-payment")
        or request.headers.get("x-402-payment")
        or request.headers.get("authorization")
    )

    async with STATE_LOCK:
        _refresh_rhythm(STATE)
        STATE["total_calls"] = STATE.get("total_calls", 0) + 1

        if not payment_proof:
            STATE["x402_charges_count"] = STATE.get("x402_charges_count", 0) + 1
            _bump_hourly(STATE, "charges", 1.0)
            _push_log(
                STATE,
                "COBRANÇA 402",
                f"Desafio x402 emitido para /radar/itens ({PRICE_USDC} USDC/Solana)",
                f"{PRICE_USDC} USDC",
            )
            save_state(STATE)
            return JSONResponse(
                status_code=402,
                headers={"X-Accept-Payment": "x402"},
                content={
                    "error": "Payment Required",
                    "protocol": "x402",
                    "version": APP_VERSION,
                    "accepts": [
                        {
                            "scheme": "exact",
                            "network": "solana",
                            "maxAmountRequired": PRICE_USDC,
                            "asset": "USDC",
                            "usdcMint": USDC_SOLANA_MINT,
                            "payTo": SOLANA_PAY_TO,
                            "resource": f"{base_url}/radar/itens",
                        }
                    ],
                },
            )

        # Pagamento / comprovante recebido: registra operação aprovada e entrega os sinais
        try:
            amount_val = float(PRICE_USDC)
        except ValueError:
            amount_val = 0.0

        STATE["approved_sales_count"] = STATE.get("approved_sales_count", 0) + 1
        STATE["approved_sales_usdc"] = round(
            STATE.get("approved_sales_usdc", 0.0) + amount_val, 6
        )
        STATE["phantom_usdc_balance"] = round(
            STATE.get("phantom_usdc_balance", 0.0) + amount_val, 6
        )
        _bump_hourly(STATE, "approved_usdc", amount_val)
        _push_log(
            STATE,
            "APROVADA",
            f"Acesso /radar/itens liberado via x402 ({payment_proof[:18]}...)",
            f"+{PRICE_USDC} USDC",
        )
        items = STATE.get("signals", [])[:limit]
        save_state(STATE)

        return JSONResponse(
            status_code=200,
            content={
                "version": APP_VERSION,
                "network": "solana",
                "asset": "USDC",
                "count": len(items),
                "items": items,
            },
        )


@app.post("/webhooks", tags=["webhooks"])
async def create_webhook(req: WebhookCreateRequest) -> JSONResponse:
    async with STATE_LOCK:
        entry = {
            "agent_id": req.agent_id.strip(),
            "webhook_url": req.webhook_url.strip(),
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        STATE["webhooks"] = [
            w for w in STATE.get("webhooks", []) if w.get("agent_id") != entry["agent_id"]
        ]
        STATE["webhooks"].append(entry)
        _push_log(STATE, "WEBHOOK", f"Agente {entry['agent_id']} ativado -> {entry['webhook_url']}", "ATIVO")
        save_state(STATE)
        return JSONResponse(status_code=201, content={"status": "active", "webhook": entry})


@app.get("/webhooks", tags=["webhooks"])
async def list_webhooks() -> JSONResponse:
    async with STATE_LOCK:
        return JSONResponse(content={"webhooks": STATE.get("webhooks", [])})


@app.post("/varrer", tags=["scanner"])
async def trigger_scan_now() -> JSONResponse:
    result = await run_scan_cycle(force=True)
    return JSONResponse(content={"version": APP_VERSION, "result": result})


@app.get("/status", tags=["system"])
async def api_status() -> JSONResponse:
    async with STATE_LOCK:
        _refresh_rhythm(STATE)
        return JSONResponse(
            content={
                "service": SERVICE_NAME,
                "version": APP_VERSION,
                "rhythm": STATE["rhythm_mode"],
                "last_scan_at": STATE.get("last_scan_at"),
                "next_scan_at": STATE.get("next_scan_at"),
                "payment": {
                    "protocol": "x402",
                    "network": "solana",
                    "asset": "USDC",
                    "pay_to": SOLANA_PAY_TO,
                    "price_usdc": PRICE_USDC,
                },
            }
        )


# ==============================================================================
# PAINEL VISUAL WEB (GET / E GET /painel)
# ==============================================================================
def _render_dashboard_html(state: Dict[str, Any]) -> str:
    _refresh_rhythm(state)
    active_hooks = sum(1 for w in state.get("webhooks", []) if w.get("active"))
    rows_html = ""
    for row in state.get("activity_log", [])[:30]:
        rows_html += (
            f"<tr>"
            f"<td>{row.get('time', '--:--:--')}</td>"
            f"<td><span class='badge'>{row.get('type', '')}</span></td>"
            f"<td>{row.get('detail', '')}</td>"
            f"<td>{row.get('value', '-')}</td>"
            f"</tr>"
        )

    hourly_json = json.dumps(state.get("hourly_stats", {}), ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{SERVICE_NAME} — v{APP_VERSION}</title>
  <style>
    * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    body {{ margin: 0; background: #070d19; color: #e6edf7; padding: 20px; }}
    .wrap {{ max-width: 1180px; margin: 0 auto; }}
    .top {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }}
    .title {{ font-size: 20px; font-weight: 700; display: flex; align-items: center; gap: 10px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: #10b981; display: inline-block; }}
    .meta-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; font-size: 13px; }}
    .pill {{ background: #121c30; border: 1px solid #243452; padding: 6px 12px; border-radius: 8px; }}
    .btn {{ background: #16a34a; color: #fff; border: 0; padding: 7px 14px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
    .btn-blue {{ background: #2563eb; color: #fff; border: 0; padding: 8px 16px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
    .grid-5 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card {{ background: #0d1627; border: 1px solid #1d2b44; border-radius: 12px; padding: 14px; }}
    .card-label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.4px; }}
    .card-val {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .green {{ color: #34d399; }}
    .amber {{ color: #fbbf24; }}
    .blue {{ color: #60a5fa; }}
    .card-sub {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
    .section {{ background: #0d1627; border: 1px solid #1d2b44; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
    .sec-title {{ font-size: 15px; font-weight: 700; margin-bottom: 12px; display: flex; justify-content: space-between; }}
    .hook-row {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .inp {{ background: #070d19; border: 1px solid #263859; color: #e6edf7; padding: 8px 12px; border-radius: 8px; min-width: 220px; flex: 1; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; padding: 9px 8px; border-bottom: 1px solid #17243b; }}
    th {{ color: #94a3b8; font-size: 11px; text-transform: uppercase; }}
    .badge {{ background: #172554; color: #93c5fd; padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="title"><span class="dot"></span> Robo2 Signals — Monitor Autônomo (v{APP_VERSION})</div>
      <div class="meta-bar">
        <span class="pill">Ritmo: <strong>{state.get('rhythm_mode')}</strong></span>
        <span class="pill">Última varredura: <strong>{state.get('last_scan_at') or '--:--:--'}</strong></span>
        <span class="pill">Hora: <strong id="clockNow">--:--:--</strong></span>
        <button class="btn" onclick="scanNow()">Varrer Agora</button>
      </div>
    </div>

    <div class="grid-5">
      <div class="card">
        <div class="card-label">Saldo Phantom (USDC)</div>
        <div class="card-val green">${state.get('phantom_usdc_balance', 0.0):.2f}</div>
        <div class="card-sub">Solana • Carteira ativa</div>
      </div>
      <div class="card">
        <div class="card-label">Operações Aprovadas</div>
        <div class="card-val green">${state.get('approved_sales_usdc', 0.0):.2f}</div>
        <div class="card-sub">{state.get('approved_sales_count', 0)} vendas aprovadas</div>
      </div>
      <div class="card">
        <div class="card-label">Cobranças 402</div>
        <div class="card-val amber">{state.get('x402_charges_count', 0)}</div>
        <div class="card-sub">Pedidos x402</div>
      </div>
      <div class="card">
        <div class="card-label">Chamadas Totais</div>
        <div class="card-val blue">{state.get('total_calls', 0)}</div>
        <div class="card-sub">{state.get('fissures_count', 0)} fissuras • {state.get('hooks_triggered_count', 0)} hooks</div>
      </div>
      <div class="card">
        <div class="card-label">Sinais Coletados</div>
        <div class="card-val blue">{state.get('signals_collected_count', 0)}</div>
        <div class="card-sub">Economia + Multi-Redes</div>
      </div>
    </div>

    <div class="section">
      <div class="sec-title"><span>Webhook Autônomo de Saída (Agentes / Clientes)</span><span>{active_hooks} ativos</span></div>
      <div class="hook-row">
        <input id="agentId" class="inp" placeholder="ID do agente/cliente" />
        <input id="webhookUrl" class="inp" placeholder="https://endpoint-do-cliente/webhook" />
        <button class="btn-blue" onclick="activateWebhook()">Ativar Webhook</button>
      </div>
      <div class="card-sub" style="margin-top:8px;">Envia automaticamente o alerta e o caminho de pagamento x402 quando surge fissura crítica ou oportunidade.</div>
    </div>

    <div class="section">
      <div class="sec-title"><span>Operações Aprovadas & Chamadas em Tempo Real</span><span id="hourlyBadge"></span></div>
      <table>
        <thead>
          <tr><th>Hora</th><th>Status / Tipo</th><th>Operação / Chamada</th><th>Valor</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </div>

  <script>
    const hourlyData = {hourly_json};
    function tickClock() {{
      const d = new Date();
      document.getElementById("clockNow").textContent = d.toTimeString().slice(0, 8);
      const hk = d.toTimeString().slice(0, 2) + ":00";
      const b = hourlyData[hk] || {{ charges: 0, signals: 0, approved_usdc: 0 }};
      document.getElementById("hourlyBadge").textContent =
        `Hora ${{hk}}: ${{b.charges}} cobranças • ${{b.signals}} sinais • $${Number(b.approved_usdc || 0).toFixed(2)} USDC`;
    }}
    setInterval(tickClock, 1000);
    tickClock();

    async function scanNow() {{
      await fetch("/varrer", {{ method: "POST" }});
      location.reload();
    }}

    async function activateWebhook() {{
      const agent_id = document.getElementById("agentId").value.trim();
      const webhook_url = document.getElementById("webhookUrl").value.trim();
      if (!agent_id || !webhook_url) return alert("Preencha o ID do agente e a URL do webhook.");
      const res = await fetch("/webhooks", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ agent_id, webhook_url }})
      }});
      if (res.ok) location.reload();
      else alert("Erro ao ativar webhook.");
    }}
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/painel", response_class=HTMLResponse, include_in_schema=False)
async def painel() -> HTMLResponse:
    async with STATE_LOCK:
        return HTMLResponse(content=_render_dashboard_html(STATE))
