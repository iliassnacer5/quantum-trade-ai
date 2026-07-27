"""Filtre de qualité d'entrée — règles PRINCIPIELLES (pas du curve-fitting).

Un edge robuste vient de la SÉLECTIVITÉ : ne trader que les setups où la probabilité penche
réellement en notre faveur. Trois critères, fondés sur la théorie du trading (pas optimisés sur les
données historiques, ce que le walk-forward sanctionnerait) :

1. Régime de TENDANCE (ADX) : en range (ADX faible), les signaux directionnels font du whipsaw.
2. CONFIANCE minimale : le consensus pondéré des agents doit être suffisant.
3. R/R minimal : couper vite les pertes, laisser courir les gains.

Ce filtre est appliqué à la fois au backtest (ce que le walk-forward mesure) et au live (signaux
réellement émis), pour que la validation reflète la stratégie réellement tradée.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.models.signal import Direction


# Modes de sévérité : le trader choisit le curseur fiabilité <-> quantité de signaux.
# strict = défaut mesuré ; équilibré = plus de signaux, filtres raisonnables ; agressif = biais
# directionnel dès qu'il existe (à réserver au paper / aux traders expérimentés).
# NB : `min_rr` reste à 2.0 dans TOUS les modes — c'est le plancher de la bande imposée par la
# stratégie (1,2 à 1,3). Le curseur fiabilité/quantité joue sur la confiance, l'ADX et le
# multi-timeframe, jamais sur le R/R.
MODES: dict[str, dict] = {
    "strict":     {"min_conf": 62, "min_adx": 22, "min_rr": 2.0, "mtf_min": 2},
    "balanced":   {"min_conf": 52, "min_adx": 18, "min_rr": 2.0, "mtf_min": 2},
    "aggressive": {"min_conf": 42, "min_adx": 12, "min_rr": 2.0, "mtf_min": 1},
}


def thresholds(mode: str | None = None) -> dict:
    s = get_settings()
    base = MODES.get(mode or "strict", MODES["strict"])
    if mode in (None, "strict"):
        # Le mode strict reste piloté par la config globale (rétrocompatible).
        return {"min_conf": s.entry_min_confidence, "min_adx": s.entry_min_adx,
                "min_rr": s.entry_min_rr, "mtf_min": 2}
    return base


def is_tradeable(card, settings=None, mode: str | None = None) -> bool:  # noqa: ANN001
    """Vrai si le signal passe le filtre de qualité (tendance + confiance + R/R) du mode choisi."""
    s = settings or get_settings()
    th = thresholds(mode)
    direction = card.direction
    dir_val = direction.value if hasattr(direction, "value") else direction
    if dir_val == Direction.HOLD.value:
        return False
    m = getattr(card, "metrics", None) or {}
    adx = m.get("adx") or 0.0

    # Anti-« couteau qui tombe » : ne pas trader CONTRE la tendance de fond (EMA longue).
    # Gardé dans TOUS les modes : mesuré comme le meilleur filtre (win rate +13 pts).
    if s.entry_trend_filter:
        ema_long = m.get("ema200") or m.get("ema50")
        price = m.get("price") or card.entry
        if ema_long:
            if dir_val == "BUY" and price < ema_long * 0.999:
                return False
            if dir_val == "SELL" and price > ema_long * 1.001:
                return False

    return (
        card.confidence >= th["min_conf"]
        and adx >= th["min_adx"]
        and (card.risk_reward or 0.0) >= th["min_rr"]
    )


def context_score(card) -> int:  # noqa: ANN001
    """Score de contexte marché 0-100 (régime, consensus, alignement, confiance). Explicabilité."""
    m = getattr(card, "metrics", None) or {}
    s = min(30, int(m.get("adx") or 0))                              # 0-30 régime de tendance
    s += min(25, int((card.consensus_pct or 0) * 25 / 100))         # 0-25 consensus agents
    s += 20 if (getattr(card, "mtf", None) or {}).get("aligned", 0) >= 2 else 0  # 0-20 multi-TF
    s += 25 if card.confidence >= 62 else int(card.confidence * 25 / 62)         # 0-25 confiance
    return min(100, s)


def timing_score(card) -> int:  # noqa: ANN001
    """Score de timing 0-100 (volatilité exploitable, force de tendance, pas de gap). Explicabilité."""
    m = getattr(card, "metrics", None) or {}
    s = 25 if (m.get("atr_pct") or 0) > 0.3 else 12                  # 0-25 volatilité (pas de range mort)
    s += 25 if m.get("adx_state") == "tendance forte" else 10       # 0-25 tendance
    s += 30                                                          # 0-30 hors blackout (déjà gaté en amont)
    s += 20 if not m.get("gap_pct") else 8                          # 0-20 pas de gap non comblé
    return min(100, s)


def rejection_reason(card, settings=None, mode: str | None = None) -> str:  # noqa: ANN001
    """Explique pourquoi un signal est filtré (pour la transparence/rationale)."""
    th = thresholds(mode)
    m = getattr(card, "metrics", None) or {}
    adx = m.get("adx") or 0.0
    reasons = []
    if card.confidence < th["min_conf"]:
        reasons.append(f"confiance {card.confidence}% < {th['min_conf']}%")
    if adx < th["min_adx"]:
        reasons.append(f"ADX {adx:.0f} < {th['min_adx']:.0f} (pas de tendance)")
    if (card.risk_reward or 0.0) < th["min_rr"]:
        reasons.append(f"R/R {card.risk_reward} < {th['min_rr']}")
    s = settings or get_settings()
    if s.entry_trend_filter:
        ema_long = m.get("ema200") or m.get("ema50")
        price = m.get("price") or card.entry
        dir_val = card.direction.value if hasattr(card.direction, "value") else card.direction
        if ema_long and dir_val == "BUY" and price < ema_long * 0.999:
            reasons.append("prix sous la tendance de fond (pas d'achat contre-tendance)")
        if ema_long and dir_val == "SELL" and price > ema_long * 1.001:
            reasons.append("prix au-dessus de la tendance de fond (pas de vente contre-tendance)")
    return ", ".join(reasons) or "OK"
