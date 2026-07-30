"""Agent Fondamental (M2) — santé de l'actif (actions).

Score déterministe à partir de ratios financiers (PER, croissance, dette, marge). Pour la crypto
(pas de fondamentaux classiques), renvoie un avis neutre. Enrichissement LLM (Claude) optionnel.
"""

from __future__ import annotations

from app.agents.base import AgentOutput, apply_playbook


def score_ratios(ratios: dict) -> tuple[float, list[str]]:
    """Score [-1..1] à partir de ratios. Heuristiques simples et explicables."""
    notes: list[str] = []
    contribs: list[float] = []

    pe = ratios.get("pe")
    if pe is not None:
        if pe < 15:
            contribs.append(0.4); notes.append(f"PER {pe:.0f} attractif")
        elif pe > 40:
            contribs.append(-0.4); notes.append(f"PER {pe:.0f} élevé")
        else:
            contribs.append(0.0); notes.append(f"PER {pe:.0f} correct")

    growth = ratios.get("revenue_growth")
    if growth is not None:
        contribs.append(max(-0.5, min(0.5, growth / 0.2 * 0.5)))
        notes.append(f"croissance CA {growth*100:.0f}%")

    debt = ratios.get("debt_to_equity")
    if debt is not None:
        contribs.append(-0.3 if debt > 2 else 0.2)
        notes.append(f"dette/capitaux {debt:.1f}")

    margin = ratios.get("net_margin")
    if margin is not None:
        contribs.append(max(-0.3, min(0.3, margin / 0.2 * 0.3)))
        notes.append(f"marge nette {margin*100:.0f}%")

    score = max(-1.0, min(1.0, sum(contribs) / len(contribs))) if contribs else 0.0
    return score, notes


async def run(symbol: str, ratios: dict | None = None,
              context: dict | None = None) -> AgentOutput:
    name = "fundamental"
    base = symbol.split("/")[0]
    is_crypto = "/" in symbol and symbol.split("/")[1] in {"USDT", "USD", "USDC", "BTC", "ETH"}

    if is_crypto or not ratios:
        return AgentOutput(
            name, 0.0, 0.2,
            f"Pas de fondamentaux classiques pour {base} (actif crypto ou données indisponibles).",
        )
    score, notes = score_ratios(ratios)
    confidence = min(1.0, 0.4 + 0.1 * len(notes))
    # Cadre de la stratégie, atténuation douce : la valorisation d'une entreprise et la tendance
    # d'un graphique ne se contredisent pas vraiment — elles répondent à des horizons différents.
    metrics: dict = {"ratios": ratios}
    pb_notes: list[str] = []
    score, confidence = apply_playbook(score, confidence, pb_notes, metrics, context, soften=0.7)
    return AgentOutput(
        name=name,
        score=round(score, 3),
        confidence=round(confidence, 3),
        rationale=f"Analyse fondamentale {base} : " + " ; ".join(notes + pb_notes) + ".",
        details=metrics,
    )
