import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from web3 import Web3
import requests

# Dependências opcionais para sincronização com Google Sheets (via Google Drive)
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSHEETS_DISPONIVEL = True
except Exception:
    GSHEETS_DISPONIVEL = False

# =========================================================================
# BASE M2M ARENA — FAIXA BLINDADA (US$ 0.03 A US$ 1.50) + ANTI-MEV
# =========================================================================
MODO_REAL = os.getenv("MODO_REAL", "false").lower() == "true"
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

COTACAO_BRL = 5.80
MARGEM_MINIMA_LUCRO_USD = 0.03
TETO_ATAQUE_LUCRO_USD = 1.50       # Ignora prêmios > US$ 1.50 para evitar guerra MEV e revert
DESCONTO_SLIPPAGE_REAL = 0.85      # Desconta 15% de perda na troca interna do harvest
PERCENTUAL_GORJETA_BASE = 0.15     # 15% até US$ 0.59
PERCENTUAL_GORJETA_ALTA = 0.22     # 22% de US$ 0.60 até US$ 1.50
LIMIAR_GORJETA_ALTA_USD = 0.60
PERCENTUAL_FUNDO_INFRA = 0.30      # 30% -> Infraestrutura, Melhorias, Projetos e Taxas
PERCENTUAL_LUCRO_LIVRE = 0.70      # 70% -> Administração / Lucro Livre
RESERVA_MINIMA_ETH_USD = 10.0      # Reserva de giro de US$ 10 em ETH na carteira do iPad

INTERVALO_ENTRE_OPS_SEG = 10.0     # Evita rajadas consecutivas
BLOCOS_ESPERA_CONFIRMACAO = 2      # Na Base (2s/bloco), 2 blocos = ~4s para confirmação
RPC_BASE = os.getenv("RPC_BASE", "https://mainnet.base.org")
PRECO_ETH_USD = float(os.getenv("PRECO_ETH_USD", "3200.00"))
LIMITE_COFRES = 500

# Google Sheets / Google Drive (opcional, ativado quando variáveis estão preenchidas)
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
GOOGLE_ABA_NOME = os.getenv("GOOGLE_ABA_NOME", "Historico_Base")

# Configurações do Auto-Refill de Gas (WETH -> ETH na Base)
WETH_BASE_ADDR = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
SALDO_MINIMO_ETH = Web3.to_wei(0.003, 'ether')
MINIMO_WETH_UNWRAP = Web3.to_wei(0.002, 'ether')
ENDERECO_SIMULADOR = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

FUSO_BRASILIA = timezone(timedelta(hours=-3))

def hora_brasilia_str(formato="%H:%M:%S"):
    return datetime.now(FUSO_BRASILIA).strftime(formato)

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
    "status": "Iniciando faixa blindada (US$ 0.03 - US$ 1.50)...",
    "conexao_base": False,
    "modo_operacao": "REAL BLINDADO (ON-CHAIN)" if MODO_REAL else "DRY-RUN BLINDADO (ATÉ $1.50)",
    "cofres_monitorados": 0,
    "bloco_atual": 0,
    "ciclo_atual": 0,
    "tempo_ultimo_ciclo": 0.0,
    "total_tentativas": 0,
    "vitorias": 0,
    "derrotas": 0,
    "lucro_acumulado_usd": 0.0,
    "lucro_acumulado_brl": 0.0,
    "fundo_30_usd": 0.0,
    "fundo_30_brl": 0.0,
    "livre_70_usd": 0.0,
    "livre_70_brl": 0.0,
    "gas_prioritario_total_usd": 0.0,
    "ultima_atualizacao": "--:--:--",
    "operacoes": [],
    "cofres_auditados_estado": {}
}
lock_dados = threading.Lock()
lock_sheets = threading.Lock()
_planilha_ws = None

def obter_planilha_sheets():
    global _planilha_ws
    if _planilha_ws is not None:
        return _planilha_ws
    if not GSHEETS_DISPONIVEL or not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        cliente = gspread.authorize(creds)
        planilha = cliente.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = planilha.worksheet(GOOGLE_ABA_NOME)
        except gspread.WorksheetNotFound:
            ws = planilha.add_worksheet(title=GOOGLE_ABA_NOME, rows=2000, cols=14)
        if not ws.row_values(1):
            ws.append_row([
                "Data_Hora_BRT", "Modo", "Bloco", "Cofre", "Status",
                "Lucro_Liquido_USD", "Lucro_Liquido_BRL",
                "Gas_Gorjeta_USD", "Fundo_30_USD", "Fundo_30_BRL",
                "Livre_70_USD", "Livre_70_BRL", "Detalhes"
            ])
        _planilha_ws = ws
        return _planilha_ws
    except Exception as e:
        print(f"⚠️ Aviso Google Sheets: {e}", flush=True)
        return None

def registrar_vitoria_sheets(linha_dados: list):
    ws = obter_planilha_sheets()
    if ws is None:
        return
    with lock_sheets:
        try:
            ws.append_row(linha_dados, value_input_option="USER_ENTERED")
        except Exception as e:
            print(f"⚠️ Falha ao gravar linha no Google Sheets: {e}", flush=True)

HTML_PAINEL = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Base M2M Arena — Faixa Blindada ($0.03 - $1.50)</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: #0b0f19; color: #e2e8f0; padding: 20px; }
  .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
  .title { font-size: 1.35rem; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 10px; }
  .badge { background: #1e3a8a; color: #93c5fd; padding: 4px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; border: 1px solid #3b82f6; }
  .dot { width: 8px; height: 8px; background: #3b82f6; border-radius: 50%; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  .sub { font-size: 0.85rem; color: #94a3b8; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 18px; }
  .card-label { font-size: 0.76rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 8px; }
  .card-val { font-size: 1.65rem; font-weight: 700; color: #f8fafc; margin-bottom: 8px; }
  .card-val.green { color: #34d399; }
  .card-val.amber { color: #fbbf24; }
  .card-val.red { color: #f87171; }
  .card-lines { display: flex; flex-direction: column; gap: 4px; font-size: 0.82rem; color: #cbd5e1; }
  .card-lines span strong { color: #f8fafc; font-weight: 600; }
  .card-sub { font-size: 0.8rem; color: #64748b; margin-top: 6px; }
  .table-box { background: #111827; border: 1px solid #1f2937; border-radius: 12px; overflow: hidden; }
  .table-head { padding: 14px 18px; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
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
      <div class="title">🔵 Base M2M Arena <span class="badge"><span class="dot"></span> <span id="modo-badge">DRY-RUN BLINDADO</span></span></div>
      <div class="sub" id="status-line">Conectando ao motor...</div>
    </div>
    <div class="sub" id="clock-line">Atualizado: --:--:--</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">1. Corridas Vencidas (Até $1.50)</div>
      <div class="card-val green" id="vitorias">0</div>
      <div class="card-lines">
        <span id="linha-liquido">Líquido: <strong>US$ 0.00</strong> • <strong>R$ 0.00</strong></span>
        <span id="linha-livre-70">70% Livre/Admin: <strong>US$ 0.00</strong> • <strong>R$ 0.00</strong></span>
      </div>
    </div>

    <div class="card">
      <div class="card-label">2. Fundo 30% (Infra, Projetos e Taxas)</div>
      <div class="card-val amber" id="fundo-30-destaque">US$ 0.00</div>
      <div class="card-lines">
        <span id="linha-fundo-30-brl">Em Reais: <strong>R$ 0.00</strong></span>
        <span class="card-sub" style="margin-top:0;">Reserva p/ estrutura, nós e taxas</span>
      </div>
    </div>

    <div class="card">
      <div class="card-label">3. Concorrente / Revertido</div>
      <div class="card-val red" id="derrotas">0</div>
      <div class="card-sub">Prêmios > $1.50 ignorados</div>
    </div>

    <div class="card">
      <div class="card-label">4. Alvos na Faixa Blindada</div>
      <div class="card-val" id="total-tentativas">0</div>
      <div class="card-sub" id="gas-prioritario">Gas + Gorjeta (15%/22%): US$ 0.00</div>
    </div>

    <div class="card">
      <div class="card-label">5. Velocidade de Varredura</div>
      <div class="card-val" id="ciclo-tempo">0.0s</div>
      <div class="card-sub" id="bloco-num">Bloco Base: #0</div>
    </div>
  </div>

  <div class="table-box">
    <div class="table-head">
      <span class="table-title">⚔️ Disputa Faixa Blindada (US$ 0.03 – US$ 1.50 | Gorjeta 15% / 22%)</span>
      <span class="sub">70% Livre/Admin • 30% Infra, Projetos e Taxas</span>
    </div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Hora (BRT)</th>
            <th>Bloco Chegada</th>
            <th>Cofre / Estratégia</th>
            <th>Resultado</th>
            <th>Lucro Líquido</th>
            <th>Cronômetro & Gorjeta</th>
          </tr>
        </thead>
        <tbody id="tabela-ops">
          <tr><td colspan="6" class="empty">Buscando oportunidades na faixa US$ 0.03 a US$ 1.50...</td></tr>
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
    document.getElementById('clock-line').textContent = 'Última leitura (BRT): ' + d.ultima_atualizacao;
    document.getElementById('vitorias').textContent = d.vitorias;
    document.getElementById('derrotas').textContent = d.derrotas;
    document.getElementById('total-tentativas').textContent = d.total_tentativas;

    document.getElementById('linha-liquido').innerHTML =
      'Líquido: <strong>US$ ' + d.lucro_acumulado_usd.toFixed(2) + '</strong> • <strong>R$ ' + d.lucro_acumulado_brl.toFixed(2) + '</strong>';
    document.getElementById('linha-livre-70').innerHTML =
      '70% Livre/Admin: <strong>US$ ' + d.livre_70_usd.toFixed(2) + '</strong> • <strong>R$ ' + d.livre_70_brl.toFixed(2) + '</strong>';

    document.getElementById('fundo-30-destaque').textContent = 'US$ ' + d.fundo_30_usd.toFixed(2);
    document.getElementById('linha-fundo-30-brl').innerHTML =
      'Em Reais: <strong>R$ ' + d.fundo_30_brl.toFixed(2) + '</strong>';

    document.getElementById('gas-prioritario').textContent = 'Gas + Gorjeta: US$ ' + d.gas_prioritario_total_usd.toFixed(2);
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
                payload = {k: v for k, v in painel_dados.items() if k != "cofres_auditados_estado"}
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

def obter_percentual_gorjeta(lucro_antes_gorjeta_usd: float) -> tuple[float, int]:
    """15% até US$ 0.59 e 22% de US$ 0.60 até US$ 1.50."""
    if lucro_antes_gorjeta_usd >= LIMIAR_GORJETA_ALTA_USD:
        return PERCENTUAL_GORJETA_ALTA, 22
    return PERCENTUAL_GORJETA_BASE, 15

def calcular_gorjeta_dinamica_wei(lucro_bruto_gas_usd, gas_estimado):
    pct_gorjeta, _ = obter_percentual_gorjeta(lucro_bruto_gas_usd)
    orcamento_tip_usd = max(0.002, lucro_bruto_gas_usd * pct_gorjeta)
    orcamento_tip_eth = orcamento_tip_usd / PRECO_ETH_USD
    tip_per_gas_wei = int((orcamento_tip_eth * 1e18) / max(1, gas_estimado))
    min_wei = Web3.to_wei(0.002, 'gwei')
    max_wei = Web3.to_wei(0.08, 'gwei')
    return max(min_wei, min(max_wei, tip_per_gas_wei))

def verificar_reserva_gas_usd(w3, executor_addr):
    try:
        saldo_eth_wei = w3.eth.get_balance(executor_addr)
        saldo_eth_usd = float(Web3.from_wei(saldo_eth_wei, 'ether')) * PRECO_ETH_USD
        if saldo_eth_usd < RESERVA_MINIMA_ETH_USD:
            print(
                f"⛽ Aviso Reserva ETH: saldo atual ~US$ {saldo_eth_usd:.2f} "
                f"(abaixo da reserva de US$ {RESERVA_MINIMA_ETH_USD:.2f} na carteira do iPad).",
                flush=True
            )
    except Exception:
        pass

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

def executar_harvest_real(w3, contrato, gas_estimado, base_fee_wei, priority_fee_wei):
    conta = w3.eth.account.from_key(PRIVATE_KEY)
    max_fee = (base_fee_wei * 2) + priority_fee_wei

    tx = contrato.functions.harvest().build_transaction({
        "from": conta.address,
        "nonce": w3.eth.get_transaction_count(conta.address),
        "gas": int(gas_estimado * 1.05),
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee_wei,
        "chainId": 8453
    })
    assinada = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(assinada.raw_transaction)
    recibo = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    return recibo.status == 1, tx_hash.hex()

def simular_corrida_boca_cofre(w3, contrato, executor_addr, bloco_chegada, last_harvest_antes, gorjeta_usd, pct_label):
    t0 = time.time()
    try:
        contrato.functions.harvest().call({"from": executor_addr})
    except Exception:
        return False, "Chegou na boca, mas contrato reverteria (sem liquidez)"
    latencia_ms = int((time.time() - t0) * 1000)

    bloco_alvo = bloco_chegada + BLOCOS_ESPERA_CONFIRMACAO
    while True:
        try:
            bloco_agora = w3.eth.block_number
            if bloco_agora >= bloco_alvo:
                break
        except Exception:
            pass
        time.sleep(0.6)

    try:
        last_harvest_depois = contrato.functions.lastHarvest().call()
        reward_depois = contrato.functions.callReward().call()
        if last_harvest_depois > last_harvest_antes or reward_depois == 0:
            return False, f"Concorrente levou antes do Bloco #{bloco_alvo}"
        return True, f"{latencia_ms}ms • Gorjeta {pct_label}% US$ {gorjeta_usd:.3f} • Livre até #{bloco_alvo}"
    except Exception:
        return False, "Oscilação de leitura no fechamento do bloco"

def executar_motor():
    w3 = Web3(Web3.HTTPProvider(RPC_BASE, request_kwargs={"timeout": 8}))
    estrategias_base = carregar_cofres_base()

    executor_addr = ENDERECO_SIMULADOR
    if MODO_REAL and PRIVATE_KEY:
        executor_addr = w3.eth.account.from_key(PRIVATE_KEY).address

    obter_planilha_sheets()

    with lock_dados:
        painel_dados["conexao_base"] = w3.is_connected()
        painel_dados["cofres_monitorados"] = len(estrategias_base)
        painel_dados["status"] = "Motor blindado ativo (alvo: US$ 0.03 a US$ 1.50)..."

    ciclo = 0
    ultima_op_ts = 0.0

    while True:
        ciclo += 1
        inicio_ciclo = time.time()
        agora_ts = int(inicio_ciclo)

        if ciclo % 60 == 0 or not estrategias_base:
            estrategias_base = carregar_cofres_base()
            with lock_dados: painel_dados["cofres_monitorados"] = len(estrategias_base)

        if ciclo % 10 == 0:
            verificar_e_reabastecer_gas(w3)
            if MODO_REAL and PRIVATE_KEY:
                verificar_reserva_gas_usd(w3, executor_addr)

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
            painel_dados["status"] = f"Varrendo faixa blindada no Bloco #{bloco}..."

        for est in estrategias_base:
            if time.time() - ultima_op_ts < INTERVALO_ENTRE_OPS_SEG:
                continue

            endereco = est["endereco"]

            try:
                contrato = w3.eth.contract(address=endereco, abi=ABI_ESTRATEGIA)
                if contrato.functions.paused().call(): continue

                last_harvest_atual = contrato.functions.lastHarvest().call()
                if painel_dados["cofres_auditados_estado"].get(endereco) == last_harvest_atual:
                    continue

                if agora_ts - last_harvest_atual < 600: continue

                reward_wei = contrato.functions.callReward().call()
                if reward_wei <= 0: continue

                reward_usd_real = ((reward_wei / 1e18) * PRECO_ETH_USD) * DESCONTO_SLIPPAGE_REAL

                if reward_usd_real > (TETO_ATAQUE_LUCRO_USD * 1.15):
                    continue

                gas_estimado = contrato.functions.harvest().estimate_gas({"from": executor_addr})
                custo_gas_base_usd = ((gas_price_wei * gas_estimado) / 1e18) * PRECO_ETH_USD
                lucro_antes_gorjeta = reward_usd_real - custo_gas_base_usd

                if lucro_antes_gorjeta < MARGEM_MINIMA_LUCRO_USD or lucro_antes_gorjeta > TETO_ATAQUE_LUCRO_USD:
                    continue

                _, pct_label = obter_percentual_gorjeta(lucro_antes_gorjeta)
                priority_fee_wei = calcular_gorjeta_dinamica_wei(lucro_antes_gorjeta, gas_estimado)
                gorjeta_usd = ((priority_fee_wei * gas_estimado) / 1e18) * PRECO_ETH_USD
                custo_gas_total_usd = custo_gas_base_usd + gorjeta_usd
                lucro_liquido = reward_usd_real - custo_gas_total_usd

                if lucro_liquido >= MARGEM_MINIMA_LUCRO_USD:
                    if MODO_REAL and PRIVATE_KEY:
                        venceu, tx_hash = executar_harvest_real(w3, contrato, gas_estimado, gas_price_wei, priority_fee_wei)
                        mensagem = f"TX confirmada: {tx_hash[:12]}... (Tip {pct_label}%: US$ {gorjeta_usd:.3f})" if venceu else f"TX revertida: {tx_hash[:12]}..."
                        custo_gas_efetivo = custo_gas_total_usd if venceu else (custo_gas_total_usd * 0.20)
                    else:
                        venceu, mensagem = simular_corrida_boca_cofre(w3, contrato, executor_addr, bloco, last_harvest_atual, gorjeta_usd, pct_label)
                        custo_gas_efetivo = custo_gas_total_usd if venceu else (custo_gas_total_usd * 0.20)

                    status_str = "VITÓRIA" if venceu else "DERROTA"
                    ultima_op_ts = time.time()
                    hora_brt = hora_brasilia_str("%H:%M:%S")
                    data_hora_brt = hora_brasilia_str("%Y-%m-%d %H:%M:%S")

                    fundo_30_op = round(lucro_liquido * PERCENTUAL_FUNDO_INFRA, 4) if venceu else 0.0
                    livre_70_op = round(lucro_liquido * PERCENTUAL_LUCRO_LIVRE, 4) if venceu else 0.0

                    with lock_dados:
                        painel_dados["cofres_auditados_estado"][endereco] = last_harvest_atual
                        painel_dados["total_tentativas"] += 1
                        painel_dados["gas_prioritario_total_usd"] += custo_gas_efetivo

                        if venceu:
                            painel_dados["vitorias"] += 1
                            painel_dados["lucro_acumulado_usd"] += lucro_liquido
                            painel_dados["lucro_acumulado_brl"] = painel_dados["lucro_acumulado_usd"] * COTACAO_BRL
                            painel_dados["fundo_30_usd"] = painel_dados["lucro_acumulado_usd"] * PERCENTUAL_FUNDO_INFRA
                            painel_dados["fundo_30_brl"] = painel_dados["fundo_30_usd"] * COTACAO_BRL
                            painel_dados["livre_70_usd"] = painel_dados["lucro_acumulado_usd"] * PERCENTUAL_LUCRO_LIVRE
                            painel_dados["livre_70_brl"] = painel_dados["livre_70_usd"] * COTACAO_BRL
                        else:
                            painel_dados["derrotas"] += 1

                        nova_op = {
                            "hora": hora_brt,
                            "bloco": bloco,
                            "cofre": est["nome"],
                            "status": status_str,
                            "liquido": round(lucro_liquido, 4) if venceu else 0.0,
                            "liquido_brl": round(lucro_liquido * COTACAO_BRL, 2) if venceu else 0.0,
                            "mensagem_disputa": mensagem
                        }
                        painel_dados["operacoes"].insert(0, nova_op)
                        painel_dados["operacoes"] = painel_dados["operacoes"][:50]

                    if venceu:
                        registrar_vitoria_sheets([
                            data_hora_brt,
                            "REAL" if MODO_REAL else "DRY-RUN",
                            bloco,
                            est["nome"],
                            status_str,
                            round(lucro_liquido, 4),
                            round(lucro_liquido * COTACAO_BRL, 2),
                            round(custo_gas_efetivo, 4),
                            fundo_30_op,
                            round(fundo_30_op * COTACAO_BRL, 2),
                            livre_70_op,
                            round(livre_70_op * COTACAO_BRL, 2),
                            mensagem
                        ])

                    print(f"⚔️ [{status_str}] {est['nome']} | {mensagem}", flush=True)
            except Exception:
                continue

        duracao = time.time() - inicio_ciclo
        with lock_dados:
            painel_dados["tempo_ultimo_ciclo"] = duracao
            painel_dados["ultima_atualizacao"] = hora_brasilia_str("%H:%M:%S")
            painel_dados["status"] = f"Faixa blindada ativa ($0.03-$1.50) • Ciclo em {duracao:.1f}s"
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=iniciar_servidor_web, daemon=True).start()
    executar_motor()
