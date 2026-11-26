import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from web3 import Web3
import requests

# =========================================================================
# BASE M2M ARENA — AUDITORIA REAL DE CONCORRÊNCIA (SHADOW MODE + REAL)
# =========================================================================
MODO_REAL = os.getenv("MODO_REAL", "false").lower() == "true"
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

COTACAO_BRL = 5.80
MARGEM_MINIMA_LUCRO_USD = 0.03
TETO_SANIDADE_REWARD_USD = 8.00
COOLDOWN_SEGUNDOS = 3600
TEMPO_JANELA_DISPUTA_SEG = 3.0  # Tempo real de latência + confirmação na Base
RPC_BASE = os.getenv("RPC_BASE", "https://mainnet.base.org")
PRECO_ETH_USD = 3200.00
LIMITE_COFRES = 500

# Configurações do Auto-Refill de Gas (WETH -> ETH na Base)
WETH_BASE_ADDR = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
SALDO_MINIMO_ETH = Web3.to_wei(0.003, 'ether')
MINIMO_WETH_UNWRAP = Web3.to_wei(0.002, 'ether')

ENDERECO_SIMULADOR = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

ABI_ESTRATEGIA = [
    {"inputs": [], "name": "callReward", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastHarvest", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "harvest", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]

ABI_WETH = [
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "wad", "type": "uint256"}], "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]

painel_dados = {
    "status": "Iniciando auditoria real de concorrência...",
    "conexao_base": False,
    "modo_operacao": "REAL (ON-CHAIN)" if MODO_REAL else "AUDITORIA REAL (SHADOW)",
    "cofres_monitorados": 0,
    "bloco_atual": 0,
    "ciclo_atual": 0,
    "tempo_ultimo_ciclo": 0.0,
    "total_tentativas": 0,
    "vitorias": 0,
    "derrotas": 0,
    "lucro_acumulado_usd": 0.0,
    "lucro_acumulado_brl": 0.0,
    "gas_prioritario_total_usd": 0.0,
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
<title>Base M2M Arena — Auditoria Real</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: #0b0f19; color: #e2e8f0; padding: 20px; }
  .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
  .title { font-size: 1.35rem; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 10px; }
  .badge { background: #1e3a8a; color: #93c5fd; padding: 4px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; border: 1px solid #3b82f6; }
  .dot { width: 8px; height: 8px; background: #3b82f6; border-radius: 50%; animation: pulse 1.5s infinite; }
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
      <div class="title">🔵 Base M2M Arena <span class="badge"><span class="dot"></span> <span id="modo-badge">AUDITORIA REAL</span></span></div>
      <div class="sub" id="status-line">Conectando ao motor...</div>
    </div>
    <div class="sub" id="clock-line">Atualizado: --:--:--</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">Janelas Reais Vencidas</div>
      <div class="card-val green" id="vitorias">0</div>
      <div class="card-sub" id="lucro-acumulado">Lucro Real: US$ 0.00 (R$ 0.00)</div>
    </div>
    <div class="card">
      <div class="card-label">Perdidas p/ Concorrentes</div>
      <div class="card-val red" id="derrotas">0</div>
      <div class="card-sub">Outras carteiras colheram antes</div>
    </div>
    <div class="card">
      <div class="card-label">Oportunidades Auditadas</div>
      <div class="card-val" id="total-tentativas">0</div>
      <div class="card-sub" id="gas-prioritario">Gas estimado: US$ 0.00</div>
    </div>
    <div class="card">
      <div class="card-label">Velocidade de Varredura</div>
      <div class="card-val" id="ciclo-tempo">0.0s</div>
      <div class="card-sub" id="bloco-num">Bloco Base: #0</div>
    </div>
  </div>

  <div class="table-box">
    <div class="table-head">
      <span class="table-title">⚔️ Registro Real da Concorrência On-Chain</span>
      <span class="sub">Verificação bloco a bloco sem sorteio</span>
    </div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Hora</th>
            <th>Bloco</th>
            <th>Cofre / Estratégia</th>
            <th>Resultado Real</th>
            <th>Lucro Líquido</th>
            <th>Diagnóstico On-Chain</th>
          </tr>
        </thead>
        <tbody id="tabela-ops">
          <tr><td colspan="6" class="empty">Auditando blocos reais na rede Base...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

<script>
async function atualizarPainel() {
  try {
    const resp = await fetch('/api/status');
    const d = await resp.json();

    document.getElementById('modo-badge').textContent = d.modo_operacao;
    document.getElementById('status-line').textContent = d.status;
    document.getElementById('clock-line').textContent = 'Última leitura: ' + d.ultima_atualizacao;
    document.getElementById('vitorias').textContent = d.vitorias;
    document.getElementById('derrotas').textContent = d.derrotas;
    document.getElementById('total-tentativas').textContent = d.total_tentativas;
    
    document.getElementById('lucro-acumulado').textContent = 'Lucro Real: US$ ' + d.lucro_acumulado_usd.toFixed(2) + ' (R$ ' + d.lucro_acumulado_brl.toFixed(2) + ')';
    document.getElementById('gas-prioritario').textContent = 'Gas estimado: US$ ' + d.gas_prioritario_total_usd.toFixed(2);
    document.getElementById('bloco-num').textContent = 'Bloco Base: #' + d.bloco_atual;
    document.getElementById('ciclo-tempo').textContent = d.tempo_ultimo_ciclo.toFixed(1) + 's';

    const tbody = document.getElementById('tabela-ops');
    if (d.operacoes.length === 0) {
      return;
    }

    tbody.innerHTML = d.operacoes.map(op => `
      <tr>
        <td>${op.hora}</td>
        <td>#${op.bloco}</td>
        <td><span class="pill-base">BASE</span> <strong>${op.cofre}</strong></td>
        <td><span class="status-pill ${op.status === 'VITÓRIA' ? 'status-win' : 'status-lose'}">${op.status}</span></td>
        <td class="profit">${op.status === 'VITÓRIA' ? '+US$ ' + op.liquido.toFixed(3) : 'US$ 0.000'} <span style="color:#94a3b8;font-weight:400;">(${op.status === 'VITÓRIA' ? 'R$ ' + op.liquido_brl.toFixed(2) : 'Perdida'})</span></td>
        <td>${op.mensagem_disputa}</td>
      </tr>
    `).join('');
  } catch (e) {
    document.getElementById('status-line').textContent = 'Reconectando ao motor...';
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

def verificar_e_reabastecer_gas(w3):
    if not MODO_REAL or not PRIVATE_KEY:
        return
    try:
        conta = w3.eth.account.from_key(PRIVATE_KEY)
        saldo_eth = w3.eth.get_balance(conta.address)
        if saldo_eth >= SALDO_MINIMO_ETH:
            return

        weth_contrato = w3.eth.contract(address=WETH_BASE_ADDR, abi=ABI_WETH)
        saldo_weth = weth_contrato.functions.balanceOf(conta.address).call()
        if saldo_weth < MINIMO_WETH_UNWRAP:
            return

        base_fee = w3.eth.gas_price
        priority_fee = Web3.to_wei(0.002, 'gwei')
        tx = weth_contrato.functions.withdraw(saldo_weth).build_transaction({
            "from": conta.address,
            "nonce": w3.eth.get_transaction_count(conta.address),
            "gas": 50000,
            "maxFeePerGas": (base_fee * 2) + priority_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": 8453
        })
        assinada = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(assinada.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
        print(f"⛽ Auto-Refill concluído: {Web3.from_wei(saldo_weth, 'ether'):.5f} WETH convertidos em ETH!", flush=True)
    except Exception as e:
        print(f"⚠️ Falha no Auto-Refill de gas: {e}", flush=True)

def executar_harvest_real(w3, contrato, gas_estimado, base_fee_wei):
    conta = w3.eth.account.from_key(PRIVATE_KEY)
    priority_fee = Web3.to_wei(0.005, 'gwei')
    max_fee = (base_fee_wei * 2) + priority_fee

    tx = contrato.functions.harvest().build_transaction({
        "from": conta.address,
        "nonce": w3.eth.get_transaction_count(conta.address),
        "gas": int(gas_estimado * 1.15),
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "chainId": 8453
    })
    assinada = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(assinada.raw_transaction)
    recibo = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    return recibo.status == 1, tx_hash.hex()

def auditar_janela_real(contrato, last_harvest_antes):
    # Aguarda o tempo real de envio + confirmação no bloco seguinte da Base (~3s)
    time.sleep(TEMPO_JANELA_DISPUTA_SEG)
    try:
        last_harvest_depois = contrato.functions.lastHarvest().call()
        reward_depois = contrato.functions.callReward().call()
        # Se lastHarvest mudou ou a recompensa zerou, outro robô real levou antes!
        if last_harvest_depois > last_harvest_antes or reward_depois == 0:
            return False, "Concorrente real colheu o cofre na janela de 3s"
        return True, "Janela aberta por +3s — execução real teria vencido"
    except Exception:
        return False, "Oscilação de leitura durante confirmação do bloco"

def executar_motor():
    w3 = Web3(Web3.HTTPProvider(RPC_BASE, request_kwargs={"timeout": 8}))
    estrategias_base = carregar_cofres_base()

    executor_addr = ENDERECO_SIMULADOR
    if MODO_REAL and PRIVATE_KEY:
        executor_addr = w3.eth.account.from_key(PRIVATE_KEY).address

    with lock_dados:
        painel_dados["conexao_base"] = w3.is_connected()
        painel_dados["cofres_monitorados"] = len(estrategias_base)
        painel_dados["status"] = "Auditor ativo. Medindo concorrência real..."

    ciclo = 0
    while True:
        ciclo += 1
        inicio_ciclo = time.time()
        agora_ts = int(inicio_ciclo)

        if ciclo % 60 == 0 or not estrategias_base:
            estrategias_base = carregar_cofres_base()
            with lock_dados: painel_dados["cofres_monitorados"] = len(estrategias_base)

        if ciclo % 10 == 0:
            verificar_e_reabastecer_gas(w3)

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
            painel_dados["status"] = f"Auditando disputas reais no Bloco #{bloco}..."

        for est in estrategias_base:
            endereco = est["endereco"]
            if agora_ts - painel_dados["historico_ts"].get(endereco, 0) < COOLDOWN_SEGUNDOS:
                continue

            try:
                contrato = w3.eth.contract(address=endereco, abi=ABI_ESTRATEGIA)
                if contrato.functions.paused().call(): continue
                
                last_harvest_atual = contrato.functions.lastHarvest().call()
                if agora_ts - last_harvest_atual < 600: continue

                reward_wei = contrato.functions.callReward().call()
                if reward_wei <= 0: continue

                reward_usd = (reward_wei / 1e18) * PRECO_ETH_USD
                if reward_usd > TETO_SANIDADE_REWARD_USD: continue

                gas_estimado = contrato.functions.harvest().estimate_gas({"from": executor_addr})
                custo_gas_base_eth = (gas_price_wei * gas_estimado) / 1e18
                
                priority_fee_wei = Web3.to_wei(0.005, 'gwei') * gas_estimado
                custo_gas_total_eth = custo_gas_base_eth + (priority_fee_wei / 1e18)
                custo_gas_prioritario_usd = custo_gas_total_eth * PRECO_ETH_USD
                
                lucro_liquido = reward_usd - custo_gas_prioritario_usd

                if lucro_liquido >= MARGEM_MINIMA_LUCRO_USD:
                    if MODO_REAL and PRIVATE_KEY:
                        venceu, tx_hash = executar_harvest_real(w3, contrato, gas_estimado, gas_price_wei)
                        mensagem = f"TX confirmada: {tx_hash[:12]}..." if venceu else f"TX revertida por concorrente: {tx_hash[:12]}..."
                        custo_gas_efetivo = custo_gas_prioritario_usd if venceu else (custo_gas_prioritario_usd * 0.25)
                    else:
                        venceu, mensagem = auditar_janela_real(contrato, last_harvest_atual)
                        custo_gas_efetivo = custo_gas_prioritario_usd if venceu else (custo_gas_prioritario_usd * 0.25)
                    
                    status_str = "VITÓRIA" if venceu else "DERROTA"

                    with lock_dados:
                        painel_dados["historico_ts"][endereco] = agora_ts
                        painel_dados["total_tentativas"] += 1
                        painel_dados["gas_prioritario_total_usd"] += custo_gas_efetivo
                        
                        if venceu:
                            painel_dados["vitorias"] += 1
                            painel_dados["lucro_acumulado_usd"] += lucro_liquido
                            painel_dados["lucro_acumulado_brl"] = painel_dados["lucro_acumulado_usd"] * COTACAO_BRL
                        else:
                            painel_dados["derrotas"] += 1

                        nova_op = {
                            "hora": datetime.now().strftime("%H:%M:%S"),
                            "bloco": bloco,
                            "cofre": est["nome"],
                            "status": status_str,
                            "liquido": round(lucro_liquido, 4) if venceu else 0.0,
                            "liquido_brl": round(lucro_liquido * COTACAO_BRL, 2) if venceu else 0.0,
                            "mensagem_disputa": mensagem
                        }
                        painel_dados["operacoes"].insert(0, nova_op)
                        painel_dados["operacoes"] = painel_dados["operacoes"][:50]

                    print(f"⚔️ [{status_str}] {est['nome']} | {mensagem}", flush=True)
            except Exception:
                continue

        duracao = time.time() - inicio_ciclo
        with lock_dados:
            painel_dados["tempo_ultimo_ciclo"] = duracao
            painel_dados["ultima_atualizacao"] = datetime.now().strftime("%H:%M:%S")
            painel_dados["status"] = f"Auditoria real ativa • Último ciclo em {duracao:.1f}s"
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=iniciar_servidor_web, daemon=True).start()
    executar_motor()
