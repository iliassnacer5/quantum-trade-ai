"""Connecteur Binance (M1) — crypto.

- `fetch_klines` : backfill historique via l'API REST publique (OHLCV).
- `stream_klines` : flux temps réel via WebSocket (reconnexion automatique).

Aucune clé requise pour les données publiques de marché. Dépendances optionnelles importées
paresseusement pour garder le cœur testable hors-ligne.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.domain.indicators import Candle

logger = logging.getLogger(__name__)

REST_URL = "https://api.binance.com/api/v3/klines"
WS_URL = "wss://stream.binance.com:9443/ws"

# Binance utilise des symboles sans slash : BTC/USDT -> BTCUSDT
def to_binance_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


async def fetch_klines(symbol: str, interval: str = "1h", limit: int = 200) -> list[Candle]:
    """Backfill OHLCV via REST. Retourne une liste de Candle (ancienne -> récente)."""
    import httpx

    from app.data.markets import _timeout

    params = {"symbol": to_binance_symbol(symbol), "interval": interval, "limit": limit}
    # Délai de CONNEXION court (cf. `markets._timeout`) : un fournisseur injoignable ne doit pas
    # immobiliser l'appelant 10 s pour rien.
    # Client partagé : 150 chargements crypto par cycle passent ici. Ouvrir une connexion neuve à
    # chaque fois produisait les `ConnectError` intermittents observés (cf. `data/http.py`).
    from app.data.http import client as shared_client

    client = await shared_client("binance", timeout=_timeout(limit))
    resp = await client.get(REST_URL, params=params)
    resp.raise_for_status()
    rows = resp.json()
    # Format Binance : [open_time, open, high, low, close, volume, ...]
    return [
        Candle(float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows
    ]


async def stream_klines(
    symbol: str, interval: str = "1h", *, max_retries: int = 0
) -> AsyncIterator[Candle]:
    """Flux WebSocket des bougies clôturées. Reconnexion automatique (max_retries=0 = infini)."""
    import json

    import websockets

    stream = f"{to_binance_symbol(symbol).lower()}@kline_{interval}"
    url = f"{WS_URL}/{stream}"
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                attempt = 0
                logger.info("WS connecté: %s", stream)
                async for raw in ws:
                    msg = json.loads(raw)
                    k = msg.get("k", {})
                    if k.get("x"):  # bougie clôturée uniquement
                        yield Candle(
                            float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])
                        )
        except Exception as exc:  # noqa: BLE001 — reconnexion
            attempt += 1
            logger.warning("WS interrompu (%s), reconnexion #%d", exc, attempt)
            if max_retries and attempt >= max_retries:
                raise
            await asyncio.sleep(min(30, 2**attempt))
