"""ANALYSE QUOTIDIENNE — l'avis du modèle sur le forex et l'or, EN DEHORS de la stratégie du desk.

Pourquoi une analyse séparée alors que la stratégie tourne déjà en continu ? Parce que la stratégie
(`domain.playbook`) répond à une seule question : « y a-t-il un trade conforme à la méthode, MAINTENANT ? »
Elle refuse presque tout, et c'est son rôle. Elle ne dit jamais « voilà ce que je pense de l'euro
aujourd'hui ».

Cette analyse-ci répond à l'autre question : **quelle lecture le modèle fait-il de chaque marché ?**
Elle est produite par les MÊMES agents (technique, volume, sentiment, figures, macro) arbitrés par
le Master — mais **sans le playbook** :

- l'agent playbook n'est pas exécuté et ne pèse pas dans le vote ;
- son droit de VETO ne s'applique pas.

C'est délibéré et c'est tout l'intérêt : un second regard qui aurait le veto de la stratégie
finirait par répéter la stratégie. Ici le modèle a le droit de dire « je suis haussier sur l'or »
un jour où la méthode refuse d'entrer — les deux réponses sont vraies, elles ne répondent pas à la
même question.

Ce que l'analyse conserve, pour chaque symbole : la direction, la confiance, le score combiné du
Master, le détail de CHAQUE agent (score, confiance, justification), les indicateurs mesurés, le
consensus et les conflits. Autrement dit tout ce qui a produit l'avis — un avis sans son
raisonnement n'est pas vérifiable.

Elle est PERSISTÉE une fois par jour (clé du jour + `latest`) et rejouée automatiquement au
démarrage si celle du jour manque : l'utilisateur qui lance le projet le matin la trouve prête.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.core.config import get_settings
from app.models.signal import Timeframe

logger = logging.getLogger(__name__)

COLLECTION = "market_opinion"
LATEST = "latest"

#: Univers par défaut : les paires de devises majeures + l'or (et l'argent, qui se lit avec l'or).
#: Volontairement court — cette analyse doit être RÉDIGÉE et lue, pas parcourue en diagonale.
DEFAULT_SYMBOLS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/JPY", "GBP/JPY", "EUR/GBP",
    "XAU/USD", "XAG/USD",
]


def symbols() -> list[str]:
    """Univers analysé, depuis la configuration (repli sur `DEFAULT_SYMBOLS`)."""
    raw = get_settings().market_opinion_symbols or ""
    parsed = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return parsed or DEFAULT_SYMBOLS


# --- Lecture d'un avis, en français ----------------------------------------------------------
_STANCE = {
    "BUY": "haussier",
    "SELL": "baissier",
    "HOLD": "neutre",
}


def _conviction(confidence: int) -> str:
    """Traduit une confiance 0-100 en force d'opinion — jamais un chiffre brut sans lecture."""
    if confidence >= 70:
        return "conviction forte"
    if confidence >= 50:
        return "conviction modérée"
    if confidence >= 30:
        return "conviction faible"
    return "aucune conviction"


def _headline(symbol: str, direction: str, confidence: int, consensus: int, conflict: bool) -> str:
    stance = _STANCE.get(direction, "neutre")
    txt = (f"{symbol} — le modèle est {stance} ({_conviction(confidence)}, "
           f"confiance {confidence}/100, consensus {consensus} %)")
    if conflict:
        txt += ", avec des agents en désaccord"
    return txt + "."


async def analyse_symbol(symbol: str, *, timeframe: Timeframe = Timeframe.H1) -> dict:
    """Avis complet du modèle sur UN symbole, hors stratégie.

    Retourne l'avis ET tout ce qui l'a produit : score de chaque agent, sa confiance, sa
    justification, les indicateurs mesurés, la pesée du Master. En cas d'échec (données
    indisponibles), retourne une ligne `error` plutôt que d'inventer une opinion.
    """
    from app.data import macro as macro_data_mod
    from app.data import markets, news as news_mod
    from app.domain.risk import RiskParams
    from app.services.signal_service import _load_candles
    from app.signal_engine.engine import generate_signal

    try:
        candles = await _load_candles(symbol, timeframe)
        if len(candles) < 60:
            return {"symbol": symbol, "error": f"historique insuffisant ({len(candles)} bougies)"}
        # Aucune opinion sur des bougies inventées : mieux vaut une ligne vide qu'un avis fabriqué.
        if not markets.is_real(symbol):
            return {"symbol": symbol, "error": "données non réelles (repli synthétique)"}

        news = await news_mod.fetch_news(symbol)
        macro_ctx = await macro_data_mod.fetch_macro_data()
        card = await generate_signal(
            asset=symbol,
            candles=candles,
            news=news,
            risk=RiskParams(capital=10_000.0, risk_per_trade_pct=1.0),
            timeframe=timeframe,
            macro_data=macro_ctx,
            # LE POINT CENTRAL : la stratégie du desk est écartée de cette analyse.
            include_playbook=False,
        )
    except Exception as exc:  # noqa: BLE001 — un symbole KO n'arrête pas l'analyse des autres
        logger.warning("Analyse quotidienne %s échouée (%s)", symbol, exc)
        return {"symbol": symbol, "error": str(exc)}

    metrics = dict(card.metrics or {})
    master = metrics.pop("master_decision", {}) or {}
    # `playbook` / `session` ne peuvent pas être là (include_playbook=False) mais on s'en assure :
    # laisser filtrer la stratégie ici rendrait l'analyse trompeuse sur sa propre indépendance.
    metrics.pop("playbook", None)

    direction = card.direction.value
    return {
        "symbol": symbol,
        "asset_class": markets.asset_class(symbol),
        "direction": direction,
        "stance": _STANCE.get(direction, "neutre"),
        "confidence": card.confidence,
        "conviction": _conviction(card.confidence),
        "consensus_pct": card.consensus_pct,
        "headline": _headline(symbol, direction, card.confidence, card.consensus_pct,
                              bool(master.get("conflict"))),
        # LE RAISONNEMENT COMPLET — c'est ce qui rend l'avis vérifiable plutôt que déclaratif.
        "rationale": card.rationale,
        "agents": card.agents,                     # score, confiance et justification de chacun
        "master": {
            "score": master.get("score"),
            "threshold": master.get("threshold"),
            "consensus": master.get("consensus"),
            "conflict": master.get("conflict"),
            "weights_used": master.get("weights_used"),
        },
        "metrics": metrics,                        # RSI, MACD, ATR, moyennes… tels que mesurés
        "price": round(candles[-1].close, 8),
        "timeframe": timeframe.value,
        "levels": {
            # Niveaux INDICATIFS, calculés sur l'ATR (la stratégie n'ayant pas posé les siens).
            # Nommés comme tels : ce ne sont pas des ordres à passer, c'est l'échelle du mouvement
            # envisagé par l'avis.
            "entry": card.entry,
            "stop_loss": card.stop_loss,
            "take_profit_1": card.take_profit_1,
            "risk_reward": card.risk_reward,
            "source": metrics.get("levels_source", "atr"),
        },
    }


def _summarise(rows: list[dict]) -> dict:
    """Vue d'ensemble : combien de haussiers, de baissiers, et sur quoi le modèle est le plus net."""
    rated = [r for r in rows if not r.get("error")]
    if not rated:
        return {"analysed": 0, "note": "aucun symbole analysable — données indisponibles"}
    buys = [r for r in rated if r["direction"] == "BUY"]
    sells = [r for r in rated if r["direction"] == "SELL"]
    holds = [r for r in rated if r["direction"] == "HOLD"]
    strongest = max(rated, key=lambda r: r["confidence"])
    return {
        "analysed": len(rated),
        "failed": len(rows) - len(rated),
        "bullish": len(buys),
        "bearish": len(sells),
        "neutral": len(holds),
        "strongest": {
            "symbol": strongest["symbol"], "direction": strongest["direction"],
            "confidence": strongest["confidence"], "headline": strongest["headline"],
        },
        "note": (
            f"{len(rated)} instrument(s) analysé(s) : {len(buys)} haussier(s), {len(sells)} "
            f"baissier(s), {len(holds)} sans direction tranchée. Avis le plus net : "
            f"{strongest['symbol']} ({_STANCE.get(strongest['direction'], 'neutre')}, "
            f"confiance {strongest['confidence']}/100)."
            + (f" {len(rows) - len(rated)} instrument(s) sans données exploitables — ils sont "
               "listés sans avis plutôt que devinés."
               if len(rows) - len(rated) else "")
        ),
    }


async def run_daily_opinion(store=None, *, universe: list[str] | None = None) -> dict:  # noqa: ANN001
    """Produit l'analyse du jour sur tout l'univers et la persiste.

    Les symboles sont traités avec une concurrence bornée : chacun déclenche plusieurs appels
    réseau (bougies, actualités, macro) et les fournisseurs gratuits limitent le débit — les lancer
    tous d'un coup rallonge le passage au lieu de l'accélérer.
    """
    started = datetime.now(UTC)
    universe = universe or symbols()
    sem = asyncio.Semaphore(max(1, get_settings().playbook_max_parallel // 2))

    async def _one(sym: str) -> dict:
        async with sem:
            return await analyse_symbol(sym)

    rows = list(await asyncio.gather(*(_one(s) for s in universe)))
    # Les avis les plus tranchés d'abord — c'est l'ordre dans lequel on veut les lire.
    rows.sort(key=lambda r: (-1 if r.get("error") else r.get("confidence", 0)), reverse=True)

    payload = {
        "date": started.date().isoformat(),
        "generated_at": started.isoformat(),
        "duration_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "universe": universe,
        "opinions": rows,
        "summary": _summarise(rows),
        "method": (
            "Analyse produite par les agents du modèle (technique, volume, sentiment, figures, "
            "macro) arbitrés par le Master — SANS la stratégie du desk : l'agent playbook n'est ni "
            "exécuté ni compté dans le vote, et son droit de veto ne s'applique pas. C'est un "
            "SECOND REGARD, pas un signal d'entrée : la stratégie garde seule la décision de "
            "trader. Les deux peuvent diverger, et c'est précisément l'intérêt."
        ),
        "disclaimer": (
            "Aide à la décision, pas un conseil en investissement. Une opinion de modèle n'est pas "
            "une prévision : elle décrit ce que les indicateurs disent aujourd'hui."
        ),
    }
    if store is not None:
        try:
            store.records.put(COLLECTION, payload["date"], payload)
            store.records.put(COLLECTION, LATEST, payload)
        except Exception as exc:  # noqa: BLE001 — la persistance ne doit pas perdre l'analyse
            logger.warning("Analyse quotidienne non persistée (%s)", exc)
    logger.info("Analyse quotidienne : %s", payload["summary"].get("note"))
    return payload


def latest(store) -> dict | None:  # noqa: ANN001
    """Dernière analyse persistée (None si aucune n'a encore été produite)."""
    return store.records.get(COLLECTION, LATEST)


def is_fresh(payload: dict | None) -> bool:
    """Vrai si l'analyse porte la date du jour — c'est ce qui déclenche (ou non) un rattrapage."""
    if not payload:
        return False
    return payload.get("date") == datetime.now(UTC).date().isoformat()
