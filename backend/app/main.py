"""Point d'entrée de l'API FastAPI Quantum Trade AI."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    audit,
    auth,
    billing,
    health,
    market,
    onboarding,
    portfolio,
    risk,
    settings as settings_api,
    signals,
    ws,
    backtest,
    agents,
    plan,
    copilot,
    journal,
    team,
    execution,
    kyc,
    copytrading,
    marketplace,
    i18n,
    branding,
    strategies,
    wallet,
)
from app.core.config import get_settings
from app.core.observability import ObservabilityMiddleware, init_sentry
from app.core.ratelimit import RateLimitMiddleware

settings = get_settings()
logging.basicConfig(level=settings.log_level)
init_sentry()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Cycle de vie : init persistance + bus Redis + scheduler quotidien des trades fiables."""
    import asyncio

    from app.realtime import bus
    from app.repositories.store import get_store

    get_store()
    await bus.init_bus()

    # Durcissement : alerte si le secret JWT est celui par défaut (critique hors dev).
    s = get_settings()
    if "change-me" in (s.secret_key or ""):
        logging.getLogger(__name__).warning(
            "⚠️ SECRET_KEY par défaut détecté — générez un secret fort avant toute mise en production "
            "(openssl rand -hex 32) et réduisez ACCESS_TOKEN_EXPIRE_MINUTES à 60."
        )

    # Ingestion temps réel (WebSocket Binance) : chauffe le cache + pousse les prix en live.
    if get_settings().live_ingestion_enabled:
        from app.realtime import market_stream

        await market_stream.start()

    daily_task = None
    if get_settings().daily_digest_enabled:
        from app.services.scheduler import daily_loop

        daily_task = asyncio.create_task(daily_loop())

    positions_task = None
    if get_settings().position_monitor_enabled:
        from app.services.scheduler import positions_loop

        positions_task = asyncio.create_task(positions_loop())

    learning_task = None
    if get_settings().learning_enabled:
        from app.services.scheduler import learning_loop

        learning_task = asyncio.create_task(learning_loop())

    alerts_task = None
    if get_settings().strategy_alerts_enabled:
        from app.services.scheduler import strategy_alerts_loop

        alerts_task = asyncio.create_task(strategy_alerts_loop())

    # Veille des sessions : ouverture Londres, ouverture New York et surtout leur chevauchement.
    session_task = None
    if get_settings().session_watch_enabled:
        from app.services.scheduler import session_watch_loop

        session_task = asyncio.create_task(session_watch_loop())

    edge_task = None
    if get_settings().edge_sweep_enabled:
        from app.services.scheduler import edge_sweep_loop

        edge_task = asyncio.create_task(edge_sweep_loop())

    logging.getLogger(__name__).info(
        "Démarrage OK (in_memory=%s, redis=%s, digest=%s)",
        get_settings().use_in_memory_db,
        bus.is_redis_enabled(),
        get_settings().daily_digest_enabled,
    )
    yield
    if daily_task:
        daily_task.cancel()
    if positions_task:
        positions_task.cancel()
    if learning_task:
        learning_task.cancel()
    if alerts_task:
        alerts_task.cancel()
    if session_task:
        session_task.cancel()
    if edge_task:
        edge_task.cancel()
    if get_settings().live_ingestion_enabled:
        from app.realtime import market_stream

        await market_stream.stop()
    await bus.shutdown_bus()


app = FastAPI(
    title="Quantum Trade AI API",
    version=__version__,
    description="API de la plateforme de trading multi-agents IA. Aide à la décision, pas un conseil en investissement.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(ObservabilityMiddleware)

# Routes
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(signals.router)
app.include_router(market.router)
app.include_router(portfolio.router)
app.include_router(risk.router)
app.include_router(settings_api.router)
app.include_router(billing.router)
app.include_router(audit.router)
app.include_router(ws.router)
app.include_router(backtest.router)
app.include_router(strategies.router)
app.include_router(wallet.router)
app.include_router(agents.router)
app.include_router(plan.router)
app.include_router(copilot.router)
app.include_router(journal.router)
app.include_router(team.router)
app.include_router(execution.router)
app.include_router(kyc.router)
app.include_router(copytrading.router)
app.include_router(marketplace.router)
app.include_router(i18n.router)
app.include_router(branding.router)


@app.get("/")
async def root() -> dict:
    return {"message": "Quantum Trade AI API", "docs": "/docs", "health": "/health"}
