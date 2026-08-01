"""Modèles SQLAlchemy (production, Postgres+TimescaleDB).

Définis pour la prod (use_in_memory_db=false). Le MVP/les tests utilisent les repositories
en mémoire. Le schéma SQL de référence reste `infra/db/init.sql`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantORM(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plan: Mapped[str] = mapped_column(String, default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserORM(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_profile: Mapped[str] = mapped_column(String, default="moderate")
    capital: Mapped[float] = mapped_column(Float, default=10000.0)
    # Watchlist sérialisée en JSON (portable Postgres/SQLite).
    watchlist: Mapped[str] = mapped_column(Text, default="[]")
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)
    max_exposure_pct: Mapped[float] = mapped_column(Float, default=50.0)
    max_daily_signals: Mapped[int] = mapped_column(Integer, default=50)
    daily_loss_limit_pct: Mapped[float] = mapped_column(Float, default=5.0)
    alert_email: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    alert_webhook: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_url: Mapped[str | None] = mapped_column(String, nullable=True)
    alert_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    push_token: Mapped[str | None] = mapped_column(String, nullable=True)
    locale: Mapped[str] = mapped_column(String, default="fr")
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditORM(Base):
    """Journal d'audit de sécurité (événements sensibles)."""

    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    actor: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OhlcvORM(Base):
    """Série temporelle OHLCV (hypertable TimescaleDB en prod, table simple en SQLite/test)."""

    __tablename__ = "ohlcv"
    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    timeframe: Mapped[str] = mapped_column(String, primary_key=True)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class JournalEntryORM(Base):
    """Mémoire d'apprentissage : issue d'un signal (gagnant/perdant) par agent."""

    __tablename__ = "journal_entries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    signal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    symbol: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)  # win | loss | open
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    agent_scores: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {agent: score}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BacktestRunRow(Base):
    """Résultat global d'un backtest de stratégie."""

    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    timeframe: Mapped[str] = mapped_column(String)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_capital: Mapped[float] = mapped_column(Float)
    params: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON config
    metrics: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON KPI
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class BacktestTradeRow(Base):
    """Trade individuel exécuté lors d'un backtest."""

    __tablename__ = "backtest_trades"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    backtest_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float)
    pnl_pct: Mapped[float] = mapped_column(Float)
    duration_minutes: Mapped[float] = mapped_column(Float)

class BacktestEquityRow(Base):
    """Point temporel de la courbe d'équité d'un backtest."""

    __tablename__ = "backtest_equity_points"
    backtest_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    equity: Mapped[float] = mapped_column(Float)
    drawdown_pct: Mapped[float] = mapped_column(Float)

class AgentRunLogRow(Base):
    """Journal d'exécution détaillée d'un agent."""

    __tablename__ = "agent_run_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    agent_name: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    latency_ms: Mapped[float] = mapped_column(Float)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    input_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON AgentOutput
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecordRow(Base):
    """Document générique (clé/valeur JSON) — Phase 4.

    Porte les collections Phase 4 (connexions broker, ordres, suivis copy-trading, annonces
    marketplace, achats, clés API dev) sans multiplier les tables. `kind` discrimine la collection ;
    `payload` est un JSON applicatif. `tenant_id` nullable pour les enregistrements globaux
    (ex. annonces marketplace visibles par tous).
    """

    __tablename__ = "records"
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())



class SignalORM(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    direction: Mapped[str] = mapped_column(String)
    # NULLABLES depuis la migration 0007 : une décision « pas de trade » (HOLD, ou trade bloqué par
    # les filtres) n'a pas de niveaux. On y écrivait auparavant le prix courant faute de mieux, ce
    # qui affichait « Entrée = Stop = TP » et un R/R de 0 — une absence déguisée en plan de trade.
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_1: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_2: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_3: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer)
    timeframe: Mapped[str | None] = mapped_column(String, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Payload complet (agents, news, métriques, mtf…) en JSON -> prédiction consultable en détail.
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
