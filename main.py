from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from datetime import datetime, timezone, timedelta
from collections import deque
import os
import base64
import urllib.request
import xml.etree.ElementTree as ET
import json

app = FastAPI(
    title="Robo2 Signals API",
    description="API de sinais e manchetes estruturadas em tempo real para agentes de IA (Tecnologia, Economia e Cripto) via x402 em USDC (Solana).",
    version="2.3.0"
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
BRT = timezone(timedelta(hours=-3))

TARGETS = [
    {"id": "tech_theverge", "categoria": "tecnologia", "fonte": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    {"id": "tech_techcrunch", "categoria": "tecnologia", "fonte": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"id": "econ_cnbc", "categoria": "economia", "fonte": "CNBC Top News", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"},
    {"id": "crypto_btc_usd", "categoria": "economia", "fonte": "Coinbase Spot BTC-USD", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"}
]

METRICAS = {
    "iniciado_em_brt": datetime.now(BRT).strftime("%d/%m %H:%M:%S"),
    "total_cobrancas_402": 0,
    "total_vendas_pagas": 0,
    "receita_sessao_usdc": 0.0,
    "total_itens_entregues": 0,
    "historico_horas": {},
    "eventos": deque(maxlen=40)
}


def hora_atual_brt() -> str:
    return datetime.now(BRT).strftime("%H:%M:%S")


def faixa_hora_brt() -> str:
    return datetime.now(BRT).strftime("%H:00")


def registrar_evento(tipo: str, detalhe: str, valor_usdc: float = 0.0, status: str = "info"):
    faixa = faixa_hora_brt()
    if faixa not in METRICAS["historico_horas"]:
        METRICAS["historico_horas"][faixa] = {"hora": faixa, "entradas_usdc": 0.0, "cobrancas": 0, "saidas_itens": 0}
    if status == "entrada":
        METRICAS["historico_horas"][faixa]["entradas_usdc"] = round(METRICAS["historico_horas"][faixa]["entradas_usdc"] + valor_usdc, 4)
    elif status == "402":
        METRICAS["historico_horas"][faixa]["cobrancas"] += 1
    elif status == "saida":
        METRICAS["historico_horas"][faixa]["saidas_itens"] += int(valor_usdc)

    METRICAS["eventos"].appendleft({
        "hora": hora_atual_brt(),
        "tipo": tipo,
        "detalhe": detalhe,
        "valor_usdc": valor_usdc,
        "status": status
    })


def consultar_saldo_solana_usdc() -> dict:
    try:
        rpc_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [PAY_TO_ADDRESS, {"mint": USDC_MINT_SOLANA}, {"encoding": "jsonParsed"}]
        }).encode("utf-8")
        req = urllib.request.Request(SOLANA_RPC_URL, data=rpc_body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            contas = json.loads(resp.read().decode("utf-8")).get("result", {}).get("value", [])
        saldo = sum(
            float(c.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("uiAmount") or 0)
            for c in contas
        )
        return {"ok": True, "saldo_usdc": round(saldo, 4)}
    except Exception:
        return {"ok": False, "saldo_usdc": 0.0}


def validar_pagamento_solana_usdc(assinatura_tx: str) -> tuple[bool, str]:
    assinatura = assinatura_tx.strip()
    if assinatura.startswith("{"):
        try:
            payload = json.loads(assinatura)
            assinatura = (payload.get("signature") or payload.get("txHash") or payload.get("payload", {}).get("signature") or "").strip()
        except Exception:
            return False, "Comprovante JSON inválido."
    if len(assinatura) < 40:
        return False, "Assinatura Solana inválida."

    rpc_body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [assinatura, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
    }).encode("utf-8")

    try:
        req = urllib.request.Request(SOLANA_RPC_URL, data=rpc_body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            resultado = json.loads(resp.read().decode("utf-8")).get("result")
        if not resultado:
            return False, "Transação não confirmada na Solana."
        meta = resultado.get("meta") or {}
        if meta.get("err") is not None:
            return False, "Transação falhou na Solana."

        pre = {b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0) for b in meta.get("preTokenBalances", []) if b.get("mint") == USDC_MINT_SOLANA and b.get("owner") == PAY_TO_ADDRESS}
        post = {b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0) for b in meta.get("postTokenBalances", []) if b.get("mint") == USDC_MINT_SOLANA and b.get("owner") == PAY_TO_ADDRESS}
        recebido = sum(post.get(idx, 0.0) - pre.get(idx, 0.0) for idx in post)
        if recebido + 1e-9 >= PRECO_USDC:
            return True, f"Confirmado {recebido:.4f} USDC ({assinatura[:12]}...)"
        return False, f"Valor insuficiente ({recebido:.4f} USDC)."
    except Exception as e:
        return False, f"Erro Solana: {str(e)}"


def extrair_itens_feed(url: str, limite: int = 5):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.3)", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    if raw.lstrip().startswith("{"):
        data = json.loads(raw).get("data", {})
        return [{"titulo": f"{data.get('base', 'BTC')}-{data.get('currency', 'USD')}: {data.get('amount')}", "link": url, "tipo": "cotacao"}]

    root = ET.fromstring(raw)
    itens = []
    for item in root.findall(".//item")[:limite]:
        titulo = (item.findtext("title") or "").strip()
        if titulo:
            itens.append({"titulo": titulo, "link": (item.findtext("link") or "").strip(), "publicado_em": (item.findtext("pubDate") or "").strip(), "tipo": "manchete"})
    if not itens:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:limite]:
            titulo = (entry.findtext("atom:title", default="", namespaces=ns)).strip()
            link_el = entry.find("atom:link", ns)
            if titulo:
                itens.append({"titulo": titulo, "link": link_el.attrib.get("href", "") if link_el is not None else "", "publicado_em": (entry.findtext("atom:updated", default="", namespaces=ns)).strip(), "tipo": "manchete"})
    return itens


def montar_manifesto_x402(resource_url: str):
    return {
        "x402Version": 1,
        "error": "Payment Required",
        "accepts": [{
            "scheme": "exact", "network": REDE_PAGAMENTO, "asset": USDC_MINT_SOLANA,
            "currency": MOEDA_PAGAMENTO, "payTo": PAY_TO_ADDRESS,
            "maxAmountRequired": str(int(round(PRECO_USDC * 1_000_000))),
            "amountUSDC": f"{PRECO_USDC:.2f}", "resource": resource_url,
            "description": "Sinais estruturados em tempo real de Tecnologia, Economia e BTC-USD.",
            "mimeType": "application/json"
        }]
    }


@app.get("/")
def root():
    return {
        "status": "online", "servico": "Robo2 Signals API", "versao": "2.3.0",
        "moeda": MOEDA_PAGAMENTO, "rede": REDE_PAGAMENTO, "recebedor": PAY_TO_ADDRESS,
        "preco_usdc": f"{PRECO_USDC:.2f}",
        "rotas": {"painel": "/painel", "radar": "/radar", "itens_x402": "/radar/itens", "x402": "/.well-known/x402", "openapi": "/openapi.json"}
    }


@app.get("/api/metricas")
def obter_metricas():
    carteira = consultar_saldo_solana_usdc()
    horas_ordenadas = sorted(METRICAS["historico_horas"].values(), key=lambda x: x["hora"])[-8:]
    return {
        "hora_agora": hora_atual_brt(),
        "iniciado_em": METRICAS["iniciado_em_brt"],
        "carteira": PAY_TO_ADDRESS,
        "saldo_usdc": carteira.get("saldo_usdc", 0.0),
        "receita_sessao_usdc": round(METRICAS["receita_sessao_usdc"], 4),
        "vendas_pagas": METRICAS["total_vendas_pagas"],
        "cobrancas_402": METRICAS["total_cobrancas_402"],
        "itens_entregues": METRICAS["total_itens_entregues"],
        "grafico_horas": horas_ordenadas,
        "eventos": list(METRICAS["eventos"])
    }


@app.get("/painel", response_class=HTMLResponse)
def dashboard_visual():
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Robo2 Painel — Entradas e Saídas</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: #0d1117; color: #e6edf3; padding: 18px; }
    .wrap { max-width: 920px; margin: 0 auto; }
    .top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
    .titulo { font-size: 1.25rem; font-weight: 700; display: flex; align-items: center; gap: 8px; }
    .ponto { width: 9px; height: 9px; border-radius: 50%; background: #2ea043; box-shadow: 0 0 8px #2ea043; }
    .hora-box { background: #161b22; border: 1px solid #30363d; padding: 6px 12px; border-radius: 8px; font-size: 0.85rem; color: #8b949e; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 18px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 14px; }
    .rotulo { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; margin-bottom: 6px; }
    .valor { font-size: 1.55rem; font-weight: 700; }
    .verde { color: #3fb950; }
    .amarelo { color: #d29922; }
    .azul { color: #58a6ff; }
    .sub { font-size: 0.75rem; color: #8b949e; margin-top: 4px; }
    .caixa { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 16px; margin-bottom: 18px; }
    .caixa-topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .caixa-topo h2 { font-size: 0.98rem; font-weight: 600; }
    .legenda { display: flex; gap: 14px; font-size: 0.75rem; color: #8b949e; }
    .legenda span { display: inline-flex; align-items: center; gap: 5px; }
    .quad { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
    .grafico { display: flex; align-items: flex-end; gap: 12px; height: 150px; padding-top: 12px; border-bottom: 1px solid #30363d; }
    .coluna-grupo { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
    .barras { display: flex; align-items: flex-end; gap: 4px; height: 115px; width: 100%; justify-content: center; }
    .barra { width: 14px; border-radius: 4px 4px 0 0; min-height: 3px; transition: height 0.3s ease; }
    .b-402 { background: #d29922; }
    .b-saida { background: #58a6ff; }
    .b-entrada { background: #3fb950; }
    .hora-lbl { font-size: 0.72rem; color: #8b949e; margin-top: 6px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
    th, td { padding: 9px 8px; border-bottom: 1px solid #21262d; text-align: left; }
    th { color: #8b949e; font-size: 0.74rem; text-transform: uppercase; }
    .pill { padding: 2px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }
    .p-entrada { background: rgba(63,185,80,0.15); color: #3fb950; }
    .p-402 { background: rgba(210,153,34,0.15); color: #d29922; }
    .p-saida { background: rgba(88,166,255,0.15); color: #58a6ff; }
    .vazio { text-align: center; color: #8b949e; padding: 20px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="titulo"><span class="ponto"></span> Robo2 Signals — Monitor</div>
      <div class="hora-box" id="hora-topo">Hora: --:--:--</div>
    </div>

    <div class="cards">
      <div class="card">
        <div class="rotulo">Saldo Phantom (USDC)</div>
        <div class="valor verde" id="saldo">$0.00</div>
        <div class="sub">Solana • Carteira ativa</div>
      </div>
      <div class="card">
        <div class="rotulo">Entradas Pagas</div>
        <div class="valor verde" id="entradas">$0.00</div>
        <div class="sub" id="qtd-vendas">0 vendas na sessão</div>
      </div>
      <div class="card">
        <div class="rotulo">Cobranças 402</div>
        <div class="valor amarelo" id="cobrancas">0</div>
        <div class="sub">Pedidos de acesso</div>
      </div>
      <div class="card">
        <div class="rotulo">Saídas (Itens)</div>
        <div class="valor azul" id="saidas">0</div>
        <div class="sub">Dados enviados</div>
      </div>
    </div>

    <div class="caixa">
      <div class="caixa-topo">
        <h2>Movimento por Hora</h2>
        <div class="legenda">
          <span><i class="quad b-402"></i>Cobranças</span>
          <span><i class="quad b-saida"></i>Saídas</span>
          <span><i class="quad b-entrada"></i>Entradas</span>
        </div>
      </div>
      <div class="grafico" id="grafico">
        <div class="vazio" style="width:100%">Sem movimentação por hora ainda.</div>
      </div>
    </div>

    <div class="caixa">
      <div class="caixa-topo">
        <h2>Últimas Movimentações</h2>
        <span class="sub" id="status-sync">Atualiza a cada 5s</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>Hora</th>
            <th>Tipo</th>
            <th>Resumo</th>
            <th>Valor</th>
          </tr>
        </thead>
        <tbody id="lista">
          <tr><td colspan="4" class="vazio">Aguardando chamadas na API...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    async function carregar() {
      try {
        const r = await fetch('/api/metricas');
        const d = await r.json();

        document.getElementById('hora-topo').textContent = 'Hora: ' + d.hora_agora;
        document.getElementById('saldo').textContent = '$' + Number(d.saldo_usdc).toFixed(2);
        document.getElementById('entradas').textContent = '$' + Number(d.receita_sessao_usdc).toFixed(2);
        document.getElementById('qtd-vendas').textContent = d.vendas_pagas + ' vendas na sessão';
        document.getElementById('cobrancas').textContent = d.cobrancas_402;
        document.getElementById('saidas').textContent = d.itens_entregues;
        document.getElementById('status-sync').textContent = 'Sincronizado às ' + d.hora_agora;

        const g = document.getElementById('grafico');
        if (d.grafico_horas && d.grafico_horas.length > 0) {
          const maxVal = Math.max(1, ...d.grafico_horas.map(h => Math.max(h.cobrancas, h.saidas_itens, h.entradas_usdc * 100)));
          g.innerHTML = d.grafico_horas.map(h => {
            const h402 = Math.max(4, Math.round((h.cobrancas / maxVal) * 110));
            const hSai = Math.max(4, Math.round((h.saidas_itens / maxVal) * 110));
            const hEnt = Math.max(4, Math.round(((h.entradas_usdc * 100) / maxVal) * 110));
            return `<div class="coluna-grupo">
              <div class="barras">
                <div class="barra b-402" style="height:${h402}px" title="Cobranças: ${h.cobrancas}"></div>
                <div class="barra b-saida" style="height:${hSai}px" title="Saídas: ${h.saidas_itens}"></div>
                <div class="barra b-entrada" style="height:${hEnt}px" title="Entradas: $${h.entradas_usdc}"></div>
              </div>
              <div class="hora-lbl">${h.hora}</div>
            </div>`;
          }).join('');
        }

        const tb = document.getElementById('lista');
        if (!d.eventos || d.eventos.length === 0) {
          tb.innerHTML = '<tr><td colspan="4" class="vazio">Nenhuma chamada registrada ainda.</td></tr>';
          return;
        }
        tb.innerHTML = d.eventos.map(ev => {
          const cls = ev.status === 'entrada' ? 'p-entrada' : (ev.status === 'saida' ? 'p-saida' : 'p-402');
          const val = ev.valor_usdc > 0 ? '+$' + Number(ev.valor_usdc).toFixed(2) : '—';
          return `<tr>
            <td>${ev.hora}</td>
            <td><span class="pill ${cls}">${ev.tipo}</span></td>
            <td>${ev.detalhe}</td>
            <td>${val}</td>
          </tr>`;
        }).join('');
      } catch (e) {
        document.getElementById('status-sync').textContent = 'Reconectando...';
      }
    }
    carregar();
    setInterval(carregar, 5000);
  </script>
</body>
</html>"""


@app.get("/.well-known/x402")
def well_known_x402(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "version": 1, "provider": "Robo2 Signals API", "network": REDE_PAGAMENTO,
        "currency": MOEDA_PAGAMENTO, "payTo": PAY_TO_ADDRESS,
        "endpoints": [{"path": "/radar/itens", "method": "GET", "priceUSDC": f"{PRECO_USDC:.2f}", "description": "Manchetes Tech/Economia + BTC-USD."}],
        "openapi": f"{base}/openapi.json"
    }


@app.get("/.well-known/ai-plugin.json")
def well_known_ai_plugin(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "schema_version": "v1", "name_for_human": "Robo2 Signals API", "name_for_model": "robo2_signals",
        "description_for_human": "Manchetes Tech/Economia + BTC-USD via x402 USDC.",
        "description_for_model": "Fetches structured tech and economy news plus live BTC-USD via GET /radar/itens with x402 USDC payment on Solana.",
        "auth": {"type": "none"}, "api": {"type": "openapi", "url": f"{base}/openapi.json"}
    }


@app.get("/radar")
def verificar_radar():
    resultados = []
    total_kb = 0
    for alvo in TARGETS:
        try:
            req = urllib.request.Request(alvo["url"], headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.3)"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                conteudo = resp.read()
                total_kb += max(1, len(conteudo) // 1024)
                resultados.append({"id": alvo["id"], "categoria": alvo["categoria"], "status": "online", "bytes": len(conteudo)})
        except Exception as e:
            resultados.append({"id": alvo["id"], "categoria": alvo["categoria"], "status": "erro", "detalhe": str(e)})
    registrar_evento("SAÍDA /radar", f"Checagem de 4 fontes ({total_kb} KB)", total_kb, "saida")
    return {"registrado_em": datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S"), "resultados": resultados}


@app.get("/radar/itens")
def listar_itens_radar(
    request: Request,
    categoria: str | None = Query(default=None),
    limite_por_fonte: int = Query(default=5, ge=1, le=20)
):
    comprovante = request.headers.get("X-PAYMENT") or request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-Payment-Proof")

    if not comprovante:
        METRICAS["total_cobrancas_402"] += 1
        registrar_evento("COBRANÇA 402", f"/radar/itens ({categoria or 'todas'})", 0.0, "402")
        manifesto = montar_manifesto_x402(str(request.url))
        header_b64 = base64.b64encode(json.dumps(manifesto).encode("utf-8")).decode("utf-8")
        return JSONResponse(status_code=402, content=manifesto, headers={"PAYMENT-REQUIRED": header_b64})

    pago_ok, detalhe_tx = validar_pagamento_solana_usdc(comprovante)
    if not pago_ok:
        registrar_evento("RECUSADO 402", detalhe_tx, 0.0, "402")
        manifesto = montar_manifesto_x402(str(request.url))
        manifesto["motivo_recusa"] = detalhe_tx
        return JSONResponse(status_code=402, content=manifesto)

    alvos = [t for t in TARGETS if not categoria or t["categoria"].lower() == categoria.lower()]
    coleta = []
    total_geral = 0

    for alvo in alvos:
        try:
            itens = extrair_itens_feed(alvo["url"], limite=limite_por_fonte)
            total_geral += len(itens)
            coleta.append({"id": alvo["id"], "fonte": alvo["fonte"], "categoria": alvo["categoria"], "status": "online", "total_itens": len(itens), "itens": itens})
        except Exception as e:
            coleta.append({"id": alvo["id"], "fonte": alvo["fonte"], "categoria": alvo["categoria"], "status": "erro", "detalhe": str(e), "total_itens": 0, "itens": []})

    coleta.sort(key=lambda x: x.get("total_itens", 0), reverse=True)
    METRICAS["total_vendas_pagas"] += 1
    METRICAS["receita_sessao_usdc"] += PRECO_USDC
    METRICAS["total_itens_entregues"] += total_geral
    registrar_evento("ENTRADA USDC", f"{total_geral} itens entregues", PRECO_USDC, "entrada")

    return JSONResponse(
        status_code=200,
        headers={"X-PAYMENT-RESPONSE": detalhe_tx},
        content={
            "registrado_em": datetime.now(BRT).isoformat(),
            "pagamento_validado": detalhe_tx,
            "filtro_categoria": categoria or "todas",
            "total_itens_retornados": total_geral,
            "fontes": coleta
        }
    )
