"""Repositories SQL (SQLAlchemy synchrone) — bascule production (use_in_memory_db=false).

Interface strictement identique aux repositories en mémoire : ils manipulent et retournent les
mêmes dataclasses (`User`, `Tenant`, `StoredSignal`), de sorte que l'API et les services restent
agnostiques du moteur de persistance.

Compatible Postgres (psycopg2) et SQLite (utilisé pour les tests de la couche SQL).
"""

from __future__ import annotations

import json
import uuid

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.db import Base, OhlcvORM, SignalORM, TenantORM, UserORM, BacktestRunRow
from app.models.entities import StoredSignal, Tenant, User
from app.backtest.schemas import BacktestReport, BacktestConfig, BacktestMetrics, BacktestTrade, BacktestEquityPoint


def make_engine_sessionmaker(url: str):
    """Crée un engine + sessionmaker et garantit l'existence des tables."""
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, future=True)


# ---------- Mappings ORM <-> dataclasses ----------
def _user_to_entity(o: UserORM) -> User:
    return User(
        id=o.id,
        tenant_id=o.tenant_id,
        email=o.email,
        password_hash=o.password_hash,
        full_name=o.full_name,
        mfa_enabled=o.mfa_enabled,
        risk_profile=o.risk_profile,
        capital=o.capital,
        watchlist=json.loads(o.watchlist or "[]"),
        onboarded=o.onboarded,
        max_exposure_pct=o.max_exposure_pct,
        max_daily_signals=o.max_daily_signals,
        daily_loss_limit_pct=o.daily_loss_limit_pct,
        alert_email=o.alert_email,
        alert_telegram=o.alert_telegram,
        telegram_chat_id=o.telegram_chat_id,
        alert_webhook=o.alert_webhook,
        webhook_url=o.webhook_url,
        alert_sms=o.alert_sms,
        phone=o.phone,
        push_token=o.push_token,
        locale=getattr(o, "locale", "fr") or "fr",
        daily_digest=getattr(o, "daily_digest", False) or False,
        mfa_secret=o.mfa_secret,
    )


def _tenant_to_entity(o: TenantORM) -> Tenant:
    return Tenant(id=o.id, name=o.name, plan=o.plan)


class SqlUserRepository:
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def create(self, *, tenant_id: str, email: str, password_hash: str, full_name: str | None) -> User:
        email = email.lower()
        with self._sm() as s:
            if s.scalar(select(UserORM).where(UserORM.email == email)):
                raise ValueError("email déjà utilisé")
            o = UserORM(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                watchlist=json.dumps(["BTC/USDT"]),
            )
            s.add(o)
            s.commit()
            return _user_to_entity(o)

    def get(self, user_id: str) -> User | None:
        with self._sm() as s:
            o = s.get(UserORM, user_id)
            return _user_to_entity(o) if o else None

    def get_by_email(self, email: str) -> User | None:
        with self._sm() as s:
            o = s.scalar(select(UserORM).where(UserORM.email == email.lower()))
            return _user_to_entity(o) if o else None

    def list_by_tenant(self, tenant_id: str) -> list[User]:
        with self._sm() as s:
            rows = s.scalars(select(UserORM).where(UserORM.tenant_id == tenant_id)).all()
            return [_user_to_entity(o) for o in rows]

    def list_all(self) -> list[User]:
        with self._sm() as s:
            return [_user_to_entity(o) for o in s.scalars(select(UserORM)).all()]

    def update(self, user: User) -> User:
        with self._sm() as s:
            o = s.get(UserORM, user.id)
            if o is None:
                raise ValueError("utilisateur introuvable")
            o.risk_profile = user.risk_profile
            o.capital = user.capital
            o.watchlist = json.dumps(user.watchlist)
            o.onboarded = user.onboarded
            o.full_name = user.full_name
            o.mfa_enabled = user.mfa_enabled
            o.max_exposure_pct = user.max_exposure_pct
            o.max_daily_signals = user.max_daily_signals
            o.daily_loss_limit_pct = user.daily_loss_limit_pct
            o.alert_email = user.alert_email
            o.alert_telegram = user.alert_telegram
            o.telegram_chat_id = user.telegram_chat_id
            o.alert_webhook = user.alert_webhook
            o.webhook_url = user.webhook_url
            o.alert_sms = user.alert_sms
            o.phone = user.phone
            o.push_token = user.push_token
            o.locale = user.locale
            o.daily_digest = user.daily_digest
            o.mfa_secret = user.mfa_secret
            s.commit()
            return _user_to_entity(o)


class SqlTenantRepository:
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def create(self, name: str, plan: str = "free") -> Tenant:
        with self._sm() as s:
            o = TenantORM(id=str(uuid.uuid4()), name=name, plan=plan)
            s.add(o)
            s.commit()
            return _tenant_to_entity(o)

    def get(self, tenant_id: str) -> Tenant | None:
        with self._sm() as s:
            o = s.get(TenantORM, tenant_id)
            return _tenant_to_entity(o) if o else None

    def update(self, tenant: Tenant) -> Tenant:
        with self._sm() as s:
            o = s.get(TenantORM, tenant.id)
            if o is None:
                raise ValueError("tenant introuvable")
            o.plan = tenant.plan
            o.name = tenant.name
            s.commit()
            return _tenant_to_entity(o)


class SqlSignalRepository:
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def add(self, tenant_id: str, payload: dict) -> StoredSignal:
        with self._sm() as s:
            o = SignalORM(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                symbol=payload.get("asset", ""),
                direction=payload.get("direction", "HOLD"),
                entry=payload.get("entry", 0.0),
                stop_loss=payload.get("stop_loss", 0.0),
                take_profit_1=payload.get("take_profit_1"),
                take_profit_2=payload.get("take_profit_2"),
                take_profit_3=payload.get("take_profit_3"),
                risk_reward=payload.get("risk_reward"),
                confidence=payload.get("confidence", 0),
                timeframe=payload.get("timeframe"),
                rationale=payload.get("rationale"),
                position_value=payload.get("position_value"),
                # Champs riches (agents, news, métriques, mtf, consensus…) -> JSON complet, pour que
                # la prédiction soit consultable en détail (le "pourquoi" du BUY/SELL/HOLD).
                details=json.dumps({k: v for k, v in payload.items() if k not in _SIGNAL_COLUMNS}),
            )
            s.add(o)
            s.commit()
            return StoredSignal(id=o.id, tenant_id=tenant_id, payload=payload)

    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[StoredSignal]:
        with self._sm() as s:
            rows = s.scalars(
                select(SignalORM)
                .where(SignalORM.tenant_id == tenant_id)
                .order_by(SignalORM.created_at.desc())
                .limit(limit)
            ).all()
            return [StoredSignal(id=r.id, tenant_id=tenant_id, payload=_signal_payload(r)) for r in rows]

    def get(self, signal_id: str) -> StoredSignal | None:
        with self._sm() as s:
            r = s.get(SignalORM, signal_id)
            return StoredSignal(id=r.id, tenant_id=r.tenant_id, payload=_signal_payload(r)) if r else None

    def clear_for_tenant(self, tenant_id: str) -> int:
        from sqlalchemy import delete as _delete

        with self._sm() as s:
            res = s.execute(_delete(SignalORM).where(SignalORM.tenant_id == tenant_id))
            s.commit()
            return res.rowcount or 0


class SqlMarketRepository:
    """Persistance OHLCV (ingestion -> TimescaleDB en prod, table simple en test)."""

    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def upsert_ohlcv(self, symbol: str, timeframe: str, rows: list[dict]) -> int:
        """Insère/MAJ des bougies (idempotent sur la clé symbol+timeframe+time). Retourne le nb traité."""
        count = 0
        with self._sm() as s:
            for r in rows:
                t = r["time"]
                ts = datetime.fromtimestamp(t, UTC) if isinstance(t, (int, float)) else t
                s.merge(
                    OhlcvORM(
                        symbol=symbol,
                        timeframe=timeframe,
                        time=ts,
                        open=r["open"],
                        high=r["high"],
                        low=r["low"],
                        close=r["close"],
                        volume=r.get("volume", 0.0),
                    )
                )
                count += 1
            s.commit()
        return count

    def count(self, symbol: str, timeframe: str) -> int:
        from sqlalchemy import func

        with self._sm() as s:
            return s.scalar(
                select(func.count())
                .select_from(OhlcvORM)
                .where(OhlcvORM.symbol == symbol, OhlcvORM.timeframe == timeframe)
            ) or 0


class NoopMarketRepository:
    """Pas de persistance OHLCV en mode in-memory (pas de TimescaleDB)."""

    def upsert_ohlcv(self, symbol: str, timeframe: str, rows: list[dict]) -> int:
        return 0

    def count(self, symbol: str, timeframe: str) -> int:
        return 0

class SqlBacktestRepository:
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def save_report(self, report: BacktestReport) -> None:
        with self._sm() as s:
            o = BacktestRunRow(
                id=report.id,
                tenant_id=report.tenant_id,
                symbol=report.config.symbol,
                timeframe=report.config.timeframe,
                start_time=report.config.start_time,
                end_time=report.config.end_time,
                initial_capital=report.config.initial_capital,
                params=report.config.model_dump_json(),
                metrics=report.metrics.model_dump_json(),
                created_at=report.created_at
            )
            s.add(o)
            s.commit()
            
    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[BacktestReport]:
        with self._sm() as s:
            rows = s.scalars(
                select(BacktestRunRow)
                .where(BacktestRunRow.tenant_id == tenant_id)
                .order_by(BacktestRunRow.created_at.desc())
                .limit(limit)
            ).all()
            
            reports = []
            for r in rows:
                config = BacktestConfig.model_validate_json(r.params or "{}")
                metrics = BacktestMetrics.model_validate_json(r.metrics or "{}")
                reports.append(BacktestReport(
                    id=r.id,
                    tenant_id=r.tenant_id,
                    config=config,
                    metrics=metrics,
                    trades=[],
                    equity_curve=[],
                    created_at=r.created_at
                ))
            return reports


# Champs stockés en colonnes dédiées (le reste part dans `details` JSON).
_SIGNAL_COLUMNS = {
    "asset", "direction", "entry", "stop_loss", "take_profit_1", "take_profit_2",
    "take_profit_3", "risk_reward", "confidence", "timeframe", "rationale",
    "position_value", "created_at", "id", "tenant_id",
}


def _signal_payload(r: SignalORM) -> dict:
    # Fusion : champs riches JSON (agents, news, métriques, mtf…) + colonnes structurées.
    out: dict = {}
    if getattr(r, "details", None):
        try:
            out = json.loads(r.details)
        except (ValueError, TypeError):
            out = {}
    out.update({
        "asset": r.symbol,
        "direction": r.direction,
        "entry": r.entry,
        "stop_loss": r.stop_loss,
        "take_profit_1": r.take_profit_1,
        "take_profit_2": r.take_profit_2,
        "take_profit_3": r.take_profit_3,
        "risk_reward": r.risk_reward,
        "confidence": r.confidence,
        "timeframe": r.timeframe,
        "rationale": r.rationale,
        "position_value": r.position_value,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    })
    return out


class SqlJournalRepository:
    """Journal d'apprentissage persistant (SQL)."""

    def __init__(self, sm) -> None:  # noqa: ANN001
        self._sm = sm

    @staticmethod
    def _to_dict(r) -> dict:  # noqa: ANN001
        import json as _json

        return {
            "id": r.id,
            "signal_id": r.signal_id,
            "outcome": r.outcome,
            "direction": r.direction,
            "symbol": r.symbol,
            "pnl": r.pnl,
            "agent_scores": _json.loads(r.agent_scores or "{}"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    def add(self, tenant_id: str, entry: dict) -> dict:
        import json as _json
        import uuid as _uuid

        from app.models.db import JournalEntryORM

        entry_id = entry.get("id") or str(_uuid.uuid4())
        with self._sm() as s:
            s.add(
                JournalEntryORM(
                    id=entry_id,
                    tenant_id=tenant_id,
                    signal_id=entry.get("signal_id"),
                    symbol=entry.get("symbol", ""),
                    direction=entry.get("direction", ""),
                    outcome=entry.get("outcome", "open"),
                    pnl=entry.get("pnl"),
                    agent_scores=_json.dumps(entry.get("agent_scores") or {}),
                )
            )
            s.commit()
        entry["id"] = entry_id
        return entry

    def list_for_tenant(self, tenant_id: str, limit: int = 200) -> list[dict]:
        from sqlalchemy import select as _select

        from app.models.db import JournalEntryORM

        with self._sm() as s:
            rows = s.scalars(
                _select(JournalEntryORM)
                .where(JournalEntryORM.tenant_id == tenant_id)
                .order_by(JournalEntryORM.created_at.desc())
                .limit(limit)
            ).all()
            return [self._to_dict(r) for r in rows]

    def get(self, tenant_id: str, entry_id: str) -> dict | None:
        from sqlalchemy import select as _select

        from app.models.db import JournalEntryORM

        with self._sm() as s:
            r = s.scalars(
                _select(JournalEntryORM).where(
                    JournalEntryORM.tenant_id == tenant_id, JournalEntryORM.id == entry_id
                )
            ).first()
            return self._to_dict(r) if r else None

    def update_outcome(self, tenant_id: str, entry_id: str, *, outcome: str, pnl: float | None) -> dict | None:
        from sqlalchemy import select as _select

        from app.models.db import JournalEntryORM

        with self._sm() as s:
            r = s.scalars(
                _select(JournalEntryORM).where(
                    JournalEntryORM.tenant_id == tenant_id, JournalEntryORM.id == entry_id
                )
            ).first()
            if r is None:
                return None
            r.outcome = outcome
            r.pnl = pnl
            s.commit()
            return self._to_dict(r)

    def clear_for_tenant(self, tenant_id: str) -> int:
        """Repart d'un journal propre (Phase A : le thermomètre doit être fiable)."""
        from sqlalchemy import delete as _delete

        from app.models.db import JournalEntryORM

        with self._sm() as s:
            res = s.execute(_delete(JournalEntryORM).where(JournalEntryORM.tenant_id == tenant_id))
            s.commit()
            return res.rowcount or 0


class SqlRecordRepository:
    """Document store générique persistant (Phase 4). Clé = (kind, id)."""

    def __init__(self, sm) -> None:  # noqa: ANN001
        self._sm = sm

    @staticmethod
    def _to_dict(r) -> dict:  # noqa: ANN001
        data = json.loads(r.payload or "{}")
        data["id"] = r.id
        data["tenant_id"] = r.tenant_id
        if r.created_at:
            data.setdefault("created_at", r.created_at.isoformat())
        return data

    def put(self, kind: str, record_id: str, payload: dict, tenant_id: str | None = None) -> dict:
        from app.models.db import RecordRow

        clean = {k: v for k, v in payload.items() if k not in {"id", "tenant_id"}}
        with self._sm() as s:
            row = s.get(RecordRow, {"kind": kind, "id": record_id})
            if row is None:
                row = RecordRow(kind=kind, id=record_id, tenant_id=tenant_id, payload=json.dumps(clean))
                s.add(row)
            else:
                row.tenant_id = tenant_id
                row.payload = json.dumps(clean)
            s.commit()
            return self._to_dict(row)

    def get(self, kind: str, record_id: str) -> dict | None:
        from app.models.db import RecordRow

        with self._sm() as s:
            row = s.get(RecordRow, {"kind": kind, "id": record_id})
            return self._to_dict(row) if row else None

    def list(self, kind: str, tenant_id: str | None = None) -> list[dict]:
        from app.models.db import RecordRow

        with self._sm() as s:
            stmt = select(RecordRow).where(RecordRow.kind == kind)
            if tenant_id is not None:
                stmt = stmt.where(RecordRow.tenant_id == tenant_id)
            rows = s.scalars(stmt.order_by(RecordRow.created_at.desc())).all()
            return [self._to_dict(r) for r in rows]

    def list_where_field_not_in(
        self, kind: str, field: str, excluded: set[str], tenant_id: str | None = None,
    ) -> list[dict]:
        """Enregistrements dont `field` n'est PAS dans `excluded`, filtrés CÔTÉ BASE.

        Motif (mesuré le 30/07/2026) : `positions_loop` enchaîne quatre passages par minute et
        chacun appelait `list(ORDER)` sans filtre — donc chargeait puis désérialisait en JSON,
        en Python, TOUT l'historique d'ordres de TOUS les comptes pour n'en garder que les quelques
        positions encore ouvertes. Le coût croissait avec le nombre total de trades jamais passés,
        pas avec le nombre de positions actives : un ralentissement qui s'aggrave avec le temps.

        Le payload est un document JSON (colonne texte), donc le filtre s'exprime en SQL sur ce
        texte. On ne se contente PAS du filtre SQL : il sert à réduire le volume transféré, et le
        résultat est revérifié en Python (`_to_dict`) qui reste l'autorité sur le contenu. Un filtre
        textuel peut être trop LARGE (un `LIKE` peut matcher la même chaîne ailleurs dans le
        document) — jamais trop étroit, ce qui serait le seul cas dangereux ici.
        """
        from sqlalchemy import not_, or_

        from app.models.db import RecordRow

        with self._sm() as s:
            stmt = select(RecordRow).where(RecordRow.kind == kind)
            if tenant_id is not None:
                stmt = stmt.where(RecordRow.tenant_id == tenant_id)
            if excluded:
                # Un enregistrement est écarté si son payload contient `"field": "valeur"` pour
                # l'une des valeurs exclues. Les deux orthographes JSON (avec et sans espace après
                # le deux-points) sont couvertes : le sérialiseur peut changer, la requête ne doit
                # pas se mettre à mentir pour autant.
                patterns = [
                    f'%"{field}":{sep}"{value}"%'
                    for value in sorted(excluded)
                    for sep in ("", " ")
                ]
                stmt = stmt.where(not_(or_(*(RecordRow.payload.like(p) for p in patterns))))
            rows = s.scalars(stmt.order_by(RecordRow.created_at.desc())).all()
            # Revérification en Python : c'est elle qui fait foi, le SQL n'a fait que dégrossir.
            return [d for r in rows if (d := self._to_dict(r)).get(field) not in excluded]

    def delete(self, kind: str, record_id: str) -> bool:
        from app.models.db import RecordRow

        with self._sm() as s:
            row = s.get(RecordRow, {"kind": kind, "id": record_id})
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True
