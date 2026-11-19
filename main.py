import asyncio
import logging
from fastapi import FastAPI
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Monitor")

app = FastAPI(title="Robo 2 - Monitor de Mercado")

TARGETS = [
    {"id": "crypto_btc", "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"},
    {"id": "tech_news", "url": "https://news.ycombinator.com"}
]

async def monitor_logic():
    async with httpx.AsyncClient() as client:
        while True:
            logger.info("A iniciar ciclo de monitorização...")
            for target in TARGETS:
                try:
                    response = await client.get(target["url"], timeout=10)
                    logger.info(f"Alvo {target['id']}: Estado {response.status_code}")
                except Exception as e:
                    logger.error(f"Erro ao aceder a {target['id']}: {str(e)}")
            await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_logic())

@app.get("/health")
def health_check():
    return {"status": "monitoring_active"}
