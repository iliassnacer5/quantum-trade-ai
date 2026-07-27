"""ENTRAÎNEMENT QUOTIDIEN DES AGENTS SUR LA STRATÉGIE DU DESK.

Chaque nuit, la stratégie (`domain.playbook`) est **rejouée sur l'historique** de chaque symbole :
on se replace bougie 15 min par bougie 15 min, on reconstruit le setup avec les SEULES données
disponibles à cet instant (walk-forward, aucun regard vers le futur), et quand le déclencheur
15 min se serait formé, on rejoue le prix pour savoir si le TP a été touché avant le SL.

Ce que ça produit, et à quoi ça sert :

1. **La fiabilité MESURÉE de chaque combinaison** — par symbole, par type de déclencheur (repli /
   cassure / divergence) et par fenêtre de session. C'est ce qui classe les 5 trades du jour
   (`playbook_service._rank_key`) : on met devant ce qui a réellement fonctionné, pas ce qui a
   l'air joli.
2. **La compétence mesurée de chaque FACTEUR** (MA, RSI, MACD, VWAP, structure, volume,
   divergences) : combien de fois cet argument avait raison quand le trade s'est joué. Agrégée par
   agent, elle devient un multiplicateur de poids — c'est ainsi que les agents deviennent experts
   DE CETTE stratégie et pas d'une autre.
3. **Une fiche d'expertise rédigée par agent** : ses chiffres de la veille transformés en règles
   opératoires courtes, réinjectées dans ses prompts (cf. `agents.expertise`).

Tout est déterministe et mesurable ; le LLM n'intervient qu'à la toute fin, pour rédiger la fiche.
Il ne fabrique aucun chiffre.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.data import sessions as sessions_mod
from app.domain import playbook
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)

COLLECTION = "playbook_training"
LATEST = "latest"

# Combien de bougies 15 min on laisse au trade pour atteindre son objectif avant de le déclarer
# « expiré » (ni TP ni SL). 96 bougies = 24 heures : au-delà, ce n'est plus un trade 15 min.
_MAX_HOLD_BARS = 96

# Profondeur d'historique passée à chaque unité de temps (bornée : le coût du walk-forward est
# linéaire en nombre de bougies × nombre de points évalués).
_TAIL = {"monthly": 60, "daily": 200, "h4": 200, "h1": 200, "m15": 200}

# Quel agent porte quel facteur de la stratégie. C'est la table qui transforme « le RSI avait
# raison 58 % du temps » en « l'agent technique mérite un poids un peu plus élevé ».
FACTOR_OWNER = {
    "ma": "technical",
    "rsi": "technical",
    "macd": "technical",
    "vwap": "volume",
    "volume": "volume",
    "structure": "pattern",
    "divergence": "pattern",
    "fibonacci": "playbook",
}

# État en mémoire : dernier entraînement connu. Rempli au démarrage depuis le store puis à chaque
# passage nocturne. Lu à chaud par le classement des trades et par les agents.
_STATE: dict = {}


# =======================================================================================
# Lecture (chaud) — utilisée par le classement des trades et par les agents
# =======================================================================================
def snapshot() -> dict:
    """Dernier entraînement connu (vide tant qu'aucun passage n'a eu lieu)."""
    return _STATE


def is_trained() -> bool:
    return bool(_STATE.get("trades"))


def load_from_store(store) -> dict:  # noqa: ANN001
    """Recharge le dernier entraînement persisté (appelé au démarrage de l'API)."""
    global _STATE
    rec = store.records.get(COLLECTION, LATEST)
    if rec:
        _STATE = rec
    return _STATE


def _stat(bucket: str, key: str) -> dict | None:
    entry = (_STATE.get(bucket) or {}).get(key)
    if not entry:
        return None
    if entry.get("trades", 0) < get_settings().playbook_training_min_trades:
        return None
    return entry


def edge_for(symbol: str, trigger_type: str | None = None) -> dict | None:
    """Fiabilité MESURÉE de ce symbole (et de ce déclencheur) sur le dernier walk-forward.

    Retourne ``{"score", "win_rate", "trades", "expectancy_r", "status"}`` ou ``None`` si la
    stratégie n'a pas encore été entraînée. ``score`` est l'espérance en R (gain moyen par trade
    exprimé en multiples du risque) : positif = edge constaté, négatif = perte constatée.

    Un symbole non mesuré (pas assez de trades) reçoit un score de 0 : il n'est ni favorisé ni
    pénalisé — on ne prétend pas savoir ce qu'on n'a pas mesuré.
    """
    if not is_trained():
        return None
    by_symbol = _stat("by_symbol", symbol.upper())
    by_trigger = _stat("by_trigger", trigger_type) if trigger_type else None
    if by_symbol is None and by_trigger is None:
        # Repli sur le BACKTEST longue portée : il couvre ~2 ans là où le walk-forward court ne
        # couvre que quelques jours. Mieux vaut une mesure ancienne qu'aucune mesure.
        deep = backtest_edge(symbol)
        if deep:
            return {**deep, "trigger": trigger_type, "measured_on": "backtest longue portée",
                    "status": (f"mesuré sur {deep['trades']} trades de backtest — "
                               f"{deep['win_rate']}% de réussite, espérance {deep['score']:+.2f} R")}
        return {"score": 0.0, "trades": 0, "status": "non mesuré (pas assez d'historique)"}

    parts, weights, bases = [], [], []
    if by_symbol:
        parts.append(by_symbol["expectancy_r"])
        weights.append(0.6)
        bases.append(
            f"{symbol.upper()} : {by_symbol['trades']} trades, {by_symbol['win_rate']}% de "
            f"réussite, espérance {by_symbol['expectancy_r']:+.2f} R"
        )
    if by_trigger:
        parts.append(by_trigger["expectancy_r"])
        weights.append(0.4)
        bases.append(
            f"déclencheur « {trigger_type} » : {by_trigger['trades']} trades, "
            f"{by_trigger['win_rate']}% de réussite, espérance {by_trigger['expectancy_r']:+.2f} R"
        )
    score = sum(p * w for p, w in zip(parts, weights, strict=True)) / sum(weights)
    # On expose SÉPARÉMENT ce qui vient du symbole et ce qui vient du déclencheur : les confondre
    # laisserait croire qu'un symbole a été mesuré sur des trades qui ne sont pas les siens.
    ref = by_symbol or by_trigger
    return {
        "score": round(score, 3),
        "win_rate": ref["win_rate"],
        "trades": ref["trades"],
        "expectancy_r": ref["expectancy_r"],
        "trigger": trigger_type,
        "measured_on": "symbole + déclencheur" if (by_symbol and by_trigger)
        else "symbole" if by_symbol else "déclencheur seul",
        "status": "mesuré — " + " ; ".join(bases),
    }


def agent_multipliers() -> dict[str, float]:
    """Multiplicateurs de poids par agent, issus de la compétence MESURÉE de ses facteurs.

    1,0 = neutre (agent non mesuré). Au-dessus : ses arguments ont eu raison plus souvent que le
    hasard sur cette stratégie. En dessous : ils ont eu tort plus souvent.
    """
    return dict(_STATE.get("agent_multipliers") or {})


def expertise(agent: str) -> str:
    """Fiche d'expertise du jour pour cet agent (chaîne vide si l'entraînement n'a pas tourné)."""
    return (_STATE.get("expertise") or {}).get(agent, "")


# =======================================================================================
# Walk-forward
# =======================================================================================
class SyntheticDataError(RuntimeError):
    """Les données du symbole ne sont pas réelles : on refuse d'en tirer une statistique."""


# Durée d'une bougie, par unité de temps (secondes). Sert à reconstituer l'axe temporel.
_INTERVAL_SECONDS = {"15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400, "1M": 2_592_000}


async def _series(symbol: str, interval: str, limit: int) -> list[Candle]:
    """Bougies RÉELLES et horodatées, via le MÊME chargeur que la stratégie en production.

    Deux exigences, dans cet ordre :

    1. **Mêmes données qu'en production.** On passe par `markets.load_candles` — le chargeur que la
       stratégie utilise réellement (Binance / Alpaca / OANDA / Yahoo selon la classe d'actif).
       S'entraîner sur une autre source que celle qui décide en live n'aurait aucun sens.
    2. **Jamais de synthétique.** Si le connecteur retombe sur des bougies factices, on lève : une
       statistique tirée de données inventées est pire qu'une absence de statistique, parce qu'elle
       se donne l'apparence d'une mesure et finirait par classer de vrais trades.

    L'horodatage est reconstitué à partir de la cadence de l'unité de temps (les bougies sont
    contiguës et régulières, et toutes les séries se terminent au même instant de marché) : c'est
    suffisant pour aligner les unités de temps entre elles, ce qui est le seul usage qu'on en fait.
    """
    from app.data import markets

    # Les fournisseurs limitent le débit, et l'entraînement tourne pendant que la boucle de fond
    # interroge déjà les mêmes API. Un refus ponctuel n'est pas une absence de données : on réessaie
    # avec attente croissante avant de conclure que le symbole n'est pas entraînable.
    candles: list[Candle] = []
    for attempt, pause in enumerate((0.0, 2.0, 5.0)):
        if pause:
            await asyncio.sleep(pause)
        candles = await markets.load_candles(symbol, interval=interval, limit=limit)
        if markets.is_real(symbol):
            break
        logger.debug(
            "Entraînement %s %s : tentative %d retombée sur du synthétique",
            symbol, interval, attempt + 1,
        )
    else:
        raise SyntheticDataError(
            f"données non réelles pour {symbol} en {interval} après 3 tentatives (repli synthétique)"
        )
    step = _INTERVAL_SECONDS.get(interval, 3_600)
    end = datetime.now(UTC)
    last = len(candles) - 1
    return [
        replace(c, timestamp=end - timedelta(seconds=step * (last - i)))
        for i, c in enumerate(candles)
    ]


def _slice_until(candles: list[Candle], stamps: list[float], t: float, tail: int) -> list[Candle]:
    """Bougies disponibles À L'INSTANT `t` (walk-forward strict : aucun regard vers le futur)."""
    idx = bisect.bisect_right(stamps, t)
    return candles[max(0, idx - tail):idx]


def _replay(m15: list[Candle], start: int, direction: str, stop: float, target: float) -> tuple[str, float]:
    """Rejoue le prix APRÈS l'entrée : le TP ou le SL est-il touché en premier ?

    Le stop l'emporte quand les deux sont touchés dans la même bougie (hypothèse prudente : on ne
    sait pas dans quel ordre le prix a circulé à l'intérieur de la bougie). Retourne
    ``(outcome, r_multiple)`` avec outcome ∈ {won, lost, expired}.
    """
    buy = direction == "BUY"
    for candle in m15[start + 1: start + 1 + _MAX_HOLD_BARS]:
        hit_sl = candle.low <= stop if buy else candle.high >= stop
        hit_tp = candle.high >= target if buy else candle.low <= target
        if hit_sl:
            return "lost", -1.0
        if hit_tp:
            return "won", 1.0
    return "expired", 0.0


def _factor_votes(setup_layers: dict, direction: str) -> dict[str, int]:
    """Pour chaque facteur : a-t-il plaidé DANS le sens du trade (+1), contre (-1), ou pas (0) ?

    On agrège les couches journalier / 4 h / 15 min (celles qui portent la décision d'entrée) :
    c'est la compétence du facteur SUR CETTE STRATÉGIE que l'on mesure, pas dans l'absolu.
    """
    want = 1 if direction == "BUY" else -1
    votes: dict[str, int] = {}
    for layer_name in ("daily", "h4", "m15"):
        for f in (setup_layers.get(layer_name) or {}).get("factors", []):
            score = f.get("score", 0)
            if not score:
                continue
            votes[f["key"]] = votes.get(f["key"], 0) + (1 if (score > 0) == (want > 0) else -1)
    return votes


async def train_symbol(symbol: str, *, asset_class: str = "") -> dict:
    """Rejoue la stratégie sur l'historique d'UN symbole et retourne ses trades reconstitués."""
    s = get_settings()
    bars = s.playbook_training_bars
    # Chargement SÉQUENTIEL : `markets.is_real` mémorise la source du DERNIER chargement par
    # symbole. En parallèle, les cinq unités de temps s'écraseraient mutuellement et on ne saurait
    # plus laquelle est retombée sur du synthétique.
    try:
        m15 = await _series(symbol, "15m", bars)
        h1 = await _series(symbol, "1h", 600)
        h4 = await _series(symbol, "4h", 400)
        daily = await _series(symbol, "1d", 400)
        monthly = await _series(symbol, "1M", 80)
    except SyntheticDataError as exc:
        logger.info("Entraînement %s ignoré : %s", symbol, exc)
        return {"symbol": symbol, "trades": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — un symbole indisponible ne casse pas l'entraînement
        logger.warning("Entraînement %s : historique indisponible (%s)", symbol, exc)
        return {"symbol": symbol, "trades": [], "error": str(exc)}

    if len(m15) < 260 or len(daily) < 80 or len(monthly) < 14:
        return {"symbol": symbol, "trades": [], "error": "historique insuffisant"}

    def _stamps(candles: list[Candle]) -> list[float]:
        return [c.timestamp.timestamp() if c.timestamp else 0.0 for c in candles]

    st = {"h1": (h1, _stamps(h1)), "h4": (h4, _stamps(h4)),
          "daily": (daily, _stamps(daily)), "monthly": (monthly, _stamps(monthly))}

    trades: list[dict] = []
    step = max(1, s.playbook_training_step)
    # On s'arrête assez tôt pour laisser au dernier trade le temps de se dénouer.
    for i in range(240, len(m15) - _MAX_HOLD_BARS - 1, step):
        stamp = m15[i].timestamp
        if stamp is None:
            continue
        t = stamp.timestamp()
        sliced = {name: _slice_until(c, ts, t, _TAIL[name]) for name, (c, ts) in st.items()}
        if len(sliced["daily"]) < 60 or len(sliced["h4"]) < 60 or len(sliced["monthly"]) < 12:
            continue
        # Calcul PUR déporté dans un thread : ce walk-forward appelle `build` des centaines de fois
        # et bloquerait sinon la boucle d'événements — donc l'API — pendant tout le passage.
        setup = await asyncio.to_thread(
            playbook.build,
            symbol,
            sliced["monthly"], sliced["daily"], sliced["h4"], m15[max(0, i - _TAIL["m15"] + 1): i + 1],
            h1=sliced["h1"] or None,
            session=sessions_mod.session_context(stamp),
            min_rr=s.playbook_min_rr, max_rr=s.playbook_max_rr,
            min_target_pips=s.playbook_min_target_pips,
            min_target_atr15=s.playbook_min_target_atr15,
            max_stop_pips=s.playbook_max_stop_pips,
            max_stop_atr15=s.playbook_max_stop_atr15,
            target_level_buffer=s.playbook_target_level_buffer,
            max_atr_multiple=s.playbook_max_atr_multiple,
        )
        if not setup.ready or setup.entry is None:
            continue
        outcome, r_raw = _replay(m15, i, setup.direction, setup.stop_loss, setup.take_profit_1)
        if outcome == "expired":
            continue
        # Gain exprimé en R : un gagnant rapporte le R/R du trade, un perdant coûte exactement 1 R.
        r_multiple = setup.risk_reward if outcome == "won" else -1.0
        sess = setup.session or {}
        trades.append({
            "symbol": symbol,
            "asset_class": asset_class,
            "at": stamp.isoformat(),
            "direction": setup.direction,
            "trigger": (setup.trigger or "").split(" — ", 1)[0].strip() or "inconnu",
            "session": (sess.get("kill_zones") or ["hors_fenetre"])[0],
            "prime": bool(sess.get("prime")),
            "risk_reward": round(setup.risk_reward, 2),
            "outcome": outcome,
            "r": round(r_multiple, 3),
            "factor_votes": _factor_votes(setup.layers, setup.direction),
        })
    return {"symbol": symbol, "trades": trades}


# =======================================================================================
# Agrégation
# =======================================================================================
def _aggregate(trades: list[dict], key: str) -> dict[str, dict]:
    """Statistiques par valeur d'une clé (symbole, déclencheur, session…)."""
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        buckets.setdefault(str(t.get(key)), []).append(t)
    return {k: _metrics(v) for k, v in buckets.items()}


def _metrics(trades: list[dict]) -> dict:
    """Réussite, espérance en R et profit factor d'un lot de trades rejoués."""
    n = len(trades)
    wins = [t for t in trades if t["outcome"] == "won"]
    gross_win = sum(t["r"] for t in wins)
    gross_loss = -sum(t["r"] for t in trades if t["outcome"] == "lost")
    expectancy = sum(t["r"] for t in trades) / n if n else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "expectancy_r": round(expectancy, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_r": round(sum(t["r"] for t in trades), 2),
    }


def _factor_competence(trades: list[dict]) -> dict[str, dict]:
    """Pour chaque facteur : à quelle fréquence son argument était le bon quand le trade s'est joué."""
    tally: dict[str, dict[str, int]] = {}
    for t in trades:
        won = t["outcome"] == "won"
        for key, vote in (t.get("factor_votes") or {}).items():
            if vote == 0:
                continue
            row = tally.setdefault(key, {"aligned_win": 0, "aligned": 0, "against_win": 0, "against": 0})
            if vote > 0:                       # le facteur plaidait DANS le sens du trade
                row["aligned"] += 1
                row["aligned_win"] += int(won)
            else:                              # il plaidait contre : il avait raison si le trade a perdu
                row["against"] += 1
                row["against_win"] += int(not won)
    out: dict[str, dict] = {}
    for key, row in tally.items():
        total = row["aligned"] + row["against"]
        right = row["aligned_win"] + row["against_win"]
        out[key] = {
            "observations": total,
            "accuracy": round(right / total * 100, 1) if total else 0.0,
            "aligned": row["aligned"],
            "aligned_win_rate": round(row["aligned_win"] / row["aligned"] * 100, 1) if row["aligned"] else None,
        }
    return out


def _agent_multipliers(competence: dict[str, dict], min_obs: int) -> dict[str, float]:
    """Compétence des facteurs -> multiplicateur de poids par agent (borné à ±30 %).

    Un agent dont les arguments tombent juste 60 % du temps pèse plus qu'un agent à 45 %. Les
    facteurs trop peu observés sont ignorés : on ne pondère jamais sur du bruit.
    """
    per_agent: dict[str, list[float]] = {}
    for key, stat in competence.items():
        if stat["observations"] < min_obs:
            continue
        owner = FACTOR_OWNER.get(key)
        if owner:
            per_agent.setdefault(owner, []).append(stat["accuracy"] / 100.0)
    out: dict[str, float] = {}
    for agent, accuracies in per_agent.items():
        acc = sum(accuracies) / len(accuracies)
        out[agent] = round(max(0.7, min(1.3, 1.0 + (acc - 0.5) * 1.2)), 3)
    return out


# =======================================================================================
# Passage complet
# =======================================================================================
def training_universe(limit: int) -> list[dict]:
    """Symboles entraînés : ceux que la stratégie balaie réellement, dans l'ordre de priorité.

    Comme `daily_universe` est désormais filtré sur les marchés du desk (forex + métaux), les agents
    s'entraînent exactement sur ce qu'ils devront trader.
    """
    from app.services import playbook_service

    return playbook_service.daily_universe(limit=limit)


async def run_backtest_training(store) -> dict:  # noqa: ANN001
    """ENTRAÎNEMENT PAR LE BACKTEST — la version longue portée de l'entraînement quotidien.

    Le walk-forward court (`run_training`) mesure la stratégie sur quelques jours d'historique
    15 min ; ce passage-ci la mesure sur ~2 ans d'historique 1 h, sur toutes les paires du desk. Les
    deux se complètent : le premier réagit vite aux conditions du moment, le second dit ce qui
    fonctionne durablement — et c'est lui qui fournit le classement des paires.
    """
    from app.backtest import playbook_backtest as pbt

    payload = await pbt.run_backtest(store)
    scope = payload["scope"]
    # On réutilise le même vocabulaire que l'entraînement court : les consommateurs (classement des
    # trades, agents) lisent une seule et même structure.
    _STATE.setdefault("backtest", {})
    _STATE["backtest"] = {
        "date": payload["date"],
        "entry_timeframe": scope["entry_timeframe"],
        "years_covered": scope["years_covered"],
        "overall": scope["overall"],
        "ranking": scope["ranking"],
        "by_symbol": scope["by_symbol"],
        "by_trigger": scope["by_trigger"],
        "losers_profile": scope["losers_profile"],
        "conclusion": payload["conclusion"],
    }
    try:
        store.records.put(COLLECTION, LATEST, _STATE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Entraînement par backtest non persisté (%s)", exc)
    return payload


def backtest_edge(symbol: str) -> dict | None:
    """Fiabilité LONGUE PORTÉE d'une paire, mesurée par le backtest (None si non mesurée)."""
    bt = (_STATE.get("backtest") or {}).get("by_symbol") or {}
    row = bt.get(symbol.upper())
    if not row or row.get("trades", 0) < get_settings().playbook_training_min_trades:
        return None
    return {"score": row["expectancy_r"], "win_rate": row["win_rate"], "trades": row["trades"],
            "source": "backtest longue portée"}


async def run_training(store, *, symbols: list[dict] | None = None, write_expertise: bool = True) -> dict:  # noqa: ANN001
    """Passage complet : walk-forward de tous les symboles, agrégation, fiches d'expertise.

    Le résultat est persisté (clé du jour + clé `latest`) ET chargé en mémoire pour être lu à chaud
    par le classement des trades et par les agents.
    """
    global _STATE

    s = get_settings()
    started = datetime.now(UTC)
    universe = symbols or training_universe(s.playbook_training_symbols)
    sem = asyncio.Semaphore(max(1, s.playbook_max_parallel // 2))

    async def _one(item: dict) -> dict:
        async with sem:
            return await train_symbol(item["symbol"], asset_class=item.get("asset_class", ""))

    results = await asyncio.gather(*(_one(i) for i in universe), return_exceptions=True)
    trades: list[dict] = []
    failures: list[dict] = []
    for item, res in zip(universe, results, strict=True):
        if isinstance(res, Exception):
            failures.append({"symbol": item["symbol"], "error": str(res)})
            continue
        if res.get("error"):
            failures.append({"symbol": res["symbol"], "error": res["error"]})
        trades.extend(res.get("trades") or [])

    competence = _factor_competence(trades)
    min_obs = max(5, s.playbook_training_min_trades)
    payload = {
        "date": started.date().isoformat(),
        "generated_at": started.isoformat(),
        "duration_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "symbols_trained": len(universe) - len(failures),
        "failures": failures,
        "trades": len(trades),
        "overall": _metrics(trades),
        "by_symbol": _aggregate(trades, "symbol"),
        "by_trigger": _aggregate(trades, "trigger"),
        "by_session": _aggregate(trades, "session"),
        "factor_competence": competence,
        "agent_multipliers": _agent_multipliers(competence, min_obs),
        "min_trades": s.playbook_training_min_trades,
        "strategy": "Playbook MTF — stop 15 min, objectif borné 1 h, R/R 1:1,2–1:1,3",
    }
    if write_expertise:
        payload["expertise"] = await build_expertise(payload)

    _STATE = payload
    try:
        store.records.put(COLLECTION, payload["date"], payload)
        store.records.put(COLLECTION, LATEST, payload)
    except Exception as exc:  # noqa: BLE001 — la persistance ne doit pas perdre l'entraînement
        logger.warning("Entraînement non persisté (%s)", exc)
    logger.info(
        "Entraînement playbook : %d trades rejoués sur %d symboles — réussite %.1f%%, espérance %+.2f R",
        payload["trades"], payload["symbols_trained"],
        payload["overall"]["win_rate"], payload["overall"]["expectancy_r"],
    )
    return payload


# =======================================================================================
# Fiches d'expertise (LLM) — rédaction seulement, jamais de chiffre inventé
# =======================================================================================
_AGENT_BRIEF = {
    "playbook": "tu appliques la stratégie complète et tu décides s'il y a un trade",
    "technical": "tu lis les moyennes mobiles, le RSI et le MACD",
    "volume": "tu valides (ou non) les mouvements par le volume et le VWAP",
    "pattern": "tu lis la structure de marché et les divergences",
}


def _agent_facts(agent: str, payload: dict) -> str:
    """Les chiffres MESURÉS de cet agent, mis en forme pour le prompt (aucune interprétation)."""
    lines: list[str] = []
    overall = payload["overall"]
    lines.append(
        f"Stratégie globale : {overall['trades']} trades rejoués, {overall['win_rate']}% de "
        f"réussite, espérance {overall['expectancy_r']:+.2f} R."
    )
    owned = [k for k, v in FACTOR_OWNER.items() if v == agent]
    for key in owned:
        stat = (payload.get("factor_competence") or {}).get(key)
        if stat and stat["observations"] >= payload["min_trades"]:
            lines.append(
                f"Facteur {key} : {stat['accuracy']}% de justesse sur {stat['observations']} "
                f"observations."
            )
    best = sorted(
        ((k, v) for k, v in (payload.get("by_symbol") or {}).items() if v["trades"] >= payload["min_trades"]),
        key=lambda kv: kv[1]["expectancy_r"], reverse=True,
    )
    if best:
        top = ", ".join(f"{k} ({v['expectancy_r']:+.2f} R)" for k, v in best[:3])
        line = f"Meilleurs marchés : {top}."
        # « Moins bons » n'a de sens que s'il reste des marchés DIFFÉRENTS de ceux déjà cités —
        # sinon on répéterait le même symbole en le présentant comme son propre contraire.
        remaining = best[3:]
        if remaining:
            worst = ", ".join(f"{k} ({v['expectancy_r']:+.2f} R)" for k, v in remaining[-3:])
            line += f" Moins bons : {worst}."
        lines.append(line)
    triggers = [(k, v) for k, v in (payload.get("by_trigger") or {}).items()
                if v["trades"] >= payload["min_trades"]]
    if triggers:
        lines.append("Déclencheurs : " + ", ".join(
            f"{k} {v['win_rate']}% ({v['trades']} trades)" for k, v in triggers))
    sessions = [(k, v) for k, v in (payload.get("by_session") or {}).items()
                if v["trades"] >= payload["min_trades"]]
    if sessions:
        lines.append("Fenêtres : " + ", ".join(
            f"{k} {v['win_rate']}% ({v['trades']} trades)" for k, v in sessions))
    return "\n".join(lines)


def _fallback_memo(agent: str, payload: dict) -> str:
    """Fiche déterministe, utilisée quand aucun LLM n'est disponible (jamais de page vide)."""
    facts = _agent_facts(agent, payload)
    return (
        f"Fiche d'expertise du {payload['date']} — agent {agent}.\n{facts}\n"
        "Règle : privilégier les combinaisons dont l'espérance mesurée est positive, se taire sur "
        "celles qui n'ont pas assez d'historique."
    )


async def build_expertise(payload: dict) -> dict[str, str]:
    """Rédige la fiche d'expertise du jour pour chaque agent, à partir de ses chiffres MESURÉS.

    Le LLM ne reçoit QUE des statistiques déjà calculées et n'a pas le droit d'en inventer : il
    transforme des mesures en règles opératoires courtes. Sans LLM disponible, on retombe sur une
    fiche déterministe — l'entraînement reste utile.
    """
    from app.agents import llm

    s = get_settings()
    out: dict[str, str] = {}
    use_llm = s.playbook_expertise_llm and llm.available() and payload["trades"] > 0
    for agent, role in _AGENT_BRIEF.items():
        facts = _agent_facts(agent, payload)
        if not use_llm:
            out[agent] = _fallback_memo(agent, payload)
            continue
        prompt = (
            "Tu es le responsable de la formation d'un desk de trading. Voici les résultats MESURÉS "
            f"de la nuit pour l'agent « {agent} » ({role}), obtenus en rejouant notre stratégie "
            "(mensuel/journalier → 4 h → entrée 15 min, stop sur la structure 15 min, objectif "
            "borné par le prochain niveau 1 h, R/R 1:1,2 à 1:1,3) sur l'historique réel :\n\n"
            f"{facts}\n\n"
            "Rédige sa fiche d'expertise du jour en 3 puces MAXIMUM, en français, à l'impératif. "
            "Chaque puce doit être une règle opératoire directement applicable demain sur CETTE "
            "stratégie. N'invente AUCUN chiffre : n'utilise que ceux ci-dessus. Pas de conseil en "
            "investissement, pas de promesse de gain."
        )
        try:
            text = await llm.complete(prompt, role="reasoning", max_tokens=320)
            out[agent] = f"Fiche du {payload['date']} — {text.strip()}"
        except Exception as exc:  # noqa: BLE001 — une fiche ratée ne casse pas l'entraînement
            logger.warning("Fiche d'expertise %s indisponible (%s)", agent, exc)
            out[agent] = _fallback_memo(agent, payload)
    return out
