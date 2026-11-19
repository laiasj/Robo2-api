from fastapi import FastAPI, Query
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
import json

app = FastAPI(
    title="Robo2 Signals API",
    description="API de sinais e manchetes estruturadas em tempo real para agentes de IA (Tecnologia, Economia e Cripto).",
    version="1.1.0"
)

TARGETS = [
    {
        "id": "tech_theverge",
        "categoria": "tecnologia",
        "fonte": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml"
    },
    {
        "id": "tech_techcrunch",
        "categoria": "tecnologia",
        "fonte": "TechCrunch",
        "url": "https://techcrunch.com/feed/"
    },
    {
        "id": "econ_cnbc",
        "categoria": "economia",
        "fonte": "CNBC Top News",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"
    },
    {
        "id": "crypto_btc_usd",
        "categoria": "economia",
        "fonte": "Coinbase Spot BTC-USD",
        "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    }
]


def extrair_itens_feed(url: str, limite: int = 5):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/1.1)",
            "Accept": "*/*"
        }
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")

    if raw.lstrip().startswith("{"):
        data = json.loads(raw).get("data", {})
        return [{
            "titulo": f"{data.get('base', 'BTC')}-{data.get('currency', 'USD')}: {data.get('amount')}",
            "link": url,
            "tipo": "cotacao"
        }]

    root = ET.fromstring(raw)
    itens = []

    for item in root.findall(".//item")[:limite]:
        titulo = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if titulo:
            itens.append({
                "titulo": titulo,
                "link": link,
                "publicado_em": pub_date,
                "tipo": "manchete"
            })

    if not itens:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:limite]:
            titulo = (entry.findtext("atom:title", default="", namespaces=ns)).strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            updated = (entry.findtext("atom:updated", default="", namespaces=ns)).strip()
            if titulo:
                itens.append({
                    "titulo": titulo,
                    "link": link,
                    "publicado_em": updated,
                    "tipo": "manchete"
                })

    return itens


@app.get("/")
def root():
    return {
        "status": "online",
        "servico": "Robo2 Signals API",
        "rotas": ["/radar", "/radar/itens", "/openapi.json"]
    }


@app.get("/radar")
def verificar_radar():
    resultados = []
    for alvo in TARGETS:
        try:
            req = urllib.request.Request(
                alvo["url"],
                headers={"User-Agent": "Mozilla/5.0 (compatible; Robo2-Signals-API/1.1)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                conteudo = resp.read()
                resultados.append({
                    "id": alvo["id"],
                    "categoria": alvo["categoria"],
                    "status": "online",
                    "bytes": len(conteudo)
                })
        except Exception as e:
            resultados.append({
                "id": alvo["id"],
                "categoria": alvo["categoria"],
                "status": "erro",
                "detalhe": str(e)
            })
    return {
        "registrado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "resultados": resultados
    }


@app.get("/radar/itens", summary="Extrai manchetes e cotações estruturadas em tempo real")
def listar_itens_radar(
    categoria: str | None = Query(default=None, description="Filtrar por 'tecnologia' ou 'economia'"),
    limite_por_fonte: int = Query(default=5, ge=1, le=20, description="Quantidade de itens por fonte")
):
    alvos_filtrados = [
        t for t in TARGETS
        if not categoria or t["categoria"].lower() == categoria.lower()
    ]

    coleta = []
    total_geral = 0

    for alvo in alvos_filtrados:
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

    return {
        "registrado_em": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "filtro_categoria": categoria or "todas",
        "total_itens_retornados": total_geral,
        "fontes": coleta
    }
