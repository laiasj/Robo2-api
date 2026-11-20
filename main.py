from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from collections import deque
import asyncio
import threading
import os
import time
import base64
import urllib.request
import xml.etree.ElementTree as ET
import json

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS", "AmghkwYE3bruzjnxdZ3fGRgpmqHx1EDbWVU2vuMnqNAk")
PRECO_USDC = float(os.getenv("PRECO_USDC", "0.01"))
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
INTERVALO_BASE_SEG = int(os.getenv("INTERVALO_BASE_SEG", "240"))        # 4 min base adaptativa
INTERVALO_MIN_SEG = int(os.getenv("INTERVALO_MIN_SEG", "90"))           # 1.5 min em momento quente
INTERVALO_MAX_SEG = int(os.getenv("INTERVALO_MAX_SEG", "600"))          # 10 min em momento calmo
COOLDOWN_WEBHOOK_SEG = int(os.getenv("COOLDOWN_WEBHOOK_SEG", "240"))     # 4 min por código de fissura
MAX_FALHAS_WEBHOOK = int(os.getenv("MAX_FALHAS_WEBHOOK", "5"))           # Pausa após 5 falhas seguidas
WEBHOOKS_ARQUIVO = os.getenv("WEBHOOKS_ARQUIVO", "webhooks.json")
WEBHOOKS_PADRAO = [u.strip() for u in os.getenv("WEBHOOKS_PADRAO", "").split(",") if u.strip()]
REDE_PAGAMENTO = "solana"
MOEDA_PAGAMENTO = "USDC"
BRT = timezone(timedelta(hours=-3))

TARGETS = [
    {"id": "tech_theverge", "categoria": "tecnologia", "fonte": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    {"id": "tech_techcrunch", "categoria": "tecnologia", "fonte": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"id": "econ_cnbc", "categoria": "economia", "fonte": "CNBC Top News", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"},
    {"id": "crypto_btc_usd", "categoria": "economia", "fonte": "Coinbase Spot BTC-USD", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"},
    {"id": "defillama_lote", "categoria": "cripto", "fonte": "DefiLlama Multi-Redes", "url": "descentralizado://defillama"},
    {"id": "dexscreener_redes", "categoria": "cripto", "fonte": "DexScreener DEX Pulse", "url": "descentralizado://dexscreener"}
]

COINS_DEFILLAMA = (
    "coingecko:bitcoin,coingecko:ethereum,coingecko:solana,coingecko:usd-coin,"
    "coingecko:arbitrum,coingecko:sui,coingecko:avalanche-2,coingecko:chainlink"
)
QUERIES_DEXSCREENER = ["solana", "ethereum"]
PALAVRAS_CHAVE_SINAL = ["sec", "etf", "fed", "hack", "outage", "solana", "bitcoin", "openai", "crash", "rate", "inflation"]

METRICAS = {
    "iniciado_em_brt": datetime.now(BRT).strftime("%d/%m %H:%M:%S"),
    "total_cobrancas_402": 0,
    "total_vendas_pagas": 0,
    "receita_sessao_usdc": 0.0,
    "total_itens_entregues": 0,
    "total_chamadas": 0,
    "total_webhooks_enviados": 0,
    "historico_horas": {},
    "eventos": deque(maxlen=120)
}

ESTADO_VIGILANCIA = {
    "ultima_varredura_brt": "Ainda não executada",
    "proximo_intervalo_seg": INTERVALO_BASE_SEG,
    "estrategia_ritmo": "EQUILIBRADO",
    "ultimo_btc_coinbase": None,
    "ultimo_btc_llama": None,
    "falhas_consecutivas_fonte": {t["id"]: 0 for t in TARGETS},
    "fonte_pausada_ate": {t["id"]: 0.0 for t in TARGETS},
    "rpc_pausado_ate": 0.0,
    "ultimos_titulos_vistos": deque(maxlen=80),
    "fissuras_ativas": deque(maxlen=40),
    "cache_coleta": {},
    "webhooks_inscritos": [],
    "ultimo_envio_por_codigo": {}
}

LOCK_WEBHOOKS = threading.Lock()


class WebhookCadastro(BaseModel):
    url: str
    gravidades: list[str] = ["critico", "oportunidade"]
    cliente_id: str | None = None
    carteira_solana: str | None = None


def hora_atual_brt() -> str:
    return datetime.now(BRT).strftime("%H:%M:%S")


def faixa_hora_brt() -> str:
    return datetime.now(BRT).strftime("%H:00")


def salvar_webhooks_disco():
    with LOCK_WEBHOOKS:
        try:
            with open(WEBHOOKS_ARQUIVO, "w", encoding="utf-8") as f:
                json.dump(ESTADO_VIGILANCIA["webhooks_inscritos"], f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def carregar_webhooks_disco():
    lista = []
    if os.path.exists(WEBHOOKS_ARQUIVO):
        try:
            with open(WEBHOOKS_ARQUIVO, "r", encoding="utf-8") as f:
                dados = json.load(f)
                if isinstance(dados, list):
                    for item in dados:
                        item.setdefault("ativo", True)
                        item.setdefault("envios_ok", 0)
                        item.setdefault("falhas_seguidas", 0)
                        item.setdefault("ultimo_envio_brt", None)
                    lista = dados
        except Exception:
            lista = []

    urls_existentes = {w.get("url") for w in lista}
    for u in WEBHOOKS_PADRAO:
        if u not in urls_existentes:
            lista.append({
                "url": u,
                "gravidades": ["critico", "oportunidade"],
                "cliente_id": "agente_padrao",
                "carteira_solana": None,
                "ativo": True,
                "envios_ok": 0,
                "falhas_seguidas": 0,
                "ultimo_envio_brt": None,
                "criado_em": hora_atual_brt()
            })
            urls_existentes.add(u)

    ESTADO_VIGILANCIA["webhooks_inscritos"] = lista


def registrar_evento(tipo: str, detalhe: str, valor_usdc: float = 0.0, status: str = "chamada"):
    faixa = faixa_hora_brt()
    if faixa not in METRICAS["historico_horas"]:
        METRICAS["historico_horas"][faixa] = {"hora": faixa, "entradas_usdc": 0.0, "cobrancas": 0, "saidas_itens": 0}

    METRICAS["total_chamadas"] += 1

    if status in ("aprovada", "entrada"):
        METRICAS["historico_horas"][faixa]["entradas_usdc"] = round(
            METRICAS["historico_horas"][faixa]["entradas_usdc"] + valor_usdc, 4
        )
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


def _enviar_webhook_com_retentativa(hook: dict, codigo: str, payload_bytes: bytes):
    url_hook = hook["url"]
    cliente_rotulo = hook.get("cliente_id") or "agente"
    ultimo_erro = ""

    for tentativa in range(2):
        try:
            req = urllib.request.Request(
                url_hook,
                data=payload_bytes,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Robo2-Cliente": cliente_rotulo,
                    "X-Robo2-Evento": codigo,
                    "User-Agent": "Robo2-Autonomous-Webhook/2.9"
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                with LOCK_WEBHOOKS:
                    hook["envios_ok"] = hook.get("envios_ok", 0) + 1
                    hook["falhas_seguidas"] = 0
                    hook["ativo"] = True
                    hook["ultimo_envio_brt"] = hora_atual_brt()
                salvar_webhooks_disco()
                METRICAS["total_webhooks_enviados"] += 1
                registrar_evento(
                    "WEBHOOK ENVIADO",
                    f"[{cliente_rotulo} • {codigo}] -> {url_hook} (HTTP {resp.status})",
                    0.0,
                    "chamada"
                )
                return
        except Exception as e:
            ultimo_erro = str(e)[:60]
            if tentativa == 0:
                time.sleep(1.5)

    pausado_agora = False
    with LOCK_WEBHOOKS:
        hook["falhas_seguidas"] = hook.get("falhas_seguidas", 0) + 1
        if hook["falhas_seguidas"] >= MAX_FALHAS_WEBHOOK:
            hook["ativo"] = False
            pausado_agora = True
    salvar_webhooks_disco()

    aviso_pausa = " • Assinante pausado após falhas seguidas" if pausado_agora else ""
    registrar_evento(
        "WEBHOOK FALHOU",
        f"[{cliente_rotulo} • {codigo}] -> {url_hook}: {ultimo_erro}{aviso_pausa}",
        0.0,
        "erro"
    )


def despachar_webhooks_fissura(evento_fissura: dict):
    gravidade = evento_fissura.get("gravidade", "")
    codigo = evento_fissura.get("codigo", "")

    if gravidade not in ("critico", "oportunidade"):
        return

    agora = time.time()
    ultimo_ts = ESTADO_VIGILANCIA["ultimo_envio_por_codigo"].get(codigo, 0)
    if agora - ultimo_ts < COOLDOWN_WEBHOOK_SEG:
        return

    with LOCK_WEBHOOKS:
        alvos_hook = [
            w for w in ESTADO_VIGILANCIA["webhooks_inscritos"]
            if w.get("ativo", True) and ("todas" in w.get("gravidades", []) or gravidade in w.get("gravidades", []))
        ]

    if not alvos_hook:
        return

    ESTADO_VIGILANCIA["ultimo_envio_por_codigo"][codigo] = agora
    payload_bytes = json.dumps({
        "servico": "Robo2 Signals API",
        "versao": "2.9.0",
        "tipo_mensagem": "alerta_fissura_autonomo",
        "emitido_em": datetime.now(BRT).isoformat(),
        "pagamento_x402": {
            "endpoint_completo": "/radar/itens",
            "protocolo": "x402",
            "moeda": MOEDA_PAGAMENTO,
            "rede": REDE_PAGAMENTO,
            "preco_usdc": PRECO_USDC,
            "payTo": PAY_TO_ADDRESS
        },
        "evento": evento_fissura
    }).encode("utf-8")

    for hook in alvos_hook:
        t = threading.Thread(
            target=_enviar_webhook_com_retentativa,
            args=(hook, codigo, payload_bytes),
            daemon=True
        )
        t.start()


def registrar_fissura(codigo: str, gravidade: str, origem: str, detalhe: str, sugestao_acao: str):
    evento_fissura = {
        "hora": hora_atual_brt(),
        "codigo": codigo,
        "gravidade": gravidade,
        "origem": origem,
        "detalhe": detalhe,
        "sugestao_acao": sugestao_acao
    }
    ESTADO_VIGILANCIA["fissuras_ativas"].appendleft(evento_fissura)
    status_painel = "erro" if gravidade == "critico" else "chamada"
    registrar_evento(f"FISSURA • {gravidade.upper()}", f"[{origem}] {detalhe}", 0.0, status_painel)
    despachar_webhooks_fissura(evento_fissura)
    return evento_fissura


def consultar_saldo_solana_usdc(forcar: bool = False) -> dict:
    agora = time.time()
    if not forcar and agora < ESTADO_VIGILANCIA.get("rpc_pausado_ate", 0.0):
        return {"ok": True, "saldo_usdc": 0.0, "em_resfriamento": True}

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
        ESTADO_VIGILANCIA["rpc_pausado_ate"] = 0.0
        return {"ok": True, "saldo_usdc": round(saldo, 4)}
    except Exception:
        # Esfria consulta de saldo por 5 min para não ficar repetindo erro de RPC a toda hora
        ESTADO_VIGILANCIA["rpc_pausado_ate"] = agora + 300
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

        pre = {
            b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in meta.get("preTokenBalances", [])
            if b.get("mint") == USDC_MINT_SOLANA and b.get("owner") == PAY_TO_ADDRESS
        }
        post = {
            b["accountIndex"]: float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in meta.get("postTokenBalances", [])
            if b.get("mint") == USDC_MINT_SOLANA and b.get("owner") == PAY_TO_ADDRESS
        }
        recebido = sum(post.get(idx, 0.0) - pre.get(idx, 0.0) for idx in post)
        if recebido + 1e-9 >= PRECO_USDC:
            return True, f"Confirmado {recebido:.4f} USDC ({assinatura[:12]}...)"
        return False, f"Valor insuficiente ({recebido:.4f} USDC)."
    except Exception as e:
        return False, f"Erro Solana: {str(e)}"


def extrair_defillama(limite: int = 10):
    url_llama = f"https://coins.llama.fi/prices/current/{COINS_DEFILLAMA}"
    req = urllib.request.Request(url_llama, headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.9)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        coins = json.loads(resp.read().decode("utf-8")).get("coins", {})

    itens = []
    for chave, info in coins.items():
        simbolo = (info.get("symbol") or chave.split(":")[-1]).upper()
        preco = info.get("price")
        confianca = info.get("confidence", 0)
        if preco is not None:
            if simbolo == "BTC":
                ESTADO_VIGILANCIA["ultimo_btc_llama"] = float(preco)
            itens.append({
                "titulo": f"{simbolo}-USD (DefiLlama): ${preco}",
                "preco": float(preco),
                "simbolo": simbolo,
                "confianca": confianca,
                "link": "https://defillama.com",
                "tipo": "cotacao_onchain"
            })
    return itens[:limite]


def extrair_dexscreener(limite: int = 8):
    itens = []
    for idx, termo in enumerate(QUERIES_DEXSCREENER):
        if idx > 0:
            time.sleep(1.2)
        url_dex = f"https://api.dexscreener.com/latest/dex/search?q={termo}"
        req = urllib.request.Request(url_dex, headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.9)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            pares = json.loads(resp.read().decode("utf-8")).get("pairs", [])[:2]

        for p in pares:
            base_sym = (p.get("baseToken") or {}).get("symbol", "?")
            quote_sym = (p.get("quoteToken") or {}).get("symbol", "?")
            chain = p.get("chainId", "dex")
            preco_usd = p.get("priceUsd", "?")
            var_24h = (p.get("priceChange") or {}).get("h24", 0)
            liq_usd = (p.get("liquidity") or {}).get("usd", 0)
            itens.append({
                "titulo": f"[{chain.upper()}] {base_sym}/{quote_sym}: ${preco_usd} (24h: {var_24h}% | Liq: ${int(liq_usd):,})",
                "chain": chain,
                "var_24h": float(var_24h or 0),
                "link": p.get("url", "https://dexscreener.com"),
                "tipo": "par_dex"
            })
    return itens[:limite]


def extrair_itens_feed(url: str, limite: int = 5):
    if url == "descentralizado://defillama":
        return extrair_defillama(limite=max(limite, 8))
    if url == "descentralizado://dexscreener":
        return extrair_dexscreener(limite=max(limite, 6))

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.9)", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    if raw.lstrip().startswith("{"):
        data = json.loads(raw).get("data", {})
        valor = data.get("amount")
        return [{
            "titulo": f"{data.get('base', 'BTC')}-{data.get('currency', 'USD')} (Coinbase): ${valor}",
            "preco": float(valor) if valor else None,
            "link": url,
            "tipo": "cotacao_spot"
        }]

    root = ET.fromstring(raw)
    itens = []
    for item in root.findall(".//item")[:limite]:
        titulo = (item.findtext("title") or "").strip()
        if titulo:
            itens.append({
                "titulo": titulo,
                "link": (item.findtext("link") or "").strip(),
                "publicado_em": (item.findtext("pubDate") or "").strip(),
                "tipo": "manchete"
            })
    if not itens:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:limite]:
            titulo = (entry.findtext("atom:title", default="", namespaces=ns)).strip()
            link_el = entry.find("atom:link", ns)
            if titulo:
                itens.append({
                    "titulo": titulo,
                    "link": link_el.attrib.get("href", "") if link_el is not None else "",
                    "publicado_em": (entry.findtext("atom:updated", default="", namespaces=ns)).strip(),
                    "tipo": "manchete"
                })
    return itens


def _calcular_proximo_intervalo(teve_oportunidade: bool, teve_bloqueio_429: bool) -> tuple[int, str]:
    """Decide autonomamente quando será a próxima varredura com base no mercado e em bloqueios."""
    hora = datetime.now(BRT).hour
    fator_horario = 0.85 if 8 <= hora <= 22 else 1.35

    if teve_bloqueio_429:
        prox = min(INTERVALO_MAX_SEG, int(max(INTERVALO_BASE_SEG * 1.5, ESTADO_VIGILANCIA["proximo_intervalo_seg"] * 1.4)))
        return prox, "RESFRIANDO (429)"

    if teve_oportunidade:
        prox = max(INTERVALO_MIN_SEG, int(INTERVALO_MIN_SEG * fator_horario))
        return prox, "JANELA QUENTE"

    prox = int(max(INTERVALO_MIN_SEG, min(INTERVALO_MAX_SEG, INTERVALO_BASE_SEG * fator_horario)))
    estrategia = "MERCADO ATIVO" if 8 <= hora <= 22 else "MODO ECONÔMICO"
    return prox, estrategia


def executar_varredura_proativa(origem_gatilho: str = "AUTO"):
    fissuras_novas = []
    total_sinais_coletados = 0
    teve_oportunidade = False
    teve_bloqueio_429 = False
    agora_ts = time.time()

    for alvo in TARGETS:
        if agora_ts < ESTADO_VIGILANCIA["fonte_pausada_ate"].get(alvo["id"], 0.0):
            cache_resfriado = ESTADO_VIGILANCIA["cache_coleta"].get(alvo["id"])
            if cache_resfriado:
                total_sinais_coletados += cache_resfriado.get("total_itens", 0)
            continue

        inicio = time.time()
        try:
            itens = extrair_itens_feed(alvo["url"], limite=6)
            latencia = round(time.time() - inicio, 2)

            if not itens:
                ESTADO_VIGILANCIA["falhas_consecutivas_fonte"][alvo["id"]] += 1
                if ESTADO_VIGILANCIA["falhas_consecutivas_fonte"][alvo["id"]] >= 2:
                    ESTADO_VIGILANCIA["fonte_pausada_ate"][alvo["id"]] = agora_ts + 420
                    fissuras_novas.append(registrar_fissura(
                        "FONTE_VAZIA", "critico", alvo["id"],
                        f"{alvo['fonte']} retornou 0 itens (em resfriamento 7m).",
                        "Servir último cache válido aos agentes."
                    ))
                continue

            ESTADO_VIGILANCIA["falhas_consecutivas_fonte"][alvo["id"]] = 0
            ESTADO_VIGILANCIA["fonte_pausada_ate"][alvo["id"]] = 0.0
            ESTADO_VIGILANCIA["cache_coleta"][alvo["id"]] = {
                "id": alvo["id"],
                "fonte": alvo["fonte"],
                "categoria": alvo["categoria"],
                "status": "online",
                "latencia_s": latencia,
                "atualizado_em": hora_atual_brt(),
                "total_itens": len(itens),
                "itens": itens
            }
            total_sinais_coletados += len(itens)

            if latencia > 4.5:
                fissuras_novas.append(registrar_fissura(
                    "FONTE_LENTA", "aviso", alvo["id"],
                    f"{alvo['fonte']} respondeu em {latencia}s.",
                    "Priorizar leitura em cache para evitar atraso no /radar/itens."
                ))

            if alvo["id"] == "crypto_btc_usd" and itens[0].get("preco"):
                atual_cb = float(itens[0]["preco"])
                anterior_cb = ESTADO_VIGILANCIA["ultimo_btc_coinbase"]
                if anterior_cb:
                    var_pct = ((atual_cb - anterior_cb) / anterior_cb) * 100
                    if abs(var_pct) >= 1.2:
                        teve_oportunidade = True
                        fissuras_novas.append(registrar_fissura(
                            "BTC_VOLATILIDADE", "oportunidade", "crypto_btc_usd",
                            f"BTC variou {var_pct:+.2f}% (${anterior_cb:.2f} -> ${atual_cb:.2f}).",
                            "Sinal quente pronto para disparo via webhook."
                        ))
                ESTADO_VIGILANCIA["ultimo_btc_coinbase"] = atual_cb

            if alvo["id"] == "dexscreener_redes":
                for it in itens:
                    var24 = abs(float(it.get("var_24h", 0)))
                    if var24 >= 15.0:
                        teve_oportunidade = True
                        fissuras_novas.append(registrar_fissura(
                            "DEX_MOVIMENTO_FORTE", "oportunidade", "dexscreener_redes",
                            f"Movimento forte detectado: {it['titulo'][:85]}",
                            "Destacar par no pacote pago de sinais."
                        ))
                        break

            for it in itens:
                if it.get("tipo") == "manchete":
                    titulo = it["titulo"]
                    if titulo not in ESTADO_VIGILANCIA["ultimos_titulos_vistos"]:
                        ESTADO_VIGILANCIA["ultimos_titulos_vistos"].append(titulo)
                        if any(p in titulo.lower() for p in PALAVRAS_CHAVE_SINAL):
                            teve_oportunidade = True
                            fissuras_novas.append(registrar_fissura(
                                "SINAL_MACRO_IMPACTO", "oportunidade", alvo["id"],
                                f"Manchete relevante: {titulo[:90]}",
                                "Cruzar com variação on-chain para venda aos agentes."
                            ))

        except Exception as e:
            erro_str = str(e)
            is_429 = "429" in erro_str or "Too Many Requests" in erro_str
            if is_429:
                teve_bloqueio_429 = True

            ESTADO_VIGILANCIA["falhas_consecutivas_fonte"][alvo["id"]] += 1
            if ESTADO_VIGILANCIA["falhas_consecutivas_fonte"][alvo["id"]] >= 2:
                fator_falha = min(4, ESTADO_VIGILANCIA["falhas_consecutivas_fonte"][alvo["id"]])
                pausa_seg = (600 if is_429 else 300) * fator_falha
                ESTADO_VIGILANCIA["fonte_pausada_ate"][alvo["id"]] = agora_ts + pausa_seg
                fissuras_novas.append(registrar_fissura(
                    "FONTE_BLOQUEADA_429" if is_429 else "FONTE_OFFLINE",
                    "critico", alvo["id"],
                    f"{alvo['fonte']} em resfriamento ({pausa_seg // 60}m): {erro_str[:55]}",
                    "Fonte pausada temporariamente para evitar bloqueio contínuo."
                ))

    btc_cb = ESTADO_VIGILANCIA["ultimo_btc_coinbase"]
    btc_ll = ESTADO_VIGILANCIA["ultimo_btc_llama"]
    if btc_cb and btc_ll and btc_cb > 0:
        spread_pct = abs((btc_cb - btc_ll) / btc_cb) * 100
        if spread_pct >= 0.6:
            teve_oportunidade = True
            fissuras_novas.append(registrar_fissura(
                "DIVERGENCIA_CEX_DEX", "oportunidade", "arbitragem_btc",
                f"Spread BTC Coinbase (${btc_cb:.2f}) vs DefiLlama (${btc_ll:.2f}): {spread_pct:.2f}%.",
                "Sinal de arbitragem pronto para agentes."
            ))

    prox_seg, estrategia = _calcular_proximo_intervalo(teve_oportunidade, teve_bloqueio_429)
    ESTADO_VIGILANCIA["proximo_intervalo_seg"] = prox_seg
    ESTADO_VIGILANCIA["estrategia_ritmo"] = estrategia
    ESTADO_VIGILANCIA["ultima_varredura_brt"] = hora_atual_brt()

    registrar_evento(
        f"VARREDURA ({origem_gatilho})",
        f"{len(TARGETS)} fontes • {total_sinais_coletados} sinais • próximo em {prox_seg}s ({estrategia})",
        total_sinais_coletados,
        "saida"
    )
    return {
        "varrido_em": ESTADO_VIGILANCIA["ultima_varredura_brt"],
        "sinais_coletados": total_sinais_coletados,
        "fissuras_detectadas": len(fissuras_novas),
        "proximo_intervalo_seg": prox_seg,
        "estrategia_ritmo": estrategia,
        "fissuras": fissuras_novas
    }


async def loop_vigilancia_background():
    await asyncio.sleep(2)
    while True:
        try:
            await asyncio.to_thread(executar_varredura_proativa, "AUTO")
        except Exception as e:
            registrar_evento("ERRO LOOP", f"Falha na varredura automática: {str(e)[:70]}", 0.0, "erro")
        await asyncio.sleep(ESTADO_VIGILANCIA.get("proximo_intervalo_seg", INTERVALO_BASE_SEG))


@asynccontextmanager
async def lifespan(app: FastAPI):
    carregar_webhooks_disco()
    tarefa = asyncio.create_task(loop_vigilancia_background())
    yield
    tarefa.cancel()


app = FastAPI(
    title="Robo2 Signals API",
    description="API proativa e adaptativa de sinais estruturados em tempo real via x402 em USDC (Solana) com Webhooks Autônomos.",
    version="2.9.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"]
)


def montar_manifesto_x402(resource_url: str):
    return {
        "x402Version": 1,
        "error": "Payment Required",
        "accepts": [{
            "scheme": "exact",
            "network": REDE_PAGAMENTO,
            "asset": USDC_MINT_SOLANA,
            "currency": MOEDA_PAGAMENTO,
            "payTo": PAY_TO_ADDRESS,
            "maxAmountRequired": str(int(round(PRECO_USDC * 1_000_000))),
            "amountUSDC": f"{PRECO_USDC:.2f}",
            "resource": resource_url,
            "description": "Sinais estruturados em tempo real de Economia, Tech, Cripto Multi-Redes e Fissuras.",
            "mimeType": "application/json"
        }]
    }


@app.get("/")
def root():
    registrar_evento("CHAMADA /", "Consulta de status da API", 0.0, "chamada")
    return {
        "status": "online",
        "servico": "Robo2 Signals API",
        "versao": "2.9.0",
        "modo": "autonomo_adaptativo",
        "estrategia_ritmo": ESTADO_VIGILANCIA["estrategia_ritmo"],
        "proximo_intervalo_seg": ESTADO_VIGILANCIA["proximo_intervalo_seg"],
        "ultima_varredura": ESTADO_VIGILANCIA["ultima_varredura_brt"],
        "moeda": MOEDA_PAGAMENTO,
        "rede": REDE_PAGAMENTO,
        "recebedor": PAY_TO_ADDRESS,
        "preco_usdc": f"{PRECO_USDC:.2f}",
        "rotas": {
            "painel": "/painel",
            "radar": "/radar",
            "itens_x402": "/radar/itens",
            "fissuras": "/api/fissuras",
            "forcar_varredura": "/api/varrer-agora",
            "webhooks": "/webhooks",
            "registrar_webhook": "/webhooks/registrar",
            "x402": "/.well-known/x402",
            "agente": "/.well-known/agent.json",
            "openapi": "/openapi.json"
        }
    }


@app.get("/webhooks")
def listar_webhooks():
    registrar_evento("CHAMADA /webhooks", f"{len(ESTADO_VIGILANCIA['webhooks_inscritos'])} webhooks cadastrados", 0.0, "chamada")
    return {
        "total": len(ESTADO_VIGILANCIA["webhooks_inscritos"]),
        "cooldown_seg": COOLDOWN_WEBHOOK_SEG,
        "max_falhas_antes_pausa": MAX_FALHAS_WEBHOOK,
        "webhooks": ESTADO_VIGILANCIA["webhooks_inscritos"]
    }


@app.post("/webhooks/registrar")
def registrar_webhook(dados: WebhookCadastro):
    url_limpa = dados.url.strip()
    if not (url_limpa.startswith("https://") or url_limpa.startswith("http://")):
        return JSONResponse(status_code=400, content={"erro": "URL de webhook inválida. Use http:// ou https://"})

    cliente_id = (dados.cliente_id or "").strip() or f"agente_{abs(hash(url_limpa)) % 10000}"
    carteira = (dados.carteira_solana or "").strip() or None
    acao = "cadastrado"

    with LOCK_WEBHOOKS:
        alvo_hook = None
        for w in ESTADO_VIGILANCIA["webhooks_inscritos"]:
            if w["url"] == url_limpa:
                w["gravidades"] = dados.gravidades
                w["cliente_id"] = cliente_id
                w["carteira_solana"] = carteira
                w["ativo"] = True
                w["falhas_seguidas"] = 0
                alvo_hook = w
                acao = "atualizado"
                break

        if alvo_hook is None:
            alvo_hook = {
                "url": url_limpa,
                "gravidades": dados.gravidades,
                "cliente_id": cliente_id,
                "carteira_solana": carteira,
                "ativo": True,
                "envios_ok": 0,
                "falhas_seguidas": 0,
                "ultimo_envio_brt": None,
                "criado_em": hora_atual_brt()
            }
            ESTADO_VIGILANCIA["webhooks_inscritos"].append(alvo_hook)

    salvar_webhooks_disco()
    registrar_evento(f"WEBHOOK {acao.upper()}", f"[{cliente_id}] {url_limpa}", 0.0, "chamada")

    boas_vindas = json.dumps({
        "servico": "Robo2 Signals API",
        "versao": "2.9.0",
        "tipo_mensagem": "assinatura_confirmada",
        "emitido_em": datetime.now(BRT).isoformat(),
        "pagamento_x402": {
            "endpoint_completo": "/radar/itens",
            "protocolo": "x402",
            "moeda": MOEDA_PAGAMENTO,
            "rede": REDE_PAGAMENTO,
            "preco_usdc": PRECO_USDC,
            "payTo": PAY_TO_ADDRESS
        },
        "ultima_fissura": ESTADO_VIGILANCIA["fissuras_ativas"][0] if ESTADO_VIGILANCIA["fissuras_ativas"] else None
    }).encode("utf-8")
    threading.Thread(
        target=_enviar_webhook_com_retentativa,
        args=(alvo_hook, "ASSINATURA_ATIVA", boas_vindas),
        daemon=True
    ).start()

    return {"status": acao, "webhook": alvo_hook}


@app.delete("/webhooks")
def remover_webhook(url: str = Query(...)):
    with LOCK_WEBHOOKS:
        antes = len(ESTADO_VIGILANCIA["webhooks_inscritos"])
        ESTADO_VIGILANCIA["webhooks_inscritos"] = [
            w for w in ESTADO_VIGILANCIA["webhooks_inscritos"] if w["url"] != url.strip()
        ]
        removidos = antes - len(ESTADO_VIGILANCIA["webhooks_inscritos"])
    salvar_webhooks_disco()
    registrar_evento("WEBHOOK REMOVIDO", f"{url} ({removidos})", 0.0, "chamada")
    return {"removidos": removidos}


@app.post("/api/varrer-agora")
@app.get("/api/varrer-agora")
def varrer_agora():
    return executar_varredura_proativa("MANUAL")


@app.get("/api/fissuras")
def listar_fissuras(executar_agora: bool = Query(default=False)):
    registrar_evento("CHAMADA /api/fissuras", f"Consulta de fissuras (executar_agora={executar_agora})", 0.0, "chamada")
    if executar_agora:
        executar_varredura_proativa("API_FISSURAS")
    return {
        "hora_agora": hora_atual_brt(),
        "ultima_varredura": ESTADO_VIGILANCIA["ultima_varredura_brt"],
        "total_fissuras_registradas": len(ESTADO_VIGILANCIA["fissuras_ativas"]),
        "fissuras": list(ESTADO_VIGILANCIA["fissuras_ativas"])
    }


@app.get("/api/metricas")
def obter_metricas():
    carteira = consultar_saldo_solana_usdc(forcar=False)
    horas_ordenadas = sorted(METRICAS["historico_horas"].values(), key=lambda x: x["hora"])[-8:]
    with LOCK_WEBHOOKS:
        lista_hooks = list(ESTADO_VIGILANCIA["webhooks_inscritos"])
    return {
        "hora_agora": hora_atual_brt(),
        "iniciado_em": METRICAS["iniciado_em_brt"],
        "ultima_varredura": ESTADO_VIGILANCIA["ultima_varredura_brt"],
        "estrategia_ritmo": ESTADO_VIGILANCIA["estrategia_ritmo"],
        "proximo_intervalo_seg": ESTADO_VIGILANCIA["proximo_intervalo_seg"],
        "carteira": PAY_TO_ADDRESS,
        "saldo_usdc": carteira.get("saldo_usdc", 0.0),
        "receita_sessao_usdc": round(METRICAS["receita_sessao_usdc"], 4),
        "vendas_pagas": METRICAS["total_vendas_pagas"],
        "cobrancas_402": METRICAS["total_cobrancas_402"],
        "itens_entregues": METRICAS["total_itens_entregues"],
        "total_chamadas": METRICAS["total_chamadas"],
        "total_webhooks_enviados": METRICAS["total_webhooks_enviados"],
        "webhooks_ativos": sum(1 for w in lista_hooks if w.get("ativo", True)),
        "webhooks_lista": lista_hooks,
        "fissuras_ativas": len(ESTADO_VIGILANCIA["fissuras_ativas"]),
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
  <title>Robo2 Painel — Operações e Chamadas em Tempo Real</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: #0d1117; color: #e6edf3; padding: 18px; }
    .wrap { max-width: 980px; margin: 0 auto; }
    .top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
    .titulo { font-size: 1.25rem; font-weight: 700; display: flex; align-items: center; gap: 8px; }
    .ponto { width: 9px; height: 9px; border-radius: 50%; background: #2ea043; box-shadow: 0 0 8px #2ea043; }
    .acoes-topo { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .hora-box { background: #161b22; border: 1px solid #30363d; padding: 6px 12px; border-radius: 8px; font-size: 0.82rem; color: #8b949e; }
    .btn-varrer { background: #238636; color: #fff; border: none; padding: 7px 13px; border-radius: 8px; font-size: 0.82rem; font-weight: 600; cursor: pointer; }
    .btn-varrer:hover { background: #2ea043; }
    .btn-varrer:disabled { opacity: 0.6; cursor: wait; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin-bottom: 18px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 14px; }
    .rotulo { font-size: 0.73rem; color: #8b949e; text-transform: uppercase; margin-bottom: 6px; }
    .valor { font-size: 1.5rem; font-weight: 700; }
    .verde { color: #3fb950; }
    .amarelo { color: #d29922; }
    .azul { color: #58a6ff; }
    .roxo { color: #bc8cff; }
    .sub { font-size: 0.74rem; color: #8b949e; margin-top: 4px; }
    .caixa { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 16px; margin-bottom: 18px; }
    .caixa-topo { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
    .caixa-topo h2 { font-size: 0.98rem; font-weight: 600; }
    .legenda { display: flex; gap: 14px; font-size: 0.75rem; color: #8b949e; }
    .legenda span { display: inline-flex; align-items: center; gap: 5px; }
    .quad { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
    .grafico { display: flex; align-items: flex-end; gap: 12px; height: 140px; padding-top: 12px; border-bottom: 1px solid #30363d; }
    .coluna-grupo { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
    .barras { display: flex; align-items: flex-end; gap: 4px; height: 105px; width: 100%; justify-content: center; }
    .barra { width: 14px; border-radius: 4px 4px 0 0; min-height: 3px; transition: height 0.3s ease; }
    .b-402 { background: #d29922; }
    .b-saida { background: #58a6ff; }
    .b-entrada { background: #3fb950; }
    .hora-lbl { font-size: 0.72rem; color: #8b949e; margin-top: 6px; }
    .hook-form { display: flex; gap: 8px; flex-wrap: wrap; width: 100%; }
    .hook-input { flex: 1; min-width: 200px; background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 8px; padding: 8px 11px; font-size: 0.83rem; }
    .hook-input.curto { flex: 0 1 180px; min-width: 140px; }
    .btn-hook { background: #1f6feb; color: #fff; border: none; padding: 8px 14px; border-radius: 8px; font-size: 0.82rem; font-weight: 600; cursor: pointer; }
    .btn-hook:hover { background: #388bfd; }
    .hook-msg { font-size: 0.76rem; color: #8b949e; margin-top: 8px; }
    .hook-lista { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
    .hook-item { display: flex; justify-content: space-between; align-items: center; gap: 8px; background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 7px 10px; font-size: 0.78rem; }
    .btn-rem { background: transparent; color: #f85149; border: 1px solid rgba(248,81,73,0.35); padding: 2px 8px; border-radius: 6px; font-size: 0.72rem; cursor: pointer; }
    .filtros { display: flex; gap: 6px; flex-wrap: wrap; }
    .filtro-btn { background: #0d1117; color: #8b949e; border: 1px solid #30363d; padding: 4px 10px; border-radius: 999px; font-size: 0.74rem; cursor: pointer; }
    .filtro-btn.ativo { background: #1f6feb; color: #fff; border-color: #1f6feb; }
    table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
    th, td { padding: 9px 8px; border-bottom: 1px solid #21262d; text-align: left; }
    th { color: #8b949e; font-size: 0.73rem; text-transform: uppercase; }
    .pill { padding: 3px 9px; border-radius: 999px; font-size: 0.71rem; font-weight: 600; white-space: nowrap; display: inline-block; }
    .p-aprovada { background: rgba(63,185,80,0.20); color: #3fb950; border: 1px solid rgba(63,185,80,0.45); }
    .p-402 { background: rgba(210,153,34,0.16); color: #d29922; }
    .p-chamada { background: rgba(88,166,255,0.15); color: #58a6ff; }
    .p-erro { background: rgba(248,81,73,0.16); color: #f85149; }
    .vazio { text-align: center; color: #8b949e; padding: 20px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="titulo"><span class="ponto"></span> Robo2 Signals — Monitor Autônomo</div>
      <div class="acoes-topo">
        <div class="hora-box" id="ritmo-topo">Ritmo: --</div>
        <div class="hora-box" id="varredura-topo">Varredura: --:--:--</div>
        <div class="hora-box" id="hora-topo">Hora: --:--:--</div>
        <button class="btn-varrer" id="btn-varrer" onclick="forcarVarredura()">Varrer Agora</button>
      </div>
    </div>

    <div class="cards">
      <div class="card">
        <div class="rotulo">Saldo Phantom (USDC)</div>
        <div class="valor verde" id="saldo">$0.00</div>
        <div class="sub">Solana • Carteira ativa</div>
      </div>
      <div class="card">
        <div class="rotulo">Operações Aprovadas</div>
        <div class="valor verde" id="entradas">$0.00</div>
        <div class="sub" id="qtd-vendas">0 vendas aprovadas</div>
      </div>
      <div class="card">
        <div class="rotulo">Cobranças 402</div>
        <div class="valor amarelo" id="cobrancas">0</div>
        <div class="sub">Pedidos x402</div>
      </div>
      <div class="card">
        <div class="rotulo">Chamadas Totais</div>
        <div class="valor roxo" id="chamadas">0</div>
        <div class="sub" id="fissuras-sub">0 fissuras • 0 hooks</div>
      </div>
      <div class="card">
        <div class="rotulo">Sinais Coletados</div>
        <div class="valor azul" id="saidas">0</div>
        <div class="sub">Economia + Multi-Redes</div>
      </div>
    </div>

    <div class="caixa">
      <div class="caixa-topo">
        <h2>Movimento por Hora</h2>
        <div class="legenda">
          <span><i class="quad b-402"></i>Cobranças</span>
          <span><i class="quad b-saida"></i>Sinais/Saídas</span>
          <span><i class="quad b-entrada"></i>Aprovadas (USDC)</span>
        </div>
      </div>
      <div class="grafico" id="grafico">
        <div class="vazio" style="width:100%">Sem movimentação por hora ainda.</div>
      </div>
    </div>

    <div class="caixa">
      <div class="caixa-topo">
        <h2>Webhook Autônomo de Saída (Agentes / Clientes)</h2>
        <span class="sub" id="hooks-ativos-lbl">0 ativos</span>
      </div>
      <div class="hook-form">
        <input class="hook-input curto" id="hook-cliente" type="text" placeholder="ID do agente/cliente" />
        <input class="hook-input" id="hook-url" type="url" placeholder="https://endpoint-do-cliente/webhook" />
        <button class="btn-hook" onclick="cadastrarWebhook()">Ativar Webhook</button>
      </div>
      <div class="hook-msg" id="hook-msg">Envia automaticamente o alerta e o caminho de pagamento x402 quando surge fissura crítica ou oportunidade.</div>
      <div class="hook-lista" id="hook-lista"></div>
    </div>

    <div class="caixa">
      <div class="caixa-topo">
        <div>
          <h2>Operações Aprovadas & Chamadas em Tempo Real</h2>
          <span class="sub" id="status-sync">Atualiza a cada 3s</span>
        </div>
        <div class="filtros">
          <button class="filtro-btn ativo" onclick="mudarFiltro('todos', this)">Todas</button>
          <button class="filtro-btn" onclick="mudarFiltro('aprovada', this)">Aprovadas</button>
          <button class="filtro-btn" onclick="mudarFiltro('402', this)">Cobranças 402</button>
          <button class="filtro-btn" onclick="mudarFiltro('chamadas', this)">Chamadas & Varreduras</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Hora</th>
            <th>Status / Tipo</th>
            <th>Operação / Chamada</th>
            <th>Valor</th>
          </tr>
        </thead>
        <tbody id="lista">
          <tr><td colspan="4" class="vazio">Carregando operações e chamadas...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    let filtroAtual = 'todos';
    let eventosCache = [];

    function mudarFiltro(novoFiltro, botao) {
      filtroAtual = novoFiltro;
      document.querySelectorAll('.filtro-btn').forEach(b => b.classList.remove('ativo'));
      botao.classList.add('ativo');
      renderizarTabela();
    }

    function renderizarTabela() {
      const tb = document.getElementById('lista');
      const filtrados = eventosCache.filter(ev => {
        if (filtroAtual === 'todos') return true;
        if (filtroAtual === 'aprovada') return ev.status === 'aprovada' || ev.status === 'entrada';
        if (filtroAtual === '402') return ev.status === '402';
        if (filtroAtual === 'chamadas') return ev.status === 'chamada' || ev.status === 'saida' || ev.status === 'erro';
        return true;
      });

      if (!filtrados || filtrados.length === 0) {
        tb.innerHTML = '<tr><td colspan="4" class="vazio">Nenhum registro neste filtro ainda.</td></tr>';
        return;
      }

      tb.innerHTML = filtrados.map(ev => {
        let cls = 'p-chamada';
        if (ev.status === 'aprovada' || ev.status === 'entrada') cls = 'p-aprovada';
        else if (ev.status === '402') cls = 'p-402';
        else if (ev.status === 'erro') cls = 'p-erro';

        const val = ev.valor_usdc > 0 && (ev.status === 'aprovada' || ev.status === 'entrada')
          ? '+$' + Number(ev.valor_usdc).toFixed(2) + ' USDC'
          : (ev.valor_usdc > 0 ? ev.valor_usdc + ' itens' : '—');
        const corVal = (ev.status === 'aprovada' || ev.status === 'entrada') ? '#3fb950' : '#8b949e';

        return `<tr>
          <td>${ev.hora}</td>
          <td><span class="pill ${cls}">${ev.tipo}</span></td>
          <td>${ev.detalhe}</td>
          <td style="font-weight:600; color:${corVal}">${val}</td>
        </tr>`;
      }).join('');
    }

    async function cadastrarWebhook() {
      const inpUrl = document.getElementById('hook-url');
      const inpCli = document.getElementById('hook-cliente');
      const msg = document.getElementById('hook-msg');
      const url = inpUrl.value.trim();
      const cliente_id = inpCli.value.trim() || null;
      if (!url) {
        msg.textContent = 'Informe uma URL http:// ou https:// do agente/cliente.';
        return;
      }
      try {
        const r = await fetch('/webhooks/registrar', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, cliente_id, gravidades: ['critico', 'oportunidade'] })
        });
        const j = await r.json();
        if (r.ok) {
          msg.textContent = 'Webhook ativo (' + j.status + ') para ' + (j.webhook.cliente_id || '') + ': ' + url;
          inpUrl.value = '';
          inpCli.value = '';
          await carregar();
        } else {
          msg.textContent = 'Erro: ' + (j.erro || 'URL inválida');
        }
      } catch (e) {
        msg.textContent = 'Falha ao salvar webhook.';
      }
    }

    async function removerWebhook(url) {
      await fetch('/webhooks?url=' + encodeURIComponent(url), { method: 'DELETE' });
      await carregar();
    }

    async function forcarVarredura() {
      const btn = document.getElementById('btn-varrer');
      btn.disabled = true;
      btn.textContent = 'Varrendo...';
      try {
        await fetch('/api/varrer-agora', { method: 'POST' });
        await carregar();
      } finally {
        btn.disabled = false;
        btn.textContent = 'Varrer Agora';
      }
    }

    async function carregar() {
      try {
        const r = await fetch('/api/metricas');
        const d = await r.json();

        document.getElementById('ritmo-topo').textContent = 'Ritmo: ' + (d.estrategia_ritmo || '--') + ' (' + (d.proximo_intervalo_seg || '--') + 's)';
        document.getElementById('hora-topo').textContent = 'Hora: ' + d.hora_agora;
        document.getElementById('varredura-topo').textContent = 'Última varredura: ' + d.ultima_varredura;
        document.getElementById('saldo').textContent = '$' + Number(d.saldo_usdc).toFixed(2);
        document.getElementById('entradas').textContent = '$' + Number(d.receita_sessao_usdc).toFixed(2);
        document.getElementById('qtd-vendas').textContent = d.vendas_pagas + ' vendas aprovadas';
        document.getElementById('cobrancas').textContent = d.cobrancas_402;
        document.getElementById('chamadas').textContent = d.total_chamadas || 0;
        document.getElementById('fissuras-sub').textContent = (d.fissuras_ativas || 0) + ' fissuras • ' + (d.total_webhooks_enviados || 0) + ' hooks';
        document.getElementById('hooks-ativos-lbl').textContent = (d.webhooks_ativos || 0) + ' ativos';
        document.getElementById('saidas').textContent = d.itens_entregues;
        document.getElementById('status-sync').textContent = 'Sincronizado às ' + d.hora_agora;

        const hl = document.getElementById('hook-lista');
        const hooks = d.webhooks_lista || [];
        hl.innerHTML = hooks.map(h => {
          const st = h.ativo !== false ? '<span style="color:#3fb950">● ativo</span>' : '<span style="color:#f85149">● pausado</span>';
          return `<div class="hook-item">
            <div>${st} <strong>${h.cliente_id || 'agente'}</strong> • ${h.url} <span style="color:#8b949e">(${h.envios_ok || 0} envios)</span></div>
            <button class="btn-rem" onclick="removerWebhook('${h.url}')">Remover</button>
          </div>`;
        }).join('');

        const g = document.getElementById('grafico');
        if (d.grafico_horas && d.grafico_horas.length > 0) {
          const maxVal = Math.max(1, ...d.grafico_horas.map(h => Math.max(h.cobrancas, h.saidas_itens, h.entradas_usdc * 100)));
          g.innerHTML = d.grafico_horas.map(h => {
            const h402 = Math.max(4, Math.round((h.cobrancas / maxVal) * 100));
            const hSai = Math.max(4, Math.round((h.saidas_itens / maxVal) * 100));
            const hEnt = Math.max(4, Math.round(((h.entradas_usdc * 100) / maxVal) * 100));
            return `<div class="coluna-grupo">
              <div class="barras">
                <div class="barra b-402" style="height:${h402}px" title="Cobranças: ${h.cobrancas}"></div>
                <div class="barra b-saida" style="height:${hSai}px" title="Saídas/Sinais: ${h.saidas_itens}"></div>
                <div class="barra b-entrada" style="height:${hEnt}px" title="Aprovadas: $${h.entradas_usdc}"></div>
              </div>
              <div class="hora-lbl">${h.hora}</div>
            </div>`;
          }).join('');
        }

        eventosCache = d.eventos || [];
        renderizarTabela();
      } catch (e) {
        document.getElementById('status-sync').textContent = 'Reconectando...';
      }
    }

    carregar();
    setInterval(carregar, 3000);
  </script>
</body>
</html>"""


@app.get("/.well-known/x402")
def well_known_x402(request: Request):
    registrar_evento("CHAMADA /.well-known/x402", "Descoberta de protocolo x402 por agente", 0.0, "chamada")
    base = str(request.base_url).rstrip("/")
    return {
        "version": 1,
        "provider": "Robo2 Signals API",
        "network": REDE_PAGAMENTO,
        "currency": MOEDA_PAGAMENTO,
        "payTo": PAY_TO_ADDRESS,
        "endpoints": [
            {
                "path": "/radar/itens",
                "method": "GET",
                "priceUSDC": f"{PRECO_USDC:.2f}",
                "description": "Manchetes Economia/Tech + Cotações CEX/DEX Multi-Redes + Fissuras."
            },
            {
                "path": "/webhooks/registrar",
                "method": "POST",
                "priceUSDC": "0.00",
                "description": "Assina notificações push autônomas de fissuras críticas e oportunidades."
            }
        ],
        "openapi": f"{base}/openapi.json"
    }


@app.get("/.well-known/agent.json")
def well_known_agent(request: Request):
    registrar_evento("CHAMADA /.well-known/agent.json", "Descoberta de agente autônomo", 0.0, "chamada")
    base = str(request.base_url).rstrip("/")
    return {
        "name": "Robo2 Signals API",
        "version": "2.9.0",
        "autonomous_scanning": True,
        "adaptive_rhythm": True,
        "autonomous_webhooks": True,
        "payment": {"protocol": "x402", "network": REDE_PAGAMENTO, "currency": MOEDA_PAGAMENTO, "payTo": PAY_TO_ADDRESS, "priceUSDC": PRECO_USDC},
        "capabilities": ["macro_tech_news", "multichain_dex_prices", "fissure_detection", "adaptive_backoff", "autonomous_push_webhooks"],
        "endpoints": {
            "signals_paid": f"{base}/radar/itens",
            "fissures": f"{base}/api/fissuras",
            "webhook_subscribe": f"{base}/webhooks/registrar",
            "webhooks_list": f"{base}/webhooks",
            "openapi": f"{base}/openapi.json"
        }
    }


@app.get("/.well-known/ai-plugin.json")
def well_known_ai_plugin(request: Request):
    registrar_evento("CHAMADA /ai-plugin.json", "Descoberta de manifest AI Plugin", 0.0, "chamada")
    base = str(request.base_url).rstrip("/")
    return {
        "schema_version": "v1",
        "name_for_human": "Robo2 Signals API",
        "name_for_model": "robo2_signals",
        "description_for_human": "Sinais de Economia, Tech e Redes Descentralizadas via x402 USDC com Ritmo Adaptativo e Webhooks Autônomos.",
        "description_for_model": "Fetches structured economy, tech, decentralized crypto markets, and anomaly signals via GET /radar/itens with x402 USDC payment on Solana, and subscribes autonomous agents via POST /webhooks/registrar.",
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": f"{base}/openapi.json"}
    }


@app.get("/radar")
def verificar_radar():
    resultados = []
    total_kb = 0
    for alvo in TARGETS:
        if alvo["url"].startswith("descentralizado://"):
            cache_item = ESTADO_VIGILANCIA["cache_coleta"].get(alvo["id"])
            resultados.append({
                "id": alvo["id"],
                "categoria": alvo["categoria"],
                "status": "online" if cache_item else "aquecendo",
                "itens_em_cache": cache_item["total_itens"] if cache_item else 0
            })
            continue
        try:
            req = urllib.request.Request(alvo["url"], headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/2.9)"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                conteudo = resp.read()
                total_kb += max(1, len(conteudo) // 1024)
                resultados.append({"id": alvo["id"], "categoria": alvo["categoria"], "status": "online", "bytes": len(conteudo)})
        except Exception as e:
            resultados.append({"id": alvo["id"], "categoria": alvo["categoria"], "status": "erro", "detalhe": str(e)})
    registrar_evento("CHAMADA /radar", f"Checagem de {len(TARGETS)} fontes ({total_kb} KB)", total_kb, "chamada")
    return {
        "registrado_em": datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S"),
        "ultima_varredura_proativa": ESTADO_VIGILANCIA["ultima_varredura_brt"],
        "resultados": resultados
    }


@app.get("/radar/itens")
def listar_itens_radar(
    request: Request,
    categoria: str | None = Query(default=None),
    limite_por_fonte: int = Query(default=6, ge=1, le=20)
):
    comprovante = request.headers.get("X-PAYMENT") or request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-Payment-Proof")

    if not comprovante:
        METRICAS["total_cobrancas_402"] += 1
        registrar_evento("COBRANÇA 402", f"Chamada em /radar/itens ({categoria or 'todas'})", 0.0, "402")
        manifesto = montar_manifesto_x402(str(request.url))
        header_b64 = base64.b64encode(json.dumps(manifesto).encode("utf-8")).decode("utf-8")
        return JSONResponse(status_code=402, content=manifesto, headers={"PAYMENT-REQUIRED": header_b64})

    pago_ok, detalhe_tx = validar_pagamento_solana_usdc(comprovante)
    if not pago_ok:
        registrar_evento("RECUSADA 402", f"/radar/itens • {detalhe_tx}", 0.0, "erro")
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
            coleta.append({
                "id": alvo["id"],
                "fonte": alvo["fonte"],
                "categoria": alvo["categoria"],
                "status": "online",
                "total_itens": len(itens),
                "itens": itens
            })
        except Exception as e:
            cache_fb = ESTADO_VIGILANCIA["cache_coleta"].get(alvo["id"])
            if cache_fb:
                itens_fb = cache_fb["itens"][:limite_por_fonte]
                total_geral += len(itens_fb)
                coleta.append({
                    "id": alvo["id"],
                    "fonte": alvo["fonte"],
                    "categoria": alvo["categoria"],
                    "status": "cache_contingencia",
                    "total_itens": len(itens_fb),
                    "itens": itens_fb
                })
            else:
                coleta.append({
                    "id": alvo["id"],
                    "fonte": alvo["fonte"],
                    "categoria": alvo["categoria"],
                    "status": "erro",
                    "detalhe": str(e),
                    "total_itens": 0,
                    "itens": []
                })

    coleta.sort(key=lambda x: x.get("total_itens", 0), reverse=True)
    METRICAS["total_vendas_pagas"] += 1
    METRICAS["receita_sessao_usdc"] += PRECO_USDC
    METRICAS["total_itens_entregues"] += total_geral

    registrar_evento(
        "OPERAÇÃO APROVADA",
        f"Entrega de {total_geral} sinais ({categoria or 'todas'}) • {detalhe_tx}",
        PRECO_USDC,
        "aprovada"
    )

    return JSONResponse(
        status_code=200,
        headers={"X-PAYMENT-RESPONSE": detalhe_tx},
        content={
            "registrado_em": datetime.now(BRT).isoformat(),
            "pagamento_validado": detalhe_tx,
            "filtro_categoria": categoria or "todas",
            "total_itens_retornados": total_geral,
            "fissuras_recentes": list(ESTADO_VIGILANCIA["fissuras_ativas"])[:5],
            "fontes": coleta
        }
    )
