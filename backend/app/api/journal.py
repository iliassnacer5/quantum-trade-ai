"""Journal de trading & apprentissage (M9) — Phase 3.

Réservé au plan Pro+. Enregistrement auto des signaux (à la génération), clôture manuelle des trades
(issue + P&L), explication IA post-mortem, et exposition des multiplicateurs de pondération que le
Master applique (boucle de feedback).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import store_dep
from app.core.plans import require_feature
from app.models.entities import User
from app.repositories.store import AppStore
from app.services import journal_service

router = APIRouter(prefix="/api/journal", tags=["journal"])


class CloseRequest(BaseModel):
    outcome: str  # win | loss | breakeven
    pnl: float | None = None


@router.get("")
async def list_entries(
    user: User = Depends(require_feature("journal")),
    store: AppStore = Depends(store_dep),
) -> list[dict]:
    """TOUS les trades du tenant : signaux classiques ET positions playbook (démo), réunis.

    Auparavant limité aux signaux du bouton « Générer un signal » : un compte n'ayant utilisé que
    l'auto-entrée ou « Ouvrir en démo » voyait cette liste rester vide alors qu'il avait de vrais
    trades — ce n'était pas une panne, seulement deux flux qui ne partageaient rien.
    """
    return journal_service.all_entries(store, user.tenant_id, limit=200)


@router.delete("")
async def clear_journal(
    user: User = Depends(require_feature("journal")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Réinitialise le Journal (Phase A du plan maître) : repartir d'un thermomètre propre.

    À utiliser après des périodes de test — l'apprentissage et le track record ne doivent mesurer
    que des trades représentatifs de ta vraie configuration."""
    removed = store.journal.clear_for_tenant(user.tenant_id)
    return {"cleared": removed}


@router.get("/insights")
async def insights(
    user: User = Depends(require_feature("journal")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Statistiques + apprentissage : fiabilité par agent et volume de trades appris.

    Les STATS (trades clôturés, taux de réussite, P&L, ouverts) portent sur TOUS les trades —
    signaux classiques et playbook réunis, cf. `journal_service.all_entries` : c'est le nombre
    réel de trades du compte, pas seulement ceux du flux « Générer un signal ».

    La fiabilité PAR AGENT (`reliability`), elle, ne peut mesurer QUE les signaux classiques : eux
    seuls portent un score par agent au moment de l'ouverture (`agent_scores`) — les trades
    playbook n'ont pas cette décomposition, la stratégie n'étant pas un vote d'agents. Quand ce
    flux est vide mais que des trades PLAYBOOK existent, on expose à la place la compétence déjà
    mesurée par le walk-forward nocturne (`training_service`) plutôt que d'afficher un « pas
    encore assez de trades » trompeur alors que l'apprentissage a bien eu lieu, ailleurs.
    """
    from app.agents.journal import reliability_report

    all_rows = journal_service.all_entries(store, user.tenant_id, limit=500)
    signal_rows = [e for e in all_rows if e.get("source") != "playbook"]
    report = reliability_report(signal_rows)
    learned = sum(1 for e in signal_rows if e.get("outcome") in ("win", "loss"))

    out = {
        "stats": journal_service.stats(all_rows),
        "weight_multipliers": journal_service.compute_multipliers(store, user.tenant_id),
        "reliability": report,            # détail par agent (réussite, volume, multiplicateur)
        "trades_learned": learned,        # trades du flux SIGNAUX qui nourrissent ce calcul précis
        "reliability_source": "signals",
    }
    if not report:
        from app.services import training_service

        snap = training_service.snapshot()
        competence = (snap or {}).get("factor_competence") or {}
        has_playbook_trades = any(e.get("source") == "playbook" for e in all_rows)
        if competence and has_playbook_trades:
            out["reliability"] = [
                {"agent": k, "hit_rate": v.get("accuracy", 0.0), "samples": v.get("observations", 0),
                 "multiplier": 1.0, "low_sample": v.get("observations", 0) < 10}
                for k, v in competence.items()
            ]
            out["reliability_source"] = "training"
            out["trades_learned"] = snap.get("trades", 0)
    return out


@router.post("/auto-resolve")
async def auto_resolve(
    user: User = Depends(require_feature("journal")),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Force la résolution des signaux ouverts (sinon fait automatiquement en arrière-plan)."""
    resolved = await journal_service.auto_resolve(store, user.tenant_id)
    return {"resolved": resolved}


@router.post("/{entry_id}/close")
async def close_trade(
    entry_id: str,
    body: CloseRequest,
    user: User = Depends(require_feature("journal")),
    store: AppStore = Depends(store_dep),
) -> dict:
    try:
        updated = journal_service.close_trade(store, user.tenant_id, entry_id, body.outcome, body.pnl)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entrée de journal introuvable")
    return updated


@router.post("/{entry_id}/explain")
async def explain(
    entry_id: str,
    user: User = Depends(require_feature("journal")),
    store: AppStore = Depends(store_dep),
) -> dict:
    entry = store.journal.get(user.tenant_id, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entrée de journal introuvable")
    explanation = await journal_service.explain_trade(entry)
    return {"id": entry_id, "explanation": explanation}
