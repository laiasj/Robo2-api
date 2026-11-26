import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from web3 import Web3
import requests

# =========================================================================
# RENDER ARENA COMPETITIVA — EXIBIÇÃO DIRETA EM DÓLAR E REAL
# =========================================================================
COTACAO_BRL = 5.80
MARGEM_MINIMA_LUCRO_USD = 0.015
TETO_SANIDADE_REWARD_USD = 8.00
COOLDOWN_SEGUNDOS = 3600
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

painel_dados = {
    "status": "Iniciando arena definitiva...",
    "conexao_base": False,
    "cofres_monitorados": 0,
    "bloco_atual": 0,
    "ciclo_atual": 0,
    "tempo_ultimo_ciclo": 0.0,
    "total_tentativas": 0,
    "vitorias": 0,
    "derrotas": 0,
    "lucro_estimado_usd": 0.0,
    "gas_total_usd": 0.0,
    "ultima_atualizacao": "--:--:--",
    "operacoes": [],
    "historico_ts": {}
}
lock_dados = threading.Lock()

HTML_PAINEL = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Base M2M Arena — Painel Definitivo</title>
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
  .card-val.red { color: #f87171; }
  .card-sub { font-size: 0.82rem; color: #64748b; margin-top: 4px; }
  .table-box { background: #111827; border: 1px solid #1f2937; border-radius: 12px; overflow: hidden; }
  .table-head { padding: 14px 18px; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; }
  .table-title { font-weight: 600; font-size: 0.95rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; padding: 12px 16px; background: #0f172a; color: #94a3b8; font-weight: 600; font-size: 0.78rem; text-transform: uppercase; }
  td { padding: 12px 16px; border-top: 1px solid #1e293b; }
  tr:hover td { background: #161f33; }
  .pill-base { background: #1e3a8a; color: #93c5fd; padding: 2px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; }
  .status-pill { padding: 4px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: inline-block; }
  .status-win { background: rgba(52, 211, 153, 0.2); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.4); }
  .status-lose { background: rgba(248, 113, 113, 0.2); color: #f87171; border: 1px solid rgba(248, 113, 113, 0.4); }
  .profit { color: #34d399; font-weight: 700; }
  .empty { text-align: center; padding: 36px; color: #64748b; }
  @media (max-width: 640px) { th:nth-child(2), td:nth-child(2), th:nth-child(5), td:nth-child(5) { display: none; } }
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">🔵 Base M2M Arena <span class="badge"><span class="dot"></span> EM PRODUÇÃO</span></div>
      <div class="sub" id="status-line">Conectando ao simulador...</div>
    </div>
    <div class="sub" id="clock-line">Atualizado: --:--:--</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">Batalhas Vencidas (Vitórias)</div>
      <div class="card-val green" id="vitorias">0</div>
      <div class="card-sub" id="lucro-vitorias">Lucro acumulado: US$ 0,00 (R$ 0,00)</div>
    </div>
    <div class="card">
      <div class="card-label">Batalhas Perdidas (Derrotas)</div>
      <div class="card-val red" id="derrotas">0</div>
      <div class="card-sub" id="custo-derrotas">Gas perdido: US$ 0,00 (R$ 0,00)</div>
    </div>
    <div class="card">
      <div class="card-label">Tentativas Totais</div>
      <div class="card-val" id="total-tentativas">0</div>
      <div class="card-sub" id="gas-total">Gas prioritário total: US$ 0,00</div>
    </div>
    <div class="card">
      <div class="card-label">Velocidade de Varredura</div>
      <div class="card-val" id="ciclo-tempo">0.0s</div>
      <div class="card-sub" id="bloco-num">Bloco Base: #0</div>
    </div>
  </div>

  <div class="table-box">
    <div class="table-head">
      <span class="table-title">⚔️ Histórico de Disputas de Bloco em Tempo Real</span>
      <span class="sub">Atualização automática a cada 5s</span>
    </div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Hora</th>
            <th>Bloco</th>
            <th>Cofre / Estratégia</th>
            <th>Resultado</th>
            <th>Lucro Estimado</th>
            <th>Impacto da Disputa</th>
          </tr>
        </thead>
        <tbody id="tabela-ops">
          <tr><td colspan="6" class="empty">Aguardando início da varredura competitiva...</td></tr>
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
    document.getElementById('vitorias').textContent = d.vitorias;
    document.getElementById('derrotas').textContent = d.derrotas;
    document.getElementById('total-tentativas').textContent = d.total_tentativas;
    
    document.getElementById('lucro-vitorias').textContent = 'Lucro acumulado: US$ ' + d.lucro_vitorias_usd.toFixed(2) + ' (R$ ' + d.lucro_vitorias_brl.toFixed(2) + ')';
    document.getElementById('custo-derrotas').textContent = 'Gas perdido: US$ ' + d.custo_derrotas_usd.toFixed(2) + ' (R$ ' + d.custo_derrotas_brl.toFixed(2) + ')';
    document.getElementById('gas-total').textContent = 'Gas total: US$ ' + d.gas_total_usd.toFixed(3);
    document.getElementById('cofres-qtd').textContent = d.cofres_monitorados;
    document.getElementById('bloco-num').textContent = 'Bloco Base: #' + d.bloco_atual;
    document.getElementById('ciclo-tempo').textContent = d.tempo_ultimo_ciclo.toFixed(1) + 's';

    const tbody = document.getElementById('tabela-ops');
    if (d.operacoes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">Nenhuma oportunidade qualificada detectada ainda.</td></tr>';
      return;
    }

    tbody.innerHTML = d.operacoes.map(op => `
      <tr>
        <td>${op.hora}</td>
        <td>#${op.bloco}</td>
        <td><span class="pill-base">BASE</span> <strong>${op.cofre}</strong></td>
        <td><span class="status-pill ${op.status === 'VITÓRIA' ? 'status-win' : 'status-lose'}">${op.status}</span></td>
        <td class="profit">+US$ ${op.liquido.toFixed(3)} <span style="color:#94a3b8;font-weight:400;">(R$ ${op.liquido_brl.toFixed(2)})</span></td>
        <td>${op.mensagem_disputa}</td>
      </tr>
    `).join('');
  } catch (e) {
    document.getElementById('status-line').textContent = 'Reconectando ao simulador...';
  }
}
atualizarPainel();
setInterval(atualizarPainel, 5000);
</script>
</body>
</html>
"""

class ArenaHandler(BaseHTTPRequestHandler):
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
    def log_message(self, format, *args): pass

def iniciar_servidor_web():
    porta = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), ArenaHandler)
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

def executar_simulacao():
    w3 = Web3(Web3.HTTPProvider(RPC_BASE, request_kwargs={"timeout": 8}))
    estrategias_base = carregar_cofres_base()

    with lock_dados:
        painel_dados["conexao_base"] = w3.is_connected()
        painel_dados["cofres_monitorados"] = len(estrategias_base)
        painel_dados["status"] = "Arena pronta. Monitorando cofres..."

    ciclo = 0
    while True:
        ciclo += 1
        inicio_ciclo = time.time()
        agora_ts = int(inicio_ciclo)

        if ciclo % 60 == 0 or not estrategias_base:
            estrategias_base = carregar_cofres_base()
            with lock_dados: painel_dados["cofres_monitorados"] = len(estrategias_base)

        if not w3.is_connected():
            time.sleep(5); continue

        try:
            bloco = w3.eth.block_number
            gas_price_wei = w3.eth.gas_price
        except Exception:
            time.sleep(5); continue

        with lock_dados:
            painel_dados["ciclo_atual"] = ciclo
            painel_dados["bloco_atual"] = bloco
            painel_dados["status"] = f"Analisando disputa no Bloco #{bloco}..."

        for est in estrategias_base:
            endereco = est["endereco"]
            if agora_ts - painel_dados["historico_ts"].get(endereco, 0) < COOLDOWN_SEGUNDOS:
                continue

            try:
                contrato = w3.eth.contract(address=endereco, abi=ABI_ESTRATEGIA)
                if contrato.functions.paused().call(): continue
                if agora_ts - contrato.functions.lastHarvest().call() < 600: continue

                reward_wei = contrato.functions.callReward().call()
                if reward_wei <= 0: continue

                reward_usd = (reward_wei / 1e18) * PRECO_ETH_USD
                if reward_usd > TETO_SANIDADE_REWARD_USD: continue

                gas_estimado = contrato.functions.harvest().estimate_gas({"from": ENDERECO_SIMULADOR})
                custo_gas_base = (gas_price_wei * gas_estimado) / 1e18
                
                priority_fee_wei = Web3.to_wei(0.005, 'gwei') * gas_estimado
                custo_gas_total_eth = custo_gas_base + (priority_fee_wei / 1e18)
                custo_gas_usd = custo_gas_total_eth * PRECO_ETH_USD
                lucro_liquido = reward_usd - custo_gas_usd

                if lucro_liquido >= MARGEM_MINIMA_LUCRO_USD:
                    chance_vitoria = min(95, max(45, int((reward_usd / custo_gas_usd) * 15)))
                    venceu = (datetime.now().microsecond % 100) < chance_vitoria
                    
                    status_str = "VITÓRIA" if venceu else "DERROTA"
                    mensagem = f"Transação priorizada incluída no bloco (Sucesso)" if venceu else f"Outro bot pagou mais caro e venceu o bloco"

                    with lock_dados:
                        painel_dados["historico_ts"][endereco] = agora_ts
                        painel_dados["total_tentativas"] += 1
                        if venceu:
                            painel_dados["vitorias"] += 1
                            painel_dados["lucro_estimado_usd"] += lucro_liquido
                        else:
                            painel_dados["derrotas"] += 1
                        
                        painel_dados["gas_total_usd"] += custo_gas_usd
                        
                        # Calcula totais acumulados para os cards
                        painel_dados["lucro_vitorias_usd"] = painel_dados["lucro_estimado_usd"]
                        painel_dados["lucro_vitorias_brl"] = painel_dados["lucro_vitorias_usd"] * COTACAO_BRL
                        
                        # Estimativa proporcional do gas perdido (considerando taxa base)
                        custo_gas_perdido_usd = custo_gas_base * PRECO_ETH_USD if not venceu else 0
                        painel_dados["custo_derrotas_usd"] += custo_gas_perdido_usd
                        painel_dados["custo_derrotas_brl"] = painel_dados["custo_derrotas_usd"] * COTACAO_BRL

                        nova_op = {
                            "hora": datetime.now().strftime("%H:%M:%S"),
                            "bloco": bloco,
                            "cofre": est["nome"],
                            "status": status_str,
                            "liquido": round(lucro_liquido, 4),
                            "liquido_brl": round(lucro_liquido * COTACAO_BRL, 2),
                            "mensagem_disputa": mensagem
                        }
                        painel_dados["operacoes"].insert(0, nova_op)
                        painel_dados["operacoes"] = painel_dados["operacoes"][:100]

                    print(f"⚔️ [{status_str}] {est['nome']} | Lucro: US$ {lucro_liquido:.3f}", flush=True)
            except Exception:
                continue

        duracao = time.time() - inicio_ciclo
        with lock_dados:
            painel_dados["tempo_ultimo_ciclo"] = duracao
            painel_dados["ultima_atualizacao"] = datetime.now().strftime("%H:%M:%S")
            painel_dados["status"] = f"Competindo ativamente • Último ciclo em {duracao:.1f}s"
        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=iniciar_servidor_web, daemon=True).start()
    executar_simulacao()
