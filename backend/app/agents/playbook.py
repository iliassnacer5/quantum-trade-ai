"""Agent PLAYBOOK — l'agent chef de file : il applique LA stratégie du desk, étape par étape.

C'est lui qui décide s'il y a un trade ou non. Les autres agents (technique, volume, sentiment,
pattern, fondamental, macro) restent des contributeurs : ils nuancent, ils ne peuvent pas
outrepasser le playbook.

Méthode appliquée (cf. `domain.playbook`) :
  1. Mensuel + Journalier -> tendance de fond + supports/résistances MAJEURS
  2. Journalier -> RSI14, MA20, MA50, volume, tendance VWAP, divergences RSI/MACD, Fibonacci si correction
  3. 4 h -> mêmes facteurs, confirmation
  4. 15 min -> déclencheur d'entrée (UNIQUE unité de temps d'entrée)
  + R/R ≥ 1:2, objectif ≥ 100 pips, faisabilité vs niveau majeur, fenêtre de session.

Sortie : AgentOutput(name="playbook", details={"veto": bool, ...}) — un `veto` à True force le
Master à rester en HOLD (aucun autre agent ne peut le contourner).
"""

from __future__ import annotations

import logging

from app.agents.base import AgentOutput, enrich
from app.domain.playbook import PlaybookSetup

logger = logging.getLogger(__name__)

NAME = "playbook"

_STEP_TITLES = {
    1: "Étape 1 — Mensuel + Journalier",
    2: "Étape 2 — Analyse journalière",
    3: "Étape 3 — Confirmation 4 h",
    4: "Étape 4 — Entrée 15 min",
    5: "Risque",
    6: "Risque/rendement",
    7: "Objectif",
    8: "Timing de session",
}


def _rationale(setup: PlaybookSetup) -> str:
    """Note de desk : verdict, puis la checklist complète étape par étape."""
    from app.agents import expertise

    head = setup.summary()
    lines = [head]
    # Ce que l'entraînement de la nuit a mesuré SUR CE SYMBOLE : on le dit avant tout le reste,
    # parce que c'est de l'historique et pas une opinion.
    edge = _measured_edge(setup)
    if edge:
        lines.append(edge)
    exp_note = expertise.note(NAME)
    if exp_note:
        lines.append("🎓 " + exp_note)
    for c in setup.checklist:
        mark = "✅" if c["pass"] else "❌"
        lines.append(f"{mark} {_STEP_TITLES.get(c['step'], '')} · {c['label']} → {c['value']}")

    sess = setup.session or {}
    if sess:
        flag = "🎯" if sess.get("prime") else "🕒"
        lines.append(f"{flag} Session : {sess.get('label')} ({sess.get('utc_time')})")
        nxt = sess.get("next_window")
        if nxt and not sess.get("prime"):
            lines.append(
                f"⏭️ Prochaine fenêtre à forte valeur : {nxt['label']} — dans "
                f"{nxt['starts_in_minutes']} min ({nxt['window_utc']})"
            )

    lv = setup.levels or {}
    if lv.get("major_support") or lv.get("major_resistance"):
        lines.append(
            f"📐 Niveaux majeurs — support {lv.get('major_support')} ({lv.get('support_source')}) · "
            f"résistance {lv.get('major_resistance')} ({lv.get('resistance_source')})"
        )
    for key, label in (("daily", "Journalier"), ("h4", "4 h"), ("m15", "15 min")):
        layer = (setup.layers or {}).get(key)
        if layer and layer.get("notes"):
            lines.append(f"• {label} : " + " ; ".join(layer["notes"]))
    return "\n".join(lines)


def _measured_edge(setup: PlaybookSetup) -> str:
    """Ce que le walk-forward nocturne a constaté pour ce symbole et ce type de déclencheur."""
    from app.services import training_service

    trigger = (setup.trigger or "").split(" — ", 1)[0].strip() or None
    edge = training_service.edge_for(setup.symbol, trigger_type=trigger)
    if not edge or not edge.get("trades"):
        return ""
    verdict = "favorable" if edge["score"] > 0 else "défavorable" if edge["score"] < 0 else "neutre"
    return f"📊 Historique rejoué ({verdict}) : {edge['status']}."


def _confidence(setup: PlaybookSetup) -> float:
    if setup.insufficient:
        return 0.1
    return max(0.15, min(1.0, setup.confidence))


async def run(setup: PlaybookSetup, *, use_llm: bool = True) -> AgentOutput:
    """Transforme un setup de playbook en sortie d'agent (score + veto + justification)."""
    rationale = _rationale(setup)

    if use_llm and setup.direction != "NO_TRADE":
        from app.agents import expertise, llm

        if llm.available():
            try:
                prompt = (
                    expertise.prompt_prefix(NAME)
                    + "Tu es trader professionnel sur un desk. En UNE phrase complète en français, "
                    "sans conseil en investissement, commente ce setup issu d'une stratégie "
                    "multi-unités de temps (mensuel/journalier → 4 h → entrée 15 min, stop sur la "
                    "structure 15 min, objectif borné par le prochain niveau 1 h) :\n"
                    f"{setup.summary()}"
                )
                rationale = enrich(rationale, await llm.complete(prompt, role="reasoning", max_tokens=200))
            except Exception as exc:  # noqa: BLE001 — le LLM n'est qu'un commentaire
                logger.warning("Erreur LLM playbook : %s", exc)

    details = setup.as_dict()
    details["expert"] = True  # agent expert -> pèse davantage dans l'arbitrage du Master
    return AgentOutput(
        name=NAME,
        score=round(setup.score, 3),
        confidence=round(_confidence(setup), 3),
        rationale=rationale,
        details=details,
    )


async def run_for_symbol(symbol: str) -> AgentOutput:
    """Raccourci : construit le setup depuis les données de marché puis produit la sortie d'agent."""
    from app.services import playbook_service

    return await run(await playbook_service.build_setup(symbol))
