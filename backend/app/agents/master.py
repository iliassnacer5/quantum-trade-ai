"""Master Agent (M2) — orchestration & arbitrage avec pondération dynamique.

Reçoit les sorties des agents spécialisés, détecte les conflits, pondère dynamiquement (poids de
base × multiplicateurs d'apprentissage du Journal × ajustement selon le régime macro), applique la
contrainte de l'Agent Risque, puis produit une décision consolidée et explicable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.base import AgentOutput
from app.models.signal import Direction

# Poids de base par agent.
# Le PLAYBOOK (stratégie du desk : mensuel+journalier -> 4h -> entrée 15 min) domine : c'est lui
# qui porte la méthode, les autres agents nuancent. Il dispose en plus d'un droit de VETO.
DEFAULT_WEIGHTS = {
    "playbook": 0.35,
    "technical": 0.25,
    "volume": 0.15,
    "sentiment": 0.20,
    "pattern": 0.15,
    "fundamental": 0.15,
    "macro": 0.10,
}


@dataclass
class MasterDecision:
    direction: Direction
    score: float
    confidence: int
    rationale: str
    conflict: bool
    weights_used: dict
    consensus: int = 0
    playbook_veto: str | None = None  # motif du refus imposé par la stratégie, le cas échéant


def _effective_weights(
    base: dict[str, float],
    journal_multipliers: dict[str, float] | None,
    regime_bias: float,
) -> dict[str, float]:
    """Combine poids de base, apprentissage (journal) et régime macro."""
    jm = journal_multipliers or {}
    eff = {a: w * jm.get(a, 1.0) for a, w in base.items()}
    # En régime extrême, on accentue le poids macro et on tempère le technique pur.
    if abs(regime_bias) > 0.4:
        eff["macro"] = eff.get("macro", 0.15) * 1.3
        eff["technical"] = eff.get("technical", 0.3) * 0.85
    return eff


def decide(
    outputs: list[AgentOutput],
    *,
    weights: dict[str, float] | None = None,
    journal_multipliers: dict[str, float] | None = None,
    risk_output: AgentOutput | None = None,
    playbook_gate: bool = True,
) -> MasterDecision:
    base = weights or DEFAULT_WEIGHTS
    regime_bias = next((o.score for o in outputs if o.name == "macro"), 0.0)
    eff = _effective_weights(base, journal_multipliers, regime_bias)

    voting = [o for o in outputs if o.name != "risk"]
    # Anti-dilution : seuls les agents qui ont un VRAI signal directionnel votent. Les agents neutres
    # (ex. fundamental=0 en crypto, macro=0 sans données) s'abstiennent au lieu de tirer le score vers
    # HOLD. Si tous sont neutres, on garde tout le monde (le HOLD est alors légitime).
    active = [o for o in voting if abs(o.score) >= 0.05]
    pool = active if active else voting
    num = den = 0.0
    for o in pool:
        # Bonus ×1.3 pour un agent EXPERT marché (connaît les règles spécifiques de son marché).
        expert_bonus = 1.3 if (o.details or {}).get("expert") else 1.0
        w = eff.get(o.name, 0.1) * max(o.confidence, 0.05) * expert_bonus
        num += w * o.score
        den += w
    combined = num / den if den else 0.0

    dirs = {o.name: o.direction() for o in voting}
    conflict = any(d == Direction.BUY for d in dirs.values()) and any(
        d == Direction.SELL for d in dirs.values()
    )

    if combined > 0.12:
        direction = Direction.BUY
    elif combined < -0.12:
        direction = Direction.SELL
    else:
        direction = Direction.HOLD

    # --- Autorité du PLAYBOOK : la stratégie du desk prime sur le vote des agents ---
    # Le playbook a un droit de veto ; et aucun trade ne peut partir DANS L'AUTRE SENS que le sien.
    # (Neutralisé si le playbook n'avait pas assez de données -> comportement historique préservé.)
    pb = next((o for o in outputs if o.name == "playbook"), None)
    pb_details = (pb.details or {}) if pb is not None else {}
    playbook_veto: str | None = None
    if playbook_gate and pb is not None and not pb_details.get("insufficient"):
        pb_dir = pb_details.get("direction")
        if pb_details.get("veto"):
            reasons = pb_details.get("reasons") or []
            playbook_veto = "; ".join(reasons) or "conditions de la stratégie non réunies"
            direction = Direction.HOLD
        elif pb_dir in ("BUY", "SELL") and direction.value != pb_dir:
            playbook_veto = (
                f"les autres agents contredisent le playbook ({pb_dir}) — on ne trade pas contre la méthode"
            )
            direction = Direction.HOLD

    # --- Confiance recalibrée : force du signal × consensus pondéré des agents ---
    sign = 1 if combined > 0 else -1 if combined < 0 else 0
    agree_w = sum(
        eff.get(o.name, 0.1) * max(o.confidence, 0.05)
        for o in pool
        if o.score != 0 and (o.score > 0) == (sign > 0)
    )
    agreement = (agree_w / den) if den and sign != 0 else 0.0
    strength = min(1.0, abs(combined) / 0.4)  # combiné ±0.4 => force maximale
    confidence = (0.55 * strength + 0.45 * agreement) * 100

    # Bonus de tendance : un ADX élevé (tendance confirmée) renforce la conviction directionnelle.
    adx = next((o.details.get("adx") for o in voting if o.name == "technical" and o.details.get("adx")), None)
    if adx and direction != Direction.HOLD:
        if adx > 25:
            confidence += 8
        elif adx < 18:
            confidence -= 6  # marché en range : on tempère

    if conflict:
        confidence *= 0.85
    # Contrainte de l'Agent Risque : sa confidence < 1 réduit la confiance globale.
    if risk_output is not None:
        confidence *= max(0.4, risk_output.confidence)
    # Timing de session : la stratégie privilégie l'ouverture de Londres, celle de New York et
    # surtout leur chevauchement. Hors de ces fenêtres, la conviction est mécaniquement réduite.
    session = pb_details.get("session") or {}
    if session.get("quality") is not None:
        confidence *= 0.75 + 0.25 * float(session["quality"])
    confidence = int(round(max(0, min(100, confidence))))
    if direction == Direction.HOLD:
        confidence = min(confidence, 45)

    # --- Rationale experte : synthèse claire puis détail par agent ---
    consensus_pct = int(round(agreement * 100))
    arb = (
        f"Arbitrage Master — Décision : {direction.value} | score {combined:+.2f} | "
        f"consensus {consensus_pct}%"
    )
    if adx:
        arb += f" | ADX {adx:.0f}"
    if session.get("label"):
        arb += f" | session : {session['label']}"
    arb += "."
    if playbook_veto:
        arb += f" ⛔ Refus imposé par la stratégie (playbook) : {playbook_veto}."
    if conflict:
        arb += " ⚠️ Signaux divergents : pondération prudente."
    if risk_output is not None and risk_output.details.get("penalty", 0) > 0:
        arb += f" Contrainte risque (pénalité {risk_output.details['penalty']})."
    parts = [o.rationale for o in outputs]
    rationale = arb + "\n• " + "\n• ".join(parts)

    return MasterDecision(
        direction=direction,
        score=round(combined, 3),
        confidence=confidence,
        rationale=rationale,
        conflict=conflict,
        weights_used={k: round(v, 3) for k, v in eff.items()},
        consensus=consensus_pct,
        playbook_veto=playbook_veto,
    )
