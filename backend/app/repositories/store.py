"""Conteneur de dépendances applicatives (repositories partagés).

Choisit l'implémentation selon la configuration :
- `use_in_memory_db=true`  -> repositories en mémoire (MVP / tests, sans Postgres)
- `use_in_memory_db=false` -> repositories SQL (SQLAlchemy, Postgres en prod)

L'interface est identique dans les deux cas : le reste du code est agnostique du moteur.
Architecture 100% synchrone (cohérente Phases 0-2).
"""

from __future__ import annotations

from typing import Protocol

from app.backtest.schemas import BacktestReport
from app.core.config import get_settings
from app.models.entities import StoredSignal, Tenant, User


class _Users(Protocol):
    def create(self, *, tenant_id: str, email: str, password_hash: str, full_name: str | None) -> User: ...
    def get(self, user_id: str) -> User | None: ...
    def get_by_email(self, email: str) -> User | None: ...
    def update(self, user: User) -> User: ...
    def list_by_tenant(self, tenant_id: str) -> list[User]: ...
    def list_all(self) -> list[User]: ...


class _Tenants(Protocol):
    def create(self, name: str, plan: str = "free") -> Tenant: ...
    def get(self, tenant_id: str) -> Tenant | None: ...
    def update(self, tenant: Tenant) -> Tenant: ...


class _Signals(Protocol):
    def add(self, tenant_id: str, payload: dict) -> StoredSignal: ...
    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[StoredSignal]: ...
    def get(self, signal_id: str) -> StoredSignal | None: ...
    def clear_for_tenant(self, tenant_id: str) -> int: ...


class _Market(Protocol):
    def upsert_ohlcv(self, symbol: str, timeframe: str, rows: list[dict]) -> int: ...
    def count(self, symbol: str, timeframe: str) -> int: ...


class _Backtests(Protocol):
    def save_report(self, report: BacktestReport) -> None: ...
    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[BacktestReport]: ...


class _Journal(Protocol):
    def add(self, tenant_id: str, entry: dict) -> dict: ...
    def list_for_tenant(self, tenant_id: str, limit: int = 200) -> list[dict]: ...
    def get(self, tenant_id: str, entry_id: str) -> dict | None: ...
    def update_outcome(self, tenant_id: str, entry_id: str, *, outcome: str, pnl: float | None) -> dict | None: ...
    def clear_for_tenant(self, tenant_id: str) -> int: ...


class _Records(Protocol):
    def put(self, kind: str, record_id: str, payload: dict, tenant_id: str | None = None) -> dict: ...
    def get(self, kind: str, record_id: str) -> dict | None: ...
    def list(self, kind: str, tenant_id: str | None = None) -> list[dict]: ...
    def delete(self, kind: str, record_id: str) -> bool: ...
    def list_where_field_not_in(
        self, kind: str, field: str, excluded: set[str], tenant_id: str | None = None,
    ) -> list[dict]: ...


class AppStore:
    users: _Users
    tenants: _Tenants
    signals: _Signals
    market: _Market
    backtests: _Backtests
    journal: _Journal
    records: _Records

    def __init__(self) -> None:
        settings = get_settings()
        if settings.use_in_memory_db:
            from app.repositories.memory import (
                BacktestRepository,
                JournalRepository,
                RecordRepository,
                SignalRepository,
                TenantRepository,
                UserRepository,
            )
            from app.repositories.sql import NoopMarketRepository

            self.users = UserRepository()
            self.tenants = TenantRepository()
            self.signals = SignalRepository()
            self.market = NoopMarketRepository()
            self.backtests = BacktestRepository()
            self.journal = JournalRepository()
            self.records = RecordRepository()
        else:
            from app.repositories.sql import (
                SqlBacktestRepository,
                SqlJournalRepository,
                SqlMarketRepository,
                SqlRecordRepository,
                SqlSignalRepository,
                SqlTenantRepository,
                SqlUserRepository,
                make_engine_sessionmaker,
            )

            _, sm = make_engine_sessionmaker(settings.database_url_sync)
            self.users = SqlUserRepository(sm)
            self.tenants = SqlTenantRepository(sm)
            self.signals = SqlSignalRepository(sm)
            self.market = SqlMarketRepository(sm)
            self.backtests = SqlBacktestRepository(sm)
            self.journal = SqlJournalRepository(sm)
            self.records = SqlRecordRepository(sm)


_store: AppStore | None = None


def get_store() -> AppStore:
    global _store
    if _store is None:
        _store = AppStore()
    return _store


def reset_store() -> None:
    """Réinitialise l'état (utilisé par les tests)."""
    global _store
    _store = AppStore()


def set_store(store: AppStore) -> None:
    """Injecte un store spécifique (tests de la couche SQL)."""
    global _store
    _store = store
