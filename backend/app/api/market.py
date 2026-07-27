"""Routes de données marché : OHLCV pour le graphique (authentifié)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.core.deps import current_user, store_dep
from app.data import symbols as symbols_catalog
from app.data.heatmap import get_heatmap
from app.data.ohlcv import get_ohlcv
from app.models.entities import User
from app.models.signal import Timeframe
from app.repositories.store import AppStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])

_TF_INTERVAL = {
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
}


@router.get("/ohlcv")
async def ohlcv(
    asset: str = Query("BTC/USDT"),
    timeframe: Timeframe = Query(Timeframe.H1),
    limit: int = Query(200, ge=20, le=500),
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Bougies d'un actif. La réponse porte sa SOURCE : le graphique ne peut pas afficher des
    données inventées en les faisant passer pour du marché réel."""
    from app.data.ohlcv import get_ohlcv_with_source

    interval = _TF_INTERVAL.get(timeframe, "1h")
    payload = await get_ohlcv_with_source(asset, interval=interval, limit=limit)
    # Ingestion best-effort vers TimescaleDB (no-op en mode in-memory) — jamais de données fictives.
    if payload["real"]:
        try:
            store.market.upsert_ohlcv(asset, interval, payload["candles"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ingestion OHLCV échouée (%s)", exc)
    return payload


@router.get("/regime")
async def market_regime(_user: User = Depends(current_user)) -> dict:
    """Régime du jour : sessions ouvertes + VIX (risk-on/off) + taux/inflation — le brief du matin."""
    from app.data import macro as macro_mod
    from app.data import sessions as sessions_mod

    overview = sessions_mod.overview()
    macro = await macro_mod.fetch_macro_data()
    vix = macro.get("vix")
    if vix is None:
        label, tone = "régime indéterminé", "neutral"
    elif vix < 15:
        label, tone = f"Risk-ON — marché calme (VIX {vix})", "on"
    elif vix < 25:
        label, tone = f"Neutre (VIX {vix})", "neutral"
    else:
        label, tone = f"Risk-OFF — stress élevé (VIX {vix})", "off"
    return {
        "utc_time": overview["utc_time"],
        "sessions": overview["sessions"],
        "open_sessions": [s["label"] for s in overview["sessions"] if s["open"]],
        "vix": vix, "regime": tone, "regime_label": label,
        "rate_trend": macro.get("rate_trend"), "inflation": macro.get("inflation"),
    }


@router.get("/data-source")
async def data_source(
    asset: str = Query("BTC/USDT"),
    timeframe: Timeframe = Query(Timeframe.H1),
    _user: User = Depends(current_user),
) -> dict:
    """Qualité des données de `asset` : réelles (live/REST) ou synthétiques (démo).

    Charge les bougies pour rafraîchir la source, puis renvoie le verdict. Permet d'afficher un
    badge « données réelles / démo » et d'éviter de trader sur des données factices.
    """
    from app.data import markets as markets_mod

    interval = _TF_INTERVAL.get(timeframe, "1h")
    await markets_mod.load_candles(asset, interval=interval, limit=60)
    source = markets_mod.data_source(asset)
    labels = {"live": "Temps réel", "real": "Données réelles", "synthetic": "Démo (synthétique)"}
    return {"asset": asset, "source": source, "real": markets_mod.is_real(asset), "label": labels.get(source, source)}


@router.get("/stream-status")
async def stream_status(_user: User = Depends(current_user)) -> dict:
    """État de l'ingestion temps réel (paires streamées + fraîcheur du flux WebSocket)."""
    from app.realtime import market_stream

    return market_stream.status()


@router.get("/heatmap")
async def heatmap(
    mix: bool = Query(default=False, description="Mélange multi-marchés (crypto+forex+actions)"),
    user: User = Depends(current_user),
) -> list[dict]:
    """Variation 24h : watchlist par défaut, ou un panel multi-marchés si mix=true."""
    if mix:
        cat = symbols_catalog.catalog()
        symbols = cat["crypto"][:6] + cat["forex"][:5] + cat["stock"][:6]
    else:
        symbols = user.watchlist or ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    return await get_heatmap(symbols)


@router.get("/sessions")
async def sessions(_user: User = Depends(current_user)) -> dict:
    """Sessions de trading mondiales (Asie/Londres/New York) + celle(s) actuellement ouverte(s)."""
    from app.data import sessions as sessions_mod

    return sessions_mod.overview()


@router.get("/symbols")
async def list_symbols(
    q: str | None = Query(default=None, description="Filtre texte (ex. BTC, EUR, AAPL)"),
    asset_class: str | None = Query(default=None, description="crypto | forex | stock"),
    session: str | None = Query(default=None, description="asian | london | newyork"),
    _user: User = Depends(current_user),
) -> dict:
    """Catalogue multi-marchés (crypto / forex / actions) + recherche, filtrable par session."""
    if session:
        from app.data import sessions as sessions_mod
        results = sessions_mod.session_universe(session)
        if asset_class:
            results = [r for r in results if r["asset_class"] == asset_class]
        if q:
            results = [r for r in results if q.strip().upper() in r["symbol"].upper()]
    else:
        results = symbols_catalog.search(q, asset_class, limit=100)
    return {"results": results, "classes": list(symbols_catalog.catalog().keys())}
