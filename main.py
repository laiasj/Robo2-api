import asyncio
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = os.getenv("DB_PATH", "robo2_producao.db")
CUSTO_POR_CHAMADA = float(os.getenv("CUSTO_POR_CHAMADA", "1.0"))
ADMIN_KEY = os.getenv("ADMIN_KEY", "robo2_prod_admin_key_90765")
PORT = int(os.getenv("PORT", "8000"))

TARGETS = [
    {"id": "coingecko_btc", "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"},
    {"id": "coinbase_btc", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"},
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bots (
                bot_key TEXT PRIMARY KEY,
                saldo REAL NOT NULL DEFAULT 0.0,
                ativo INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS creditos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_key TEXT NOT NULL,
                valor REAL NOT NULL,
                criado_em INTEGER NOT NULL
            )
            """
        )
        conn.commit()


class CriarBotRequest(BaseModel):
    bot_key: str
    saldo_inicial: float = 10.0


class CreditoRequest(BaseModel):
    bot_key: str
    valor: float


class ConsultaRequest(BaseModel):
    bot_key: str
    url: str | None = None
    payload: dict[str, Any] | None = None


async def monitor_loop() -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            for target in TARGETS:
                try:
                    resp = await client.get(target["url"])
                    _ = resp.status_code
                except Exception:
                    pass
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(monitor_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Robo2-api", lifespan=lifespan)


def validar_admin(x_admin_key: str | None) -> None:
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Chave administrativa inválida.")


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return {"status": "online", "servico": "Robo2-api"}


@app.post("/admin/criar-bot")
def criar_bot(dados: CriarBotRequest, x_admin_key: str | None = Header(default=None)):
    validar_admin(x_admin_key)
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bots (bot_key, saldo, ativo) VALUES (?, ?, 1)",
            (dados.bot_key, dados.saldo_inicial),
        )
        conn.commit()
        row = conn.execute(
            "SELECT bot_key, saldo, ativo FROM bots WHERE bot_key = ?",
            (dados.bot_key,),
        ).fetchone()
    return dict(row)


@app.post("/admin/adicionar-credito")
def adicionar_credito(dados: CreditoRequest, x_admin_key: str | None = Header(default=None)):
    validar_admin(x_admin_key)
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bots (bot_key, saldo, ativo) VALUES (?, 0.0, 1)",
            (dados.bot_key,),
        )
        conn.execute(
            "UPDATE bots SET saldo = saldo + ? WHERE bot_key = ?",
            (dados.valor, dados.bot_key),
        )
        conn.execute(
            "INSERT INTO creditos (bot_key, valor, criado_em) VALUES (?, ?, ?)",
            (dados.bot_key, dados.valor, int(time.time())),
        )
        conn.commit()
        row = conn.execute(
            "SELECT bot_key, saldo, ativo FROM bots WHERE bot_key = ?",
            (dados.bot_key,),
        ).fetchone()
    return dict(row)


@app.post("/consultar")
async def consultar(dados: ConsultaRequest):
    with get_db() as conn:
        bot = conn.execute(
            "SELECT bot_key, saldo, ativo FROM bots WHERE bot_key = ?",
            (dados.bot_key,),
        ).fetchone()
        if not bot or not bot["ativo"]:
            raise HTTPException(status_code=403, detail="Bot inexistente ou inativo.")
        if bot["saldo"] < CUSTO_POR_CHAMADA:
            return JSONResponse(status_code=402, content={"detail": "Saldo insuficiente."})

    conteudo_extraido: Any = None
    if dados.url:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(dados.url)
            content_type = r.headers.get("content-type", "")
            if "application/json" in content_type:
                conteudo_extraido = r.json()
            else:
                soup = BeautifulSoup(r.text, "html.parser")
                conteudo_extraido = {
                    "titulo": soup.title.string.strip() if soup.title and soup.title.string else None,
                    "texto": " ".join(soup.get_text().split())[:2000],
                }

    with get_db() as conn:
        conn.execute(
            "UPDATE bots SET saldo = saldo - ? WHERE bot_key = ?",
            (CUSTO_POR_CHAMADA, dados.bot_key),
        )
        conn.commit()
        saldo_atual = conn.execute(
            "SELECT saldo FROM bots WHERE bot_key = ?",
            (dados.bot_key,),
        ).fetchone()["saldo"]

    return {
        "status": "ok",
        "saldo_restante": saldo_atual,
        "payload_recebido": dados.payload,
        "dados": conteudo_extraido,
    }

from datetime import datetime
import json
import urllib.request

TARGETS = [
    {"id": "cripto_coinbase", "categoria": "Financas e Cripto", "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"},
    {"id": "mercado_cnbc", "categoria": "Financas e Mercados", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"},
    {"id": "tech_hackernews", "categoria": "Tecnologia", "url": "https://news.ycombinator.com/rss"},
    {"id": "tech_theverge", "categoria": "Tecnologia", "url": "https://www.theverge.com/rss/index.xml"},
    {"id": "economia_bbc", "categoria": "Economia e Negocios", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"}
]


def garantir_tabela_radar():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS radar_historico (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registrado_em TEXT NOT NULL,
                alvo_id TEXT NOT NULL,
                categoria TEXT NOT NULL,
                status TEXT NOT NULL,
                volume_dados INTEGER NOT NULL,
                detalhe TEXT
            )
            """
        )
        conn.commit()


@app.get("/radar")
def verificar_categorias():
    garantir_tabela_radar()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resultados = []

    with get_db() as conn:
        for alvo in TARGETS:
            status = "online"
            volume = 0
            detalhe = None
            try:
                req = urllib.request.Request(
                    alvo["url"],
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; Robo2-Radar/1.0)",
                        "Accept": "*/*"
                    }
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    conteudo = resp.read().decode("utf-8", errors="ignore")
                    volume = len(conteudo)
            except Exception as e:
                status = "erro"
                detalhe = str(e)

            conn.execute(
                """
                INSERT INTO radar_historico
                    (registrado_em, alvo_id, categoria, status, volume_dados, detalhe)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agora, alvo["id"], alvo["categoria"], status, volume, detalhe)
            )

            item = {
                "id": alvo["id"],
                "categoria": alvo["categoria"],
                "status": status,
                "volume_dados": volume
            }
            if detalhe:
                item["detalhe"] = detalhe
            resultados.append(item)

        conn.commit()

    resultados.sort(key=lambda x: x.get("volume_dados", 0), reverse=True)
    return {
        "registrado_em": agora,
        "ranking_movimento": resultados
    }


@app.get("/radar/historico")
def consultar_historico_radar(limite: int = 50):
    garantir_tabela_radar()
    with get_db() as conn:
        linhas = conn.execute(
            """
            SELECT id, registrado_em, alvo_id, categoria, status, volume_dados, detalhe
            FROM radar_historico
            ORDER BY id DESC
            LIMIT ?
            """,
            (limite,)
        ).fetchall()

    return {
        "total_registros": len(linhas),
        "historico": [dict(linha) for linha in linhas]
    }
