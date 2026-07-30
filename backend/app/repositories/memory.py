"""Repositories en mémoire (MVP / tests). Thread-safe suffisant pour un process unique."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.entities import StoredSignal, Tenant, User
from app.backtest.schemas import BacktestReport


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class UserRepository:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._by_email: dict[str, str] = {}

    def create(self, *, tenant_id: str, email: str, password_hash: str, full_name: str | None) -> User:
        email = email.lower()
        if email in self._by_email:
            raise ValueError("email déjà utilisé")
        user = User(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
        )
        self._users[user.id] = user
        self._by_email[email] = user.id
        return user

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def get_by_email(self, email: str) -> User | None:
        uid = self._by_email.get(email.lower())
        return self._users.get(uid) if uid else None

    def update(self, user: User) -> User:
        self._users[user.id] = user
        return user

    def list_by_tenant(self, tenant_id: str) -> list[User]:
        return [u for u in self._users.values() if u.tenant_id == tenant_id]

    def list_all(self) -> list[User]:
        return list(self._users.values())


class TenantRepository:
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}

    def create(self, name: str, plan: str = "free") -> Tenant:
        tenant = Tenant(id=str(uuid.uuid4()), name=name, plan=plan)
        self._tenants[tenant.id] = tenant
        return tenant

    def get(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def update(self, tenant: Tenant) -> Tenant:
        self._tenants[tenant.id] = tenant
        return tenant


class SignalRepository:
    def __init__(self) -> None:
        self._signals: dict[str, StoredSignal] = {}

    def add(self, tenant_id: str, payload: dict) -> StoredSignal:
        sig = StoredSignal(id=str(uuid.uuid4()), tenant_id=tenant_id, payload=payload)
        self._signals[sig.id] = sig
        return sig

    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[StoredSignal]:
        items = [s for s in self._signals.values() if s.tenant_id == tenant_id]
        items.sort(key=lambda s: s.created_at, reverse=True)
        return items[:limit]

    def get(self, signal_id: str) -> StoredSignal | None:
        return self._signals.get(signal_id)

    def clear_for_tenant(self, tenant_id: str) -> int:
        ids = [sid for sid, s in self._signals.items() if s.tenant_id == tenant_id]
        for sid in ids:
            del self._signals[sid]
        return len(ids)

class BacktestRepository:
    def __init__(self) -> None:
        self._reports: dict[str, BacktestReport] = {}

    def save_report(self, report: BacktestReport) -> None:
        self._reports[report.id] = report

    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[BacktestReport]:
        items = [r for r in self._reports.values() if r.tenant_id == tenant_id]
        items.sort(key=lambda r: r.created_at, reverse=True)
        return items[:limit]


class JournalRepository:
    """Journal d'apprentissage en mémoire (entrées d'issue de signaux par agent)."""

    def __init__(self) -> None:
        self._entries: dict[str, list[dict]] = {}

    def add(self, tenant_id: str, entry: dict) -> dict:
        entry.setdefault("id", str(uuid.uuid4()))
        entry.setdefault("created_at", _now_iso())
        self._entries.setdefault(tenant_id, []).append(entry)
        return entry

    def list_for_tenant(self, tenant_id: str, limit: int = 200) -> list[dict]:
        return list(reversed(self._entries.get(tenant_id, [])))[:limit]

    def get(self, tenant_id: str, entry_id: str) -> dict | None:
        return next((e for e in self._entries.get(tenant_id, []) if e.get("id") == entry_id), None)

    def update_outcome(self, tenant_id: str, entry_id: str, *, outcome: str, pnl: float | None) -> dict | None:
        entry = self.get(tenant_id, entry_id)
        if entry is not None:
            entry["outcome"] = outcome
            entry["pnl"] = pnl
        return entry

    def clear_for_tenant(self, tenant_id: str) -> int:
        """Repart d'un journal propre (Phase A : le thermomètre doit être fiable)."""
        n = len(self._entries.get(tenant_id, []))
        self._entries[tenant_id] = []
        return n


class RecordRepository:
    """Document store générique en mémoire (Phase 4). Clé = (kind, id)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}

    def put(self, kind: str, record_id: str, payload: dict, tenant_id: str | None = None) -> dict:
        rec = {**payload, "id": record_id, "tenant_id": tenant_id}
        rec.setdefault("created_at", _now_iso())
        self._records[(kind, record_id)] = rec
        return rec

    def get(self, kind: str, record_id: str) -> dict | None:
        return self._records.get((kind, record_id))

    def list(self, kind: str, tenant_id: str | None = None) -> list[dict]:
        items = [v for (k, _), v in self._records.items() if k == kind]
        if tenant_id is not None:
            items = [v for v in items if v.get("tenant_id") == tenant_id]
        return sorted(items, key=lambda r: r.get("created_at", ""), reverse=True)

    def list_where_field_not_in(
        self, kind: str, field: str, excluded: set[str], tenant_id: str | None = None,
    ) -> list[dict]:
        """Enregistrements dont `field` n'est PAS dans `excluded` (ex. positions encore ouvertes).

        En mémoire, le filtre coûte le même prix qu'ailleurs — l'intérêt de cette méthode est côté
        SQL, où elle évite de matérialiser tout l'historique pour n'en garder qu'une poignée. Les
        deux implémentations doivent rendre le même résultat, c'est le contrat du Protocol.
        """
        return [r for r in self.list(kind, tenant_id) if r.get(field) not in excluded]

    def delete(self, kind: str, record_id: str) -> bool:
        return self._records.pop((kind, record_id), None) is not None
