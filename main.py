import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from web3 import Web3
import requests

# =========================================================================
# RENDER LIVE DASHBOARD + KEEPER EXCLUSIVO BASE MAINNET
# =========================================================================
COTACAO_BRL = 5.80
MARGEM_MINIMA_LUCRO_USD = 0.015
TETO_SANIDADE_REWARD_USD = 8.00
COOLDOWN_SEGUNDOS = 3600         # 1 hora de trava por cofre (zero duplicidade)
RPC_BASE = os.getenv("RPC_BASE", "https://mainnet.base.org")
PRECO_ETH_USD = 3200.00
LIMITE_COFRES = 500

ENDERECO_SIMULADOR = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

ABI_ESTRATEGIA = [
    {"inputs": [], "name": "callReward", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastHarvest", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "harvest", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]

# Estado compartilhado entre o motor Web3 e o Painel Visual
painel_dados = {
    "status": "Iniciando varredura na Base...",
    "conexao_base": False,
    "cofres_monitorados": 0,
    "bloco_atual": 0,
    "ciclo_atual": 0,
    "tempo_ultimo_ciclo": 0.0,
    "total_colheitas": 0,
    "lucro_total_usd": 0.0,
    "gas_total_usd": 0.0,
    "ultima_atualizacao": "--:--:--",
    "operacoes": [],  # Últimas 100 operações validadas
    "historico_ts": {}
}
lock_dados = threading.Lock()

HTML_PAINEL = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Base M2M Keeper — Painel ao Vivo</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: #0b0f19; color: #e2e8f0; padding: 20px; }
  .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
  .title { font-size: 1.35rem; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 10px; }
  .badge { background: #064e3b; color: #34d399; padding: 4px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
  .dot { width: 8px; height: 8px; background: #10b981; border-radius: 50%; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  .sub { font-size: 0.85rem; color: #94a3b8; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 18px; }
  .card-label { font-size: 0.78rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
  .card-val { font-size: 1.65rem; font-weight: 700; color: #f8fafc; }
  .card-val.green { color: #34d399; }
  .card-sub { font-size: 0.82rem; color: #64748b; margin-top: 4px; }
  .table-box { background: #111827; border: 1px solid #1f2937; border-radius: 12px; overflow: hidden; }
  .table-head { padding: 14px 18px; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; }
  .table-title { font-weight: 600; font-size: 0.95rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; padding: 12px 16px; background: #0f172a; color: #94a3b8; font-weight: 600; font-size: 0.78rem; text-transform: uppercase; }
  td { padding: 12px 16px; border-top: 1px solid #1e293b; }
  tr:hover td { background: #161f33; }
  .pill-base { background: #1e3a8a; color: #93c5fd; padding: 2px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; }
  .profit { color: #34d399; font-weight: 700; }
  .empty { text-align: center; padding: 36px; color: #64748b; }
  @media (max-width: 640px) { th:nth-child(2), td:nth-child(2), th:nth-child(5), td:nth-child(5) { display: none; } }
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">🔵 Base M2M Keeper <span class="badge"><span class="dot"></span> AO VIVO</span></div>
      <div class="sub" id="status-line">Conectando ao servidor Render...</div>
    </div>
    <div class="sub" id="clock-line">Atualizado: --:--:--</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">Lucro Líquido Acumulado</div>
      <div class="card-val green" id="lucro-usd">US$ 0,00</div>
      <div class="card-sub" id="lucro-brl">R$ 0,00 (já descontando gas)</div>
    </div>
    <div class="card">
      <div class="card-label">Colheitas Validadas</div>
      <div class="card-val" id="total-colheitas">0</div>
      <div class="card-sub" id="gas-total">Gas estimado total: US$ 0,00</div>
    </div>
    <div class="card">
      <div class="card-label">Cofres Ativos na Base</div>
      <div class="card-val" id="cofres-qtd">0</div>
      <div class="card-sub" id="bloco-num">Bloco atual: #0</div>
    </div>
    <div class="card">
      <div class="card-label">Velocidade de Varredura</div>
      <div class="card-val" id="ciclo-tempo">0.0s</div>
      <div class="card-sub" id="ciclo-num">Ciclo #0 • Trava anti-duplicidade 1h</div>
    </div>
  </div>

  <div class="table-box">
    <div class="table-head">
      <span class="table-title">💧 Histórico de Operações Validadas na EVM (Base Mainnet)</span>
      <span class="sub">Atualização automática a cada 5s</span>
    </div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Hora</th>
            <th>Bloco</th>
            <th>Cofre / Estratégia</th>
            <th>Recompensa</th>
            <th>Gas</th>
            <th>Lucro Líquido</th>
          </tr>
        </thead>
        <tbody id="tabela-ops">
          <tr><td colspan="6" class="empty">Aguardando primeira leitura dos cofres da Base...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

<script>
async function atualizarPainel() {
  try {
    const resp = await fetch('/api/status');
    const d = await resp.json();

    document.getElementById('status-line').textContent = d.status;
    document.getElementById('clock-line').textContent = 'Última leitura: ' + d.ultima_atualizacao;
    document.getElementById('lucro-usd').textContent = 'US$ ' + d.lucro_total_usd.toFixed(2);
    document.getElementById('lucro-brl').textContent = 'R$ ' + (d.lucro_total_usd * 5.80).toFixed(2) + ' (líquido estimado)';
    document.getElementById('total-colheitas').textContent = d.total_colheitas;
    document.getElementById('gas-total').textContent = 'Gas total: US$ ' + d.gas_total_usd.toFixed(3);
    document.getElementById('cofres-qtd').textContent = d.cofres_monitorados;
    document.getElementById('bloco-num').textContent = 'Bloco Base: #' + d.bloco_atual;
    document.getElementById('ciclo-tempo').textContent = d.tempo_ultimo_ciclo.toFixed(1) + 's';
    document.getElementById('ciclo-num').textContent = 'Ciclo #' + d.ciclo_atual + ' • Modo Auditoria 24/7';

    const tbody = document.getElementById('tabela-ops');
    if (d.operacoes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">Nenhuma colheita atingiu o mínimo ainda neste período.</td></tr>';
      return;
    }

    tbody.innerHTML = d.operacoes.map(op => `
      <tr>
        <td>${op.hora}</td>
        <td>#${op.bloco}</td>
        <td><span class="pill-base">BASE</span> <strong>${op.cofre}</strong></td>
        <td>US$ ${op.recompensa.toFixed(3)}</td>
        <td>US$ ${op.gas.toFixed(4)}</td>
        <td class="profit">+US$ ${op.liquido.toFixed(3)} <span style="color:#94a3b8;font-weight:400;">(R$ ${op.liquido_brl.toFixed(2)})</span></td>
      </tr>
    `).join('');
  } catch (e) {
    document.getElementById('status-line').textContent = 'Reconectando ao servidor...';
  }
}
atualizarPainel();
setInterval(atualizarPainel, 5000);
</script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with lock_dados:
                payload = {k: v for k, v in painel_dados.items() if k != "historico_ts"}
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAINEL.encode("utf-8"))

    def log_message(self, format, *args):
        pass

def iniciar_servidor_web():
    porta = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), DashboardHandler)
    server.serve_forever()

def carregar_cofres_base():
    lista_est = []
    try:
        resp = requests.get("https://api.beefy.finance/vaults/base", timeout=10)
        if resp.status_code == 200:
            for v in resp.json():
                if v.get("status") == "active" and v.get("strategy"):
                    try:
                        lista_est.append({
                            "nome": v.get("name", v.get("id")),
                            "endereco": Web3.to_checksum_address(v["strategy"])
                        })
                    except Exception:
                        continue
    except Exception:
        pass
    return lista_est[:LIMITE_COFRES]

def executar_laboratorio():
    w3 = Web3(Web3.HTTPProvider(RPC_BASE, request_kwargs={"timeout": 8}))
    estrategias_base = carregar_cofres_base()

    with lock_dados:
        painel_dados["conexao_base"] = w3.is_connected()
        painel_dados["cofres_monitorados"] = len(estrategias_base)
        painel_dados["status"] = f"Monitorando {len(estrategias_base)} cofres ativos na Base Mainnet"

    print(f"🔵 RENDER DASHBOARD ATIVO | Cofres Base: {len(estrategias_base)}", flush=True)
    ciclo = 0

    while True:
        ciclo += 1
        inicio_ciclo = time.time()
        agora_ts = int(inicio_ciclo)

        if ciclo % 60 == 0 or not estrategias_base:
            estrategias_base = carregar_cofres_base()
            with lock_dados:
                painel_dados["cofres_monitorados"] = len(estrategias_base)

        if not w3.is_connected():
            time.sleep(5)
            continue

        try:
            bloco = w3.eth.block_number
            gas_price_wei = w3.eth.gas_price
        except Exception:
            time.sleep(5)
            continue

        with lock_dados:
            painel_dados["ciclo_atual"] = ciclo
            painel_dados["bloco_atual"] = bloco
            painel_dados["status"] = f"Varrendo {len(estrategias_base)} cofres no Bloco #{bloco}..."

        for est in estrategias_base:
            endereco = est["endereco"]
            if agora_ts - painel_dados["historico_ts"].get(endereco, 0) < COOLDOWN_SEGUNDOS:
                continue

            try:
                contrato = w3.eth.contract(address=endereco, abi=ABI_ESTRATEGIA)
                if contrato.functions.paused().call():
                    continue
                if agora_ts - contrato.functions.lastHarvest().call() < 600:
                    continue

                reward_wei = contrato.functions.callReward().call()
                if reward_wei <= 0:
                    continue

                reward_usd = (reward_wei / 1e18) * PRECO_ETH_USD
                if reward_usd > TETO_SANIDADE_REWARD_USD:
                    continue

                gas_estimado = contrato.functions.harvest().estimate_gas({"from": ENDERECO_SIMULADOR})
                custo_gas_usd = ((gas_price_wei * gas_estimado) / 1e18) * PRECO_ETH_USD
                lucro_liquido = reward_usd - custo_gas_usd

                if lucro_liquido >= MARGEM_MINIMA_LUCRO_USD:
                    hora_brt = datetime.now().strftime("%H:%M:%S")
                    nova_op = {
                        "hora": hora_brt,
                        "bloco": bloco,
                        "cofre": est["nome"],
                        "recompensa": round(reward_usd, 4),
                        "gas": round(custo_gas_usd, 4),
                        "liquido": round(lucro_liquido, 4),
                        "liquido_brl": round(lucro_liquido * COTACAO_BRL, 2)
                    }

                    with lock_dados:
                        painel_dados["historico_ts"][endereco] = agora_ts
                        painel_dados["total_colheitas"] += 1
                        painel_dados["lucro_total_usd"] += lucro_liquido
                        painel_dados["gas_total_usd"] += custo_gas_usd
                        painel_dados["operacoes"].insert(0, nova_op)
                        painel_dados["operacoes"] = painel_dados["operacoes"][:100]

                    print(f"💧 [BASE] {est['nome']} -> +US$ {lucro_liquido:.3f} (R$ {lucro_liquido * COTACAO_BRL:.2f})", flush=True)
            except Exception:
                continue

        duracao = time.time() - inicio_ciclo
        with lock_dados:
            painel_dados["tempo_ultimo_ciclo"] = duracao
            painel_dados["ultima_atualizacao"] = datetime.now().strftime("%H:%M:%S")
            painel_dados["status"] = f"Online • Varredura concluída em {duracao:.1f}s (próxima em 8s)"

        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=iniciar_servidor_web, daemon=True).start()
    executar_laboratorio()
