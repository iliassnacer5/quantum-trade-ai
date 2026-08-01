"""Endpoints de santé / liveness / readiness + métriques Prometheus (Phase 5)."""

import time

from fastapi import APIRouter, Response, status

from app import __version__
from app.core import metrics
from app.core.config import get_settings

router = APIRouter(tags=["health"])

_STARTED_AT = time.time()


@router.get("/health")
async def health() -> dict:
    """Liveness simple — confirme que le service répond."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "quantum-trade-ai-backend",
        "version": __version__,
        "environment": settings.environment,
    }


@router.get("/health/live")
async def live() -> dict:
    """Liveness probe (Kubernetes) — uptime du process."""
    return {"status": "alive", "uptime_seconds": round(time.time() - _STARTED_AT, 1)}


@router.get("/health/sources")
async def sources() -> dict:
    """ÉTAT DES SOURCES DE DONNÉES ET DE L'APPRENTISSAGE — la page à regarder chaque matin.

    Pensée pour une exploitation de plusieurs semaines sans surveillance : elle répond en un coup
    d'œil aux deux seules questions qui comptent alors — « les données arrivent-elles encore ? » et
    « les agents apprennent-ils toujours ? ».

    Elle ne DÉCLENCHE aucun chargement : elle rapporte la source du dernier chargement réel de
    chaque symbole témoin (`markets.data_source`). L'interroger ne consomme donc aucun quota, ce
    qui compte quand c'est précisément le quota qu'on surveille.
    """
    from datetime import UTC, datetime

    from app.data import markets
    from app.services import training_service

    s = get_settings()
    # Un témoin par classe d'actif : le but est de repérer une classe entière au tapis, pas de
    # dresser l'inventaire des 88 symboles.
    temoins = {"crypto": "BTC/USDT", "forex": "EUR/USD", "stock": "AAPL",
               "index": "GER40", "commodity": "XAU/USD"}
    couverture = {cls: markets.data_source(sym) for cls, sym in temoins.items()}

    entrainement = training_service.snapshot() or {}
    genere = entrainement.get("generated_at")
    age_h = None
    if genere:
        try:
            age_h = round((datetime.now(UTC) - datetime.fromisoformat(genere)).total_seconds() / 3600, 1)
        except (ValueError, TypeError):
            age_h = None

    return {
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        # Clés configurées — jamais leur valeur : un endpoint de santé ne divulgue pas de secret.
        "providers_configured": {
            "binance": True,                     # public, sans clé
            "yahoo": True,                       # public, sans clé
            "alpaca": bool(s.alpaca_api_key),
            "oanda": bool(s.oanda_api_key),
            "twelve_data": bool(s.twelve_data_api_key),
            "finnhub_news": bool(s.finnhub_api_key),
            "massive_news": bool(s.massive_news_key),
        },
        # Source du DERNIER chargement réel, par classe : "real" / "live" / "unavailable" /
        # "unknown" (jamais chargé depuis le démarrage).
        "last_source_by_class": couverture,
        "classes_unavailable": [c for c, v in couverture.items() if v == "unavailable"],
        "training": {
            "generated_at": genere,
            "age_hours": age_h,
            "symbols_trained": entrainement.get("symbols_trained"),
            "trades": entrainement.get("trades"),
            # Au-delà de 48 h sans entraînement publié, l'apprentissage a cessé de progresser.
            "stale": age_h is None or age_h > 48,
        },
        "synthetic_data_allowed": s.data_allow_synthetic,
        "trade_only_when_open": s.playbook_trade_only_when_open,
    }


@router.get("/health/ready")
async def ready(response: Response) -> dict:
    """Readiness probe (SLA) — vérifie les dépendances critiques (DB, Redis)."""
    from app.realtime import bus

    checks: dict[str, bool] = {}

    settings = get_settings()
    if settings.use_in_memory_db:
        checks["database"] = True
    else:
        try:
            from sqlalchemy import text

            from app.repositories.sql import make_engine_sessionmaker

            engine, _ = make_engine_sessionmaker(settings.database_url_sync)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:  # noqa: BLE001
            checks["database"] = False

    checks["redis"] = bus.is_redis_enabled() or settings.use_in_memory_db or True  # bus a un repli mémoire

    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "degraded", "checks": checks}


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Exposition Prometheus (à scraper par Prometheus/Grafana)."""
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")
