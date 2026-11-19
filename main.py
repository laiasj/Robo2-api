import json
import os
import sqlite3
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
import requests
import trafilatura

DB_PATH = os.getenv("DB_PATH", "robo2_producao.db")
CUSTO_POR_CHAMADA = float(os.getenv("CUSTO_POR_CHAMADA", "1.0"))
ADMIN_KEY = os.getenv("ADMIN_KEY", "robo2_prod_admin_key_98765")
PORT = int(os.getenv("PORT", "8000"))


def get_conn():
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  return conn


def init_db():
  conn = get_conn()
  cur = conn.cursor()
  cur.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            bot_key TEXT PRIMARY KEY,
            saldo REAL NOT NULL DEFAULT 0.0,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em INTEGER NOT NULL DEFAULT 0
        )
    """)
  cur.execute("""
        CREATE TABLE IF NOT EXISTS creditos (
            payment_id TEXT PRIMARY KEY,
            bot_key TEXT NOT NULL,
            valor REAL NOT NULL,
            origem TEXT NOT NULL,
            criado_em INTEGER NOT NULL
        )
    """)
  cur.execute("""
        CREATE TABLE IF NOT EXISTS transacoes (
            request_id TEXT PRIMARY KEY,
            bot_key TEXT NOT NULL,
            valor REAL NOT NULL,
            status_code INTEGER NOT NULL,
            criado_em INTEGER NOT NULL
        )
    """)
  conn.commit()
  conn.close()


init_db()
app = FastAPI(title="Robo 2 - Web Scraping & Credit API")


class CreditarRequest(BaseModel):
  bot_key: str = Field(min_length=3)
  valor: float = Field(gt=0)
  payment_id: str = Field(min_length=3)
  origem: str = "pagamento_confirmado"


class ExtrairRequest(BaseModel):
  url: HttpUrl
  include_links: bool = False
  max_chars: int = 2500


def extrair_pagina(url: str, include_links: bool, max_chars: int) -> dict:
  headers = {"User-Agent": "Mozilla/5.0 (compatible; Robo2Scraper/1.0)"}
  resp = requests.get(url, headers=headers, timeout=15)
  resp.raise_for_status()
  html = resp.text

  extracted_json = trafilatura.extract(
      html,
      url=url,
      include_links=include_links,
      output_format="json",
      with_metadata=True,
  )

  title = ""
  clean_text = ""
  links = []

  if extracted_json:
    data = json.loads(extracted_json)
    title = data.get("title") or ""
    clean_text = (data.get("text") or "").strip()

  if not clean_text:
    soup = BeautifulSoup(html, "html.parser")
    if not title and soup.title and soup.title.string:
      title = soup.title.string.strip()
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript"]):
      tag.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    clean_text = "\n\n".join([p for p in paragraphs if p])

  if include_links:
    soup_links = BeautifulSoup(html, "html.parser")
    seen = set()
    for a in soup_links.find_all("a", href=True):
      full_url = urljoin(url, a["href"].strip())
      if full_url.startswith(("http://", "https://")) and full_url not in seen:
        seen.add(full_url)
        links.append(full_url)
      if len(links) >= 20:
        break

  if max_chars > 0 and len(clean_text) > max_chars:
    clean_text = clean_text[:max_chars]

  return {
      "status": "ok",
      "source_url": url,
      "title": title,
      "clean_text": clean_text,
      "excerpt": clean_text[:250].strip(),
      "links": links if include_links else [],
      "char_count": len(clean_text),
  }


@app.get("/health")
def health():
  return {"status": "ok", "service": "robo2"}


@app.post("/creditar")
def creditar(
    payload: CreditarRequest,
    x_admin_key: str | None = Header(default=None),
):
  if x_admin_key != ADMIN_KEY:
    raise HTTPException(status_code=401, detail="Chave administrativa invalida.")

  conn = get_conn()
  cur = conn.cursor()
  agora = int(time.time())

  try:
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        """
            INSERT OR IGNORE INTO bots (bot_key, saldo, ativo, criado_em)
            VALUES (?, 0.0, 1, ?)
        """,
        (payload.bot_key, agora),
    )

    cur.execute(
        "SELECT valor FROM creditos WHERE payment_id = ?", (payload.payment_id,)
    )
    if cur.fetchone():
      cur.execute("SELECT saldo FROM bots WHERE bot_key = ?", (payload.bot_key,))
      saldo_atual = float(cur.fetchone()["saldo"])
      conn.commit()
      return JSONResponse(
          status_code=200,
          content={
              "status": "already_credited",
              "payment_id": payload.payment_id,
              "bot_key": payload.bot_key,
              "saldo_atual": saldo_atual,
          },
      )

    cur.execute(
        """
            INSERT INTO creditos (payment_id, bot_key, valor, origem, criado_em)
            VALUES (?, ?, ?, ?, ?)
        """,
        (
            payload.payment_id,
            payload.bot_key,
            payload.valor,
            payload.origem,
            agora,
        ),
    )

    cur.execute(
        """
            UPDATE bots
            SET saldo = ROUND(saldo + ?, 4), ativo = 1
            WHERE bot_key = ?
        """,
        (payload.valor, payload.bot_key),
    )

    cur.execute("SELECT saldo FROM bots WHERE bot_key = ?", (payload.bot_key,))
    novo_saldo = float(cur.fetchone()["saldo"])
    conn.commit()

    return JSONResponse(
        status_code=200,
        content={
            "status": "credited",
            "payment_id": payload.payment_id,
            "bot_key": payload.bot_key,
            "valor_creditado": payload.valor,
            "saldo_atual": novo_saldo,
        },
    )
  except Exception as e:
    conn.rollback()
    raise HTTPException(
        status_code=500, detail=f"Erro ao creditar saldo: {str(e)}"
    )
  finally:
    conn.close()


@app.post("/extrair")
def extrair(
    payload: ExtrairRequest,
    x_bot_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
  conn = get_conn()
  cur = conn.cursor()

  try:
    if not x_bot_key:
      return JSONResponse(
          status_code=402,
          content={
              "status": "payment_required",
              "code": 402,
              "message": "Chave de bot ausente.",
          },
      )

    cur.execute("BEGIN IMMEDIATE")

    if x_request_id:
      cur.execute(
          "SELECT status_code FROM transacoes WHERE request_id = ?",
          (x_request_id,),
      )
      existente = cur.fetchone()
      if existente:
        cur.execute("SELECT saldo FROM bots WHERE bot_key = ?", (x_bot_key,))
        row_s = cur.fetchone()
        saldo_atual = float(row_s["saldo"]) if row_s else 0.0
        conn.commit()
        return JSONResponse(
            status_code=int(existente["status_code"]),
            content={
                "status": "duplicate_ignored",
                "message": "Requisicao ja processada.",
                "saldo_atual": saldo_atual,
            },
        )

    cur.execute(
        "SELECT saldo, ativo FROM bots WHERE bot_key = ?", (x_bot_key,)
    )
    row = cur.fetchone()
    if not row or int(row["ativo"]) != 1:
      conn.commit()
      return JSONResponse(
          status_code=402,
          content={
              "status": "payment_required",
              "code": 402,
              "message": "Chave invalida, inativa ou sem credito.",
              "saldo_atual": 0.0,
          },
      )

    saldo_atual = float(row["saldo"])
    if saldo_atual < CUSTO_POR_CHAMADA:
      if x_request_id:
        cur.execute(
            """
                    INSERT OR IGNORE INTO transacoes (request_id, bot_key, valor, status_code, criado_em)
                    VALUES (?, ?, 0.0, 402, ?)
                """,
            (x_request_id, x_bot_key, int(time.time())),
        )
      conn.commit()
      return JSONResponse(
          status_code=402,
          content={
              "status": "payment_required",
              "code": 402,
              "message": "Saldo insuficiente.",
              "saldo_atual": saldo_atual,
          },
      )

    res = extrair_pagina(str(payload.url), payload.include_links, payload.max_chars)
    novo_saldo = round(saldo_atual - CUSTO_POR_CHAMADA, 4)

    cur.execute(
        "UPDATE bots SET saldo = ? WHERE bot_key = ?", (novo_saldo, x_bot_key)
    )
    if x_request_id:
      cur.execute(
          """
                INSERT OR IGNORE INTO transacoes (request_id, bot_key, valor, status_code, criado_em)
                VALUES (?, ?, ?, 200, ?)
            """,
          (x_request_id, x_bot_key, CUSTO_POR_CHAMADA, int(time.time())),
      )
    conn.commit()

    res["saldo_restante"] = novo_saldo
    return JSONResponse(status_code=200, content=res)

  except requests.RequestException as e:
    conn.rollback()
    raise HTTPException(
        status_code=502, detail=f"Erro ao acessar URL: {str(e)}"
    )
  except Exception as e:
    conn.rollback()
    raise HTTPException(
        status_code=500, detail=f"Erro interno: {str(e)}"
    )
  finally:
    conn.close()


@app.get("/saldo/{bot_key}")
def consultar_saldo(bot_key: str, x_admin_key: str | None = Header(default=None)):
  if x_admin_key != ADMIN_KEY:
    raise HTTPException(status_code=401, detail="Chave administrativa invalida.")
  conn = get_conn()
  cur = conn.cursor()
  cur.execute("SELECT saldo, ativo FROM bots WHERE bot_key = ?", (bot_key,))
  row = cur.fetchone()
  conn.close()
  if not row:
    return {"bot_key": bot_key, "saldo": 0.0, "ativo": 0}
  return {
      "bot_key": bot_key,
      "saldo": float(row["saldo"]),
      "ativo": int(row["ativo"]),
  }


if __name__ == "__main__":
  import uvicorn

  uvicorn.run("main:app", host="0.0.0.0", port=PORT)
