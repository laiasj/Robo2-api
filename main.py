import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from web3 import Web3
import requests

# =========================================================================
# LABORATÓRIO M2M KEEPER EXCLUSIVO BASE - VERSÃO DE TESTE 24/7 NO RENDER
# =========================================================================
COTACAO_BRL = 5.80
MARGEM_MINIMA_LUCRO_USD = 0.015
TETO_SANIDADE_REWARD_USD = 8.00
COOLDOWN_SEGUNDOS = 3600         # 1 hora de trava por cofre (zero duplicidade)
RPC_BASE = os.getenv("RPC_BASE", "https://mainnet.base.org")
PRECO_ETH_USD = 3200.00
LIMITE_COFRES = 500              # Varre 100% dos cofres ativos da Base

ENDERECO_SIMULADOR = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

ABI_ESTRATEGIA = [
    {"inputs": [], "name": "callReward", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastHarvest", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "harvest", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]

# Servidor HTTP leve para manter o Web Service ativo no Render sem erro de porta
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Base M2M Keeper Lab Online")
    def log_message(self, format, *args):
        pass

def iniciar_servidor_health():
    porta = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", porta), HealthHandler)
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

    print("==========================================================================================", flush=True)
    print("🔵 RENDER LAB — KEEPER EXCLUSIVO REDE BASE MAINNET (MODO AUDITORIA 24/7)", flush=True)
    print(f"🔗 Conexão Base: {w3.is_connected()} | Total de Cofres Ativos na Base: {len(estrategias_base)}", flush=True)
    print("==========================================================================================\n", flush=True)

    estado = {"total_colheitas": 0, "lucro_total_usd": 0.0, "historico_ts": {}}
    ciclo = 0

    while True:
        ciclo += 1
        inicio_ciclo = time.time()
        hora = datetime.now().strftime("%H:%M:%S")
        agora_ts = int(inicio_ciclo)

        if ciclo % 60 == 0 or not estrategias_base:
            estrategias_base = carregar_cofres_base()

        if not w3.is_connected():
            time.sleep(5)
            continue

        try:
            bloco = w3.eth.block_number
            gas_price_wei = w3.eth.gas_price
        except Exception:
            time.sleep(5)
            continue

        novas_no_ciclo = 0

        for est in estrategias_base:
            endereco = est["endereco"]
            if agora_ts - estado["historico_ts"].get(endereco, 0) < COOLDOWN_SEGUNDOS:
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
                    estado["historico_ts"][endereco] = agora_ts
                    estado["total_colheitas"] += 1
                    estado["lucro_total_usd"] += lucro_liquido
                    novas_no_ciclo += 1

                    print(f"💧 [BASE] {est['nome'][:28]:<28} | Recomp.: US$ {reward_usd:.3f} | Gas: US$ {custo_gas_usd:.4f} | LÍQUIDO: +US$ {lucro_liquido:.3f} (R$ {lucro_liquido * COTACAO_BRL:.2f})", flush=True)
            except Exception:
                continue

        duracao = time.time() - inicio_ciclo
        print(f"[{hora}] 🔄 Ciclo #{ciclo} concluído em {duracao:.1f}s | Bloco #{bloco} | Novas: {novas_no_ciclo} | Total: {estado['total_colheitas']} (US$ {estado['lucro_total_usd']:.2f} / R$ {estado['lucro_total_usd'] * COTACAO_BRL:.2f})", flush=True)
        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=iniciar_servidor_health, daemon=True).start()
    executar_laboratorio()
