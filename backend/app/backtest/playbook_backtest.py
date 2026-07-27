"""BACKTEST DE LA STRATÉGIE DU DESK — forex et or, plusieurs paires, plusieurs années.

Ce module rejoue la stratégie complète (`domain.playbook`) sur l'historique réel, en walk-forward
strict : à chaque bougie évaluée, on ne reconstruit le setup qu'avec les données **déjà
disponibles** à cet instant. Aucun regard vers le futur, ni pour le contexte, ni pour l'entrée.

DEUX PASSES COMPLÉMENTAIRES, et c'est volontaire — aucune des deux ne suffit seule :

- **Passe PORTÉE (`1h`)** — le déclencheur d'entrée est évalué sur les bougies 1 h, sur toute la
  profondeur disponible (~2,8 ans). Elle donne le recul statistique : combien de trades, sur quelles
  paires, dans quelles conditions de marché.
- **Passe FIDÉLITÉ (`15m`)** — le vrai déclencheur de la stratégie, mais seulement sur les ~80 jours
  d'historique 15 min réellement disponibles chez les fournisseurs gratuits. Elle sert de contrôle :
  le passage au 15 min améliore-t-il ou dégrade-t-il ce que la passe portée a mesuré ?

**Pourquoi pas 5 ans en 15 min ?** Parce que la donnée n'existe pas gratuitement : Yahoo refuse
explicitement le 15 min au-delà de ~60 jours, et plafonne le 1 h à ~2,8 ans. Le prétendre serait
mentir sur la mesure. La couverture réelle de chaque passe est reportée dans le résultat.

Le classement des paires se lit sur l'espérance en R (gain moyen par trade, exprimé en multiples du
risque) : c'est la seule mesure qui combine taux de réussite ET rapport gain/perte.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.data import sessions as sessions_mod
from app.domain import pips as pips_mod
from app.domain import playbook
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)

COLLECTION = "playbook_backtest"
LATEST = "latest"

# Univers du desk : majeures + croisées liquides + métaux précieux.
FOREX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "GBP/JPY", "EUR/CHF", "AUD/JPY",
]
METALS = ["XAU/USD", "XAG/USD"]
DEFAULT_UNIVERSE = METALS + FOREX_PAIRS

# Profondeur de contexte passée à chaque unité de temps (bornée : le coût est linéaire).
_TAIL = {"monthly": 60, "daily": 200, "h4": 200, "h1": 200, "m15": 200}
_INTERVAL_SECONDS = {"15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400, "1M": 2_592_000}

# Durée maximale de détention, par unité de temps d'entrée. La stratégie vise 200 pips (~2-3 ATR
# journaliers) : au-delà de ~15 séances, le scénario qui a motivé l'entrée n'est plus d'actualité.
_MAX_HOLD = {"1h": 24 * 15, "15m": 4 * 24 * 15}


class NoRealDataError(RuntimeError):
    """Historique indisponible ou synthétique : on ne backteste pas sur des bougies inventées."""


# --- État d'exécution ------------------------------------------------------------------------
# Un backtest complet dure une dizaine de minutes et consomme un cœur entier. Il ne doit donc
# JAMAIS être exécuté dans le fil d'une requête HTTP (le navigateur attendrait indéfiniment), ni
# être lancé deux fois en parallèle (deux passages se disputeraient le CPU et les quotas API).
_state: dict = {"running": False, "started_at": None, "phase": None, "done": 0, "total": 0}


def run_state() -> dict:
    """État courant du backtest — permet à l'interface d'afficher une progression honnête."""
    out = dict(_state)
    if out["running"] and out["started_at"]:
        out["elapsed_s"] = round(
            (datetime.now(UTC) - datetime.fromisoformat(out["started_at"])).total_seconds(), 1)
    return out


def _set_phase(phase: str, done: int = 0, total: int = 0) -> None:
    _state.update(phase=phase, done=done, total=total)


# =======================================================================================
# Chargement
# =======================================================================================
async def _series(symbol: str, interval: str, limit: int) -> list[Candle]:
    """Bougies RÉELLES horodatées, via le chargeur utilisé en production.

    On refuse le repli synthétique : un backtest sur des données inventées produirait un classement
    de paires entièrement fictif, avec l'apparence du sérieux.
    """
    from app.data import markets

    candles: list[Candle] = []
    for attempt, pause in enumerate((0.0, 2.0, 5.0)):
        if pause:
            await asyncio.sleep(pause)
        candles = await markets.load_candles(symbol, interval=interval, limit=limit)
        if markets.is_real(symbol):
            break
        logger.debug("Backtest %s %s : tentative %d synthétique", symbol, interval, attempt + 1)
    else:
        raise NoRealDataError(f"{symbol} en {interval} : données non réelles après 3 tentatives")

    step = _INTERVAL_SECONDS.get(interval, 3_600)
    end = datetime.now(UTC)
    last = len(candles) - 1
    return [replace(c, timestamp=end - timedelta(seconds=step * (last - i)))
            for i, c in enumerate(candles)]


def _slice_until(candles: list[Candle], stamps: list[float], t: float, tail: int) -> list[Candle]:
    """Bougies disponibles À L'INSTANT `t` — walk-forward strict, aucun regard vers le futur."""
    idx = bisect.bisect_right(stamps, t)
    return candles[max(0, idx - tail):idx]


# =======================================================================================
# Rejeu d'un trade
# =======================================================================================
def replay_trade(
    bars: list[Candle], start: int, direction: str, entry: float,
    stop: float, target: float, *, max_hold: int, secure_at_r: float,
) -> dict:
    """Rejoue un trade bougie par bougie, AVEC la règle de sécurisation du profit.

    Règles appliquées, dans l'ordre où le marché les rencontrerait :
    1. si le stop courant est touché -> sortie au stop ;
    2. si +`secure_at_r` R est atteint -> le stop est remonté sur ce niveau et n'en redescend plus ;
    3. si l'objectif est touché -> sortie à l'objectif ;
    4. au-delà de `max_hold` bougies -> sortie au marché (le scénario a expiré).

    Quand stop et objectif tombent dans la MÊME bougie, on suppose le stop touché en premier : on ne
    sait pas dans quel ordre le prix a circulé à l'intérieur de la bougie, et l'hypothèse prudente
    est la seule honnête. C'est aussi ce qui empêche un backtest de surestimer sa réussite.
    """
    buy = direction == "BUY"
    initial_risk = abs(entry - stop)
    if initial_risk <= 0:
        return {"outcome": "invalid", "r": 0.0, "bars": 0, "secured": False}

    current_stop = stop
    secured = False
    secure_level = entry + (1 if buy else -1) * secure_at_r * initial_risk

    for i, candle in enumerate(bars[start + 1: start + 1 + max_hold], start=1):
        hit_stop = candle.low <= current_stop if buy else candle.high >= current_stop
        if hit_stop:
            r = (current_stop - entry) / initial_risk if buy else (entry - current_stop) / initial_risk
            return {"outcome": "won" if r > 0 else "lost", "r": round(r, 3), "bars": i,
                    "secured": secured, "exit": current_stop,
                    "exit_reason": "stop sécurisé" if secured else "stop initial"}
        # Sécurisation : le stop monte sur +2R et n'en redescend jamais.
        reached = (candle.high >= secure_level) if buy else (candle.low <= secure_level)
        if not secured and reached:
            current_stop = secure_level
            secured = True
        hit_target = candle.high >= target if buy else candle.low <= target
        if hit_target:
            r = (target - entry) / initial_risk if buy else (entry - target) / initial_risk
            return {"outcome": "won", "r": round(r, 3), "bars": i, "secured": secured,
                    "exit": target, "exit_reason": "objectif"}

    window = bars[start + 1: start + 1 + max_hold]
    if not window:
        return {"outcome": "invalid", "r": 0.0, "bars": 0, "secured": secured}
    close = window[-1].close
    r = (close - entry) / initial_risk if buy else (entry - close) / initial_risk
    return {"outcome": "expired", "r": round(r, 3), "bars": len(window), "secured": secured,
            "exit": close, "exit_reason": "expiration du scénario"}


# =======================================================================================
# Backtest d'une paire
# =======================================================================================
async def backtest_symbol(
    symbol: str, *, entry_tf: str = "1h", step: int = 2, overrides: dict | None = None,
) -> dict:
    """Rejoue la stratégie sur UNE paire et retourne ses trades reconstitués.

    `entry_tf` : unité de temps sur laquelle le déclencheur d'entrée est évalué ("1h" pour la passe
    portée, "15m" pour la passe fidélité). Le CONTEXTE (mensuel, journalier, 4 h) reste identique
    dans les deux cas — c'est bien la même stratégie qu'on mesure.

    `overrides` : paramètres de `playbook.build` à surcharger pour CE run (A/B tests : mode de
    volatilité, stop k×ATR 4 h…). Le run de référence n'en passe aucun.
    """
    s = get_settings()
    # Profondeur demandée : le maximum réellement servi par les fournisseurs (1 h ≈ 2,8 ans,
    # 15 min ≈ 80 jours). Demander plus ne renvoie pas plus — cf. `data_limits` du rapport.
    limits = {"1h": 20_000, "15m": 6_000}
    try:
        entry_bars = await _series(symbol, entry_tf, limits.get(entry_tf, 5_000))
        h1 = entry_bars if entry_tf == "1h" else await _series(symbol, "1h", 20_000)
        h4 = await _series(symbol, "4h", 6_000)
        daily = await _series(symbol, "1d", 2_500)
        monthly = await _series(symbol, "1M", 200)
    except NoRealDataError as exc:
        return {"symbol": symbol, "trades": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — une paire indisponible n'arrête pas le backtest
        logger.warning("Backtest %s : historique indisponible (%s)", symbol, exc)
        return {"symbol": symbol, "trades": [], "error": str(exc)}

    if len(entry_bars) < 300 or len(daily) < 120 or len(monthly) < 14:
        return {"symbol": symbol, "trades": [], "error": "historique insuffisant"}

    def _stamps(c: list[Candle]) -> list[float]:
        return [x.timestamp.timestamp() if x.timestamp else 0.0 for x in c]

    ctx = {"h1": (h1, _stamps(h1)), "h4": (h4, _stamps(h4)),
           "daily": (daily, _stamps(daily)), "monthly": (monthly, _stamps(monthly))}
    max_hold = _MAX_HOLD.get(entry_tf, 300)
    pip = pips_mod.pip_size(symbol, entry_bars[-1].close)
    trades: list[dict] = []
    open_until = -1                    # une seule position à la fois par paire (réalisme)

    for i in range(250, len(entry_bars) - max_hold - 1, max(1, step)):
        if i < open_until:
            continue
        stamp = entry_bars[i].timestamp
        if stamp is None:
            continue
        t = stamp.timestamp()
        sliced = {name: _slice_until(c, st, t, _TAIL[name]) for name, (c, st) in ctx.items()}
        if len(sliced["daily"]) < 60 or len(sliced["h4"]) < 60 or len(sliced["monthly"]) < 12:
            continue
        session = sessions_mod.session_context(stamp)
        tradable, _ = sessions_mod.can_trade(stamp)
        # Calcul PUR déporté dans un thread : un backtest complet appelle `build` des dizaines de
        # milliers de fois. Le laisser dans la boucle d'événements gelait l'API pendant tout le run.
        build_kwargs = dict(
            h1=sliced["h1"] or None,
            session=session,
            min_rr=s.playbook_min_rr, max_rr=s.playbook_max_rr,
            min_target_pips=s.playbook_min_target_pips,
            max_stop_pips=s.playbook_max_stop_pips,
            target_level_buffer=s.playbook_target_level_buffer,
            max_atr_multiple=s.playbook_max_atr_multiple,
            can_trade=(not s.playbook_trade_only_when_open) or tradable,
            # Mêmes filtres que la production : le backtest mesure la stratégie TELLE QU'ELLE TRADE.
            allow_divergence=s.playbook_allow_divergence_entry,
            volatility_filter=s.playbook_volatility_filter,
            volatility_mode=s.playbook_volatility_mode,
            max_atr_pct=s.playbook_max_atr_pct,
            volatility_max_widen=s.playbook_volatility_max_widen,
        )
        # Les surcharges REMPLACENT les réglages de base (un même mot-clé des deux côtés est
        # normal pour un A/B : `dict(a=1, **{"a": 2})` lèverait un TypeError).
        build_kwargs.update(overrides or {})
        setup = await asyncio.to_thread(
            playbook.build,
            symbol,
            sliced["monthly"], sliced["daily"], sliced["h4"],
            entry_bars[max(0, i - _TAIL["m15"] + 1): i + 1],
            **build_kwargs,
        )
        if not setup.ready or setup.entry is None:
            continue
        res = replay_trade(
            entry_bars, i, setup.direction, setup.entry, setup.stop_loss, setup.take_profit_1,
            max_hold=max_hold, secure_at_r=s.playbook_secure_at_r,
        )
        if res["outcome"] == "invalid":
            continue
        # Le MÊME trade rejoué SANS la sécurisation +2R : c'est ce qui permet le comparatif
        # « sécurisation on/off » sur des trades strictement identiques (aucun autre facteur ne bouge).
        res_plain = replay_trade(
            entry_bars, i, setup.direction, setup.entry, setup.stop_loss, setup.take_profit_1,
            max_hold=max_hold, secure_at_r=float("inf"),
        )
        open_until = i + res["bars"]
        sess = setup.session or {}
        trigger_type = (setup.trigger or "").split(" — ", 1)[0].strip() or "inconnu"
        trades.append({
            "symbol": symbol,
            "at": stamp.isoformat(),
            "direction": setup.direction,
            "trigger": trigger_type,
            "pair_trigger": f"{symbol}|{trigger_type}",
            "r_no_secure": res_plain["r"],
            "session": (sess.get("kill_zones") or ["hors_fenetre"])[0],
            "prime": bool(sess.get("prime")),
            "entry": setup.entry, "stop_loss": setup.stop_loss, "target": setup.take_profit_1,
            "risk_pips": round(setup.risk_pips, 1), "reward_pips": round(setup.reward_pips, 1),
            "planned_rr": round(setup.risk_reward, 2),
            "outcome": res["outcome"], "r": res["r"], "bars_held": res["bars"],
            "secured": res["secured"], "exit_reason": res.get("exit_reason", ""),
            "confidence": setup.confidence,
            # Contexte du PERDANT : c'est ce qui permet de dire ce que les perdants ont en commun.
            "adx": (setup.layers.get("daily", {}).get("metrics", {}) or {}).get("adx"),
            "atr_pct": (setup.layers.get("daily", {}).get("metrics", {}) or {}).get("atr_pct"),
            "rsi_daily": (setup.layers.get("daily", {}).get("metrics", {}) or {}).get("rsi"),
            "alignment": sum(
                1 for k in ("monthly", "daily", "h4", "h1", "m15")
                if (setup.layers.get(k, {}) or {}).get("bias") == setup.bias
            ),
            "stop_atr_daily": (
                round(abs(setup.entry - setup.stop_loss) /
                      ((setup.layers.get("daily", {}).get("metrics", {}) or {}).get("atr") or 1e9), 2)
            ),
        })

    covered = ""
    if entry_bars:
        covered = (f"{entry_bars[0].timestamp:%Y-%m-%d} → {entry_bars[-1].timestamp:%Y-%m-%d}")
    return {
        "symbol": symbol, "trades": trades, "entry_timeframe": entry_tf,
        "bars": len(entry_bars), "coverage": covered, "pip": pip,
        "years": round((entry_bars[-1].timestamp - entry_bars[0].timestamp).days / 365.25, 2)
        if entry_bars else 0.0,
    }


# =======================================================================================
# Agrégation & classement
# =======================================================================================
def metrics(trades: list[dict]) -> dict:
    """Statistiques complètes d'un lot de trades — tout ce qu'on veut lire après un backtest."""
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "expired": 0, "win_rate": 0.0,
                "expectancy_r": 0.0, "profit_factor": None, "total_r": 0.0,
                "avg_win_r": 0.0, "avg_loss_r": 0.0, "avg_planned_rr": 0.0,
                "avg_bars_held": 0.0, "secured_rate": 0.0, "max_drawdown_r": 0.0}
    wins = [t for t in trades if t["r"] > 0]
    losses = [t for t in trades if t["r"] <= 0]
    gross_win = sum(t["r"] for t in wins)
    gross_loss = -sum(t["r"] for t in losses)

    # Drawdown en R sur la courbe cumulée des résultats (dans l'ordre chronologique).
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["at"]):
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "expired": sum(1 for t in trades if t["outcome"] == "expired"),
        "win_rate": round(len(wins) / n * 100, 1),
        "expectancy_r": round(sum(t["r"] for t in trades) / n, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_r": round(sum(t["r"] for t in trades), 2),
        "avg_win_r": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_r": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_planned_rr": round(sum(t["planned_rr"] for t in trades) / n, 2),
        "avg_reward_pips": round(sum(t["reward_pips"] for t in trades) / n, 1),
        "avg_risk_pips": round(sum(t["risk_pips"] for t in trades) / n, 1),
        "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 1),
        "secured_rate": round(sum(1 for t in trades if t["secured"]) / n * 100, 1),
        "max_drawdown_r": round(max_dd, 2),
    }


def _group(trades: list[dict], key: str) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        buckets.setdefault(str(t.get(key)), []).append(t)
    return {k: metrics(v) for k, v in buckets.items()}


def losers_profile(trades: list[dict], min_trades: int = 3) -> dict:
    """CE QUE LES PERDANTS ONT EN COMMUN — la partie la plus utile d'un backtest.

    On compare le profil moyen des trades stoppés à celui des gagnants, facteur par facteur. Un
    écart net désigne une condition de marché où la stratégie ne fonctionne pas : c'est là qu'il
    faut ajouter un filtre, pas ailleurs.
    """
    losers = [t for t in trades if t["r"] <= 0]
    winners = [t for t in trades if t["r"] > 0]
    if len(losers) < min_trades:
        return {"sample": len(losers), "note": "trop peu de perdants pour dégager un profil fiable"}

    def avg(rows: list[dict], field: str) -> float | None:
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    findings: list[str] = []
    comparisons: dict[str, dict] = {}
    for field, label, sense in (
        ("adx", "force de tendance (ADX journalier)", "bas"),
        ("alignment", "unités de temps alignées (sur 5)", "bas"),
        ("confidence", "confiance de la stratégie", "bas"),
        ("atr_pct", "volatilité journalière (ATR %)", "haut"),
        ("stop_atr_daily", "largeur du stop (× ATR journalier)", "bas"),
        ("risk_pips", "distance au stop (pips)", "bas"),
    ):
        lo, wi = avg(losers, field), avg(winners, field)
        if lo is None or wi is None or wi == 0:
            continue
        delta_pct = round((lo - wi) / abs(wi) * 100, 1)
        comparisons[field] = {"label": label, "perdants": lo, "gagnants": wi, "ecart_pct": delta_pct}
        if abs(delta_pct) >= 12:
            direction = "plus faible" if delta_pct < 0 else "plus élevée"
            findings.append(
                f"{label} : {lo} chez les perdants contre {wi} chez les gagnants "
                f"({direction} de {abs(delta_pct):.0f} %)"
            )

    # Répartitions : y a-t-il un déclencheur, une session ou un sens qui perd systématiquement ?
    def share(rows: list[dict], field: str) -> dict[str, float]:
        c = Counter(r[field] for r in rows)
        return {k: round(v / len(rows) * 100, 1) for k, v in c.most_common()}

    by_trigger_l, by_trigger_w = share(losers, "trigger"), share(winners, "trigger")
    for trig, pct in by_trigger_l.items():
        won = by_trigger_w.get(trig, 0.0)
        if pct - won >= 12:
            findings.append(
                f"déclencheur « {trig} » : {pct} % des perdants contre {won} % des gagnants"
            )
    by_sess_l, by_sess_w = share(losers, "session"), share(winners, "session")
    for sess, pct in by_sess_l.items():
        won = by_sess_w.get(sess, 0.0)
        if pct - won >= 12:
            findings.append(f"fenêtre « {sess} » : {pct} % des perdants contre {won} % des gagnants")

    return {
        "sample": len(losers),
        "winners_sample": len(winners),
        "comparisons": comparisons,
        "trigger_share_losers": by_trigger_l,
        "trigger_share_winners": by_trigger_w,
        "session_share_losers": by_sess_l,
        "avg_bars_to_stop": avg(losers, "bars_held"),
        "findings": findings or [
            "aucun facteur ne distingue nettement les perdants des gagnants : les pertes semblent "
            "réparties au hasard, ce qui est le comportement attendu d'une stratégie sans biais "
            "exploitable résiduel"
        ],
    }


def rank_pairs(by_symbol: dict[str, dict], min_trades: int) -> list[dict]:
    """Classement des paires par FIABILITÉ mesurée, du meilleur au moins bon.

    Le critère principal est l'espérance en R : elle combine le taux de réussite et le rapport
    gain/perte, là où le seul taux de réussite peut être flatteur (beaucoup de petits gains, quelques
    grosses pertes). Les paires sous le seuil d'échantillon sont classées à part : on ne les note
    pas, on dit qu'on ne sait pas.
    """
    rated, unrated = [], []
    for symbol, m in by_symbol.items():
        row = {"symbol": symbol, **m}
        (rated if m["trades"] >= min_trades else unrated).append(row)
    # Tri : espérance d'abord, puis profit factor (infini quand il n'y a aucun perdant), puis
    # taux de réussite.
    rated.sort(
        key=lambda r: (r["expectancy_r"],
                       float("inf") if r["profit_factor"] is None else r["profit_factor"],
                       r["win_rate"]),
        reverse=True,
    )
    for i, row in enumerate(rated, 1):
        row["rank"] = i
        row["verdict"] = _verdict(row)
    for row in unrated:
        row["rank"] = None
        row["verdict"] = f"non classé — {row['trades']} trade(s), échantillon insuffisant"
    unrated.sort(key=lambda r: -r["trades"])
    return rated + unrated


def _verdict(row: dict) -> str:
    """Verdict lisible d'une paire, sans enjoliver un résultat négatif.

    Le profit factor vaut `None` quand la paire n'a AUCUN trade perdant : il est alors infini, pas
    nul. Le confondre avec zéro déclasserait précisément les meilleures paires.
    """
    e, wr = row["expectancy_r"], row["win_rate"]
    pf = row["profit_factor"]
    pf_txt = "∞ (aucun perdant)" if pf is None else f"{pf}"
    pf_value = float("inf") if pf is None else pf
    if e >= 0.25 and pf_value >= 1.5:
        return f"✅ exploitable — espérance {e:+.2f} R, profit factor {pf_txt}, réussite {wr} %"
    if e > 0:
        return f"🟡 marginal — espérance {e:+.2f} R, profit factor {pf_txt} : positif mais fragile"
    return f"🔴 non exploitable — espérance {e:+.2f} R sur {row['trades']} trades : la stratégie perd ici"


# =======================================================================================
# Passe complète
# =======================================================================================
def secure_ab(trades: list[dict]) -> dict:
    """Comparatif « sécurisation +2R » ON/OFF sur les MÊMES trades (plan, tâche 2.6).

    Chaque trade porte son résultat avec la règle (`r`) et sans elle (`r_no_secure`) : la
    différence mesure exactement ce que la sécurisation coûte ou rapporte. Informatif — la règle
    de discipline reste en vigueur quoi qu'il arrive, mais on veut connaître son prix.
    """
    rows = [t for t in trades if t.get("r_no_secure") is not None]
    if not rows:
        return {"note": "aucun trade comparable", "trades": 0}
    n = len(rows)
    with_secure = sum(t["r"] for t in rows) / n
    without = sum(t["r_no_secure"] for t in rows) / n
    changed = sum(1 for t in rows if abs(t["r"] - t["r_no_secure"]) > 1e-9)
    return {
        "trades": n,
        "changed_by_rule": changed,
        "expectancy_with_secure_r": round(with_secure, 3),
        "expectancy_without_secure_r": round(without, 3),
        "delta_r": round(with_secure - without, 3),
        "note": (
            f"La sécurisation +2R a modifié l'issue de {changed}/{n} trades ; espérance "
            f"{with_secure:+.3f} R avec la règle contre {without:+.3f} R sans "
            f"({with_secure - without:+.3f} R par trade). La règle reste appliquée : c'est une "
            "mesure, pas une décision."
        ),
    }


async def run_pass(
    universe: list[str], *, entry_tf: str, step: int, parallel: int = 4,
    overrides: dict | None = None,
) -> dict:
    """Une passe de backtest sur tout l'univers, pour une unité de temps d'entrée donnée."""
    started = datetime.now(UTC)
    sem = asyncio.Semaphore(max(1, parallel))

    async def _one(symbol: str) -> dict:
        async with sem:
            return await backtest_symbol(symbol, entry_tf=entry_tf, step=step, overrides=overrides)

    results = await asyncio.gather(*(_one(sym) for sym in universe), return_exceptions=True)

    trades: list[dict] = []
    failures: list[dict] = []
    coverage: dict[str, str] = {}
    years: list[float] = []
    for symbol, res in zip(universe, results, strict=True):
        if isinstance(res, Exception):
            failures.append({"symbol": symbol, "error": str(res)})
            continue
        if res.get("error"):
            failures.append({"symbol": symbol, "error": res["error"]})
            continue
        trades.extend(res["trades"])
        coverage[symbol] = res.get("coverage", "")
        if res.get("years"):
            years.append(res["years"])

    s = get_settings()
    min_trades = s.playbook_training_min_trades
    by_symbol = _group(trades, "symbol")
    return {
        "entry_timeframe": entry_tf,
        "generated_at": started.isoformat(),
        "duration_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "pairs_tested": len(universe) - len(failures),
        "failures": failures,
        "coverage": coverage,
        "years_covered": round(sum(years) / len(years), 2) if years else 0.0,
        "overall": metrics(trades),
        "by_symbol": by_symbol,
        "ranking": rank_pairs(by_symbol, min_trades),
        "by_trigger": _group(trades, "trigger"),
        "by_session": _group(trades, "session"),
        "by_direction": _group(trades, "direction"),
        # Matrice PAIRE × DÉCLENCHEUR (plan, tâche 2.3) : c'est elle qui permet de désactiver un
        # déclencheur là où il est mesuré perdant, sans le condamner partout.
        "by_pair_trigger": _group(trades, "pair_trigger"),
        # Comparatif sécurisation +2R on/off (plan, tâche 2.6) — mêmes trades, deux rejeux.
        "secure_ab": secure_ab(trades),
        "losers_profile": losers_profile(trades),
        "min_trades": min_trades,
        "trades": len(trades),
        # Journal COMPLET des trades rejoués : indispensable aux méta-analyses (simulation de
        # sizing, ventilations futures) sans avoir à relancer dix minutes de calcul.
        "trade_log": trades,
    }


async def run_backtest(store=None, *, universe: list[str] | None = None,  # noqa: ANN001
                       step_1h: int = 2, step_15m: int = 4) -> dict:
    """Backtest COMPLET de la stratégie : passe portée (1 h) + passe fidélité (15 min).

    Le résultat contient tout ce qu'il faut pour décider : nombre de trades, gagnants, perdants,
    taux de réussite, R/R moyen, classement des paires par fiabilité, et le profil commun des
    trades qui ont touché le stop.
    """
    universe = universe or DEFAULT_UNIVERSE
    if _state["running"]:
        raise RuntimeError("un backtest est déjà en cours — attendre qu'il se termine")
    _state.update(running=True, started_at=datetime.now(UTC).isoformat())
    try:
        _set_phase("passe portée (1 h)", 0, len(universe))
        scope = await run_pass(universe, entry_tf="1h", step=step_1h)
        _set_phase("passe fidélité (15 min)", 0, len(universe))
        fidelity = await run_pass(universe, entry_tf="15m", step=step_15m)
    finally:
        _state.update(running=False, phase=None)

    payload = {
        "date": datetime.now(UTC).date().isoformat(),
        "strategy": (
            "Playbook MTF — Mensuel/Journalier → 4 h → 1 h → entrée 15 min · objectif ≥ 200 pips · "
            "R/R 1:2–1:3 · stop sécurisé à +2R"
        ),
        "universe": universe,
        "scope": scope,          # 1 h, profondeur maximale
        "fidelity": fidelity,    # 15 min, le vrai déclencheur, sur l'historique disponible
        "data_limits": {
            "note": (
                "Le 15 min n'est pas disponible gratuitement au-delà de ~80 jours (Yahoo refuse "
                "explicitement les périodes plus anciennes) et le 1 h plafonne à ~2,8 ans. La passe "
                "PORTÉE mesure donc la stratégie avec un déclencheur évalué en 1 h sur toute la "
                "profondeur disponible, et la passe FIDÉLITÉ vérifie sur le vrai 15 min ce que la "
                "première a mesuré."
            ),
            "m15_days": 81,
            "h1_years": 2.8,
            "daily_years": 10,
        },
        "conclusion": {},
    }
    payload["conclusion"] = write_conclusion(payload)
    if store is not None:
        try:
            store.records.put(COLLECTION, payload["date"], payload)
            store.records.put(COLLECTION, LATEST, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Backtest non persisté (%s)", exc)
        # Verdicts par paire (🟢/🟡/🔴) : mis à jour à CHAQUE passage — c'est eux qui gouvernent
        # l'auto-entrée et le sizing (plan, tâches 2.1/2.2).
        try:
            from app.services import verdict_service

            verdicts = verdict_service.update_from_backtest(store, payload)
            if verdicts:
                payload["pair_verdicts"] = verdicts["pairs"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Verdicts par paire non mis à jour (%s)", exc)
    logger.info(
        "Backtest playbook : %d trades (1 h) / %d trades (15 min) sur %d paires",
        scope["trades"], fidelity["trades"], len(universe),
    )
    return payload


def write_conclusion(payload: dict) -> dict:
    """Conclusion RÉDIGÉE, marché par marché et paire par paire — uniquement des chiffres mesurés."""
    scope, fidelity = payload["scope"], payload["fidelity"]
    o = scope["overall"]
    lines: list[str] = []

    if not o["trades"]:
        return {"headline": "Aucun trade conforme à la stratégie sur la période testée.",
                "lines": ["La cascade complète (5 étapes + R/R 1:2–1:3 + 200 pips) n'a jamais été "
                          "entièrement satisfaite sur l'historique disponible."],
                "markets": {}, "verdict": "non mesurable"}

    verdict = ("exploitable" if o["expectancy_r"] >= 0.25 and (o["profit_factor"] or 0) >= 1.5
               else "marginal" if o["expectancy_r"] > 0 else "non exploitable")
    headline = (
        f"Sur {scope['years_covered']} ans et {scope['pairs_tested']} paires, la stratégie a produit "
        f"{o['trades']} trades : {o['wins']} gagnants et {o['losses']} perdants, soit "
        f"{o['win_rate']} % de réussite, pour une espérance de {o['expectancy_r']:+.2f} R par trade "
        f"et un profit factor de {o['profit_factor']}. Verdict global : {verdict}."
    )

    lines.append(
        f"R/R moyen PLANIFIÉ : 1:{o['avg_planned_rr']} (objectif moyen {o['avg_reward_pips']} pips "
        f"pour {o['avg_risk_pips']} pips risqués). Gain moyen d'un gagnant : {o['avg_win_r']:+.2f} R ; "
        f"perte moyenne d'un perdant : {o['avg_loss_r']:+.2f} R."
    )
    lines.append(
        f"La sécurisation du stop à +2R s'est déclenchée sur {o['secured_rate']} % des trades. "
        f"Durée moyenne de détention : {o['avg_bars_held']} bougies. Pire série de pertes cumulées : "
        f"{o['max_drawdown_r']} R."
    )

    # Marchés : métaux vs devises.
    markets: dict[str, dict] = {}
    for label, syms in (("Métaux précieux", METALS), ("Forex", FOREX_PAIRS)):
        rows = [r for r in scope["ranking"] if r["symbol"] in syms and r["trades"]]
        if not rows:
            continue
        total = sum(r["trades"] for r in rows)
        exp = sum(r["expectancy_r"] * r["trades"] for r in rows) / total
        wins = sum(r["wins"] for r in rows)
        markets[label] = {
            "pairs": len(rows), "trades": total,
            "win_rate": round(wins / total * 100, 1),
            "expectancy_r": round(exp, 3),
            "best": rows[0]["symbol"] if rows else None,
        }
        lines.append(
            f"{label} : {total} trades sur {len(rows)} instrument(s), {round(wins / total * 100, 1)} % "
            f"de réussite, espérance {exp:+.2f} R. Meilleur instrument : {rows[0]['symbol']} "
            f"({rows[0]['expectancy_r']:+.2f} R)."
        )

    # Déclencheurs et fenêtres.
    for label, group in (("déclencheur", scope["by_trigger"]), ("fenêtre de session", scope["by_session"])):
        rated = [(k, m) for k, m in group.items() if m["trades"] >= scope["min_trades"]]
        if rated:
            rated.sort(key=lambda kv: kv[1]["expectancy_r"], reverse=True)
            detail = " · ".join(
                f"{k} : {m['trades']} trades, {m['win_rate']} %, {m['expectancy_r']:+.2f} R"
                for k, m in rated
            )
            lines.append(f"Par {label} — {detail}.")

    # Ce que les perdants ont en commun.
    lp = scope["losers_profile"]
    if lp.get("findings"):
        lines.append("Points communs des trades qui ont touché le stop : " + " ; ".join(lp["findings"]) + ".")
    if lp.get("avg_bars_to_stop"):
        lines.append(f"Un perdant est stoppé en moyenne au bout de {lp['avg_bars_to_stop']} bougies.")

    # Croisement avec la passe 15 min — et SEULEMENT si l'échantillon permet de conclure.
    fo = fidelity["overall"]
    min_compare = max(scope.get("min_trades", 8), 8)
    if fo["trades"] >= min_compare:
        delta = fo["expectancy_r"] - o["expectancy_r"]
        sense = "confirme" if abs(delta) < 0.15 else ("améliore" if delta > 0 else "dégrade")
        lines.append(
            f"Contrôle sur le VRAI déclencheur 15 min ({fidelity['years_covered']} an(s) "
            f"disponibles) : {fo['trades']} trades, {fo['win_rate']} % de réussite, espérance "
            f"{fo['expectancy_r']:+.2f} R. Le passage au 15 min {sense} le résultat de la passe 1 h "
            f"({delta:+.2f} R d'écart)."
        )
    elif fo["trades"]:
        # Comparer deux espérances sur 1 ou 2 trades n'a aucun sens statistique : on donne le
        # chiffre brut et on dit explicitement qu'il ne conclut rien.
        lines.append(
            f"Contrôle 15 min : seulement {fo['trades']} trade(s) sur les ~80 jours de 15 min "
            f"disponibles ({fo['win_rate']} % de réussite, {fo['expectancy_r']:+.2f} R). "
            f"C'est très en dessous des {min_compare} trades nécessaires pour comparer quoi que ce "
            f"soit : ce chiffre ne confirme NI n'infirme la passe 1 h."
        )
    else:
        lines.append(
            "Contrôle 15 min : aucun trade conforme sur les ~80 jours disponibles — l'échantillon "
            "est trop court pour confirmer ou infirmer la passe 1 h."
        )

    return {"headline": headline, "verdict": verdict, "lines": lines, "markets": markets}


# =======================================================================================
# A/B VOLATILITÉ (plan, tâche 2.4) — un seul run, trois réponses candidates au même constat
# =======================================================================================
# Constat mesuré : les trades stoppés ont un ATR journalier 21 % plus haut que les gagnants.
# Trois réponses possibles, à départager par la MESURE et non par préférence :
#   - adapt      : stop élargi proportionnellement à l'excès de volatilité (réglage actuel) ;
#   - refuse     : aucun trade au-dessus du seuil ;
#   - stop_atr4h : stop posé à k × ATR 4 h (borné par la bande de R/R) au lieu de la structure.
AB_COLLECTION = "playbook_volatility_ab"

VOLATILITY_VARIANTS: dict[str, dict] = {
    "adapt": {"volatility_filter": True, "volatility_mode": "adapt"},
    "refuse": {"volatility_filter": True, "volatility_mode": "refuse"},
    "stop_atr4h": {"volatility_filter": False, "stop_mode": "atr4h"},
}


async def run_volatility_ab(
    store=None, *, universe: list[str] | None = None, step: int = 3,  # noqa: ANN001
) -> dict:
    """Rejoue la passe portée (1 h) une fois PAR VARIANTE et classe les trois par espérance.

    Le gagnant n'est pas appliqué automatiquement : changer la stratégie est une décision, le rôle
    du run est de la rendre possible avec des chiffres. Le résultat est persisté pour l'interface.
    """
    universe = universe or DEFAULT_UNIVERSE
    if _state["running"]:
        raise RuntimeError("un backtest est déjà en cours — attendre qu'il se termine")
    _state.update(running=True, started_at=datetime.now(UTC).isoformat())
    results: dict[str, dict] = {}
    try:
        for name, overrides in VOLATILITY_VARIANTS.items():
            _set_phase(f"A/B volatilité — variante « {name} »", 0, len(universe))
            one = await run_pass(universe, entry_tf="1h", step=step, overrides=overrides)
            results[name] = {
                "overall": one["overall"],
                "by_symbol": one["by_symbol"],
                "failures": one["failures"],
                "trades": one["trades"],
            }
    finally:
        _state.update(running=False, phase=None)

    ranked = sorted(
        results.items(),
        key=lambda kv: (kv[1]["overall"]["expectancy_r"], kv[1]["overall"]["profit_factor"] or 0.0),
        reverse=True,
    )
    winner = ranked[0][0] if ranked else None
    payload = {
        "date": datetime.now(UTC).date().isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "universe": universe,
        "step": step,
        "variants": results,
        "winner": winner,
        "current_setting": get_settings().playbook_volatility_mode
        if get_settings().playbook_volatility_filter else "off",
        "note": (
            " · ".join(
                f"{name} : {r['overall']['trades']} trades, {r['overall']['win_rate']} %, "
                f"{r['overall']['expectancy_r']:+.2f} R"
                for name, r in ranked
            )
            + (f" — variante gagnante : « {winner} ». Elle n'est PAS appliquée automatiquement : "
               "figer un changement de stratégie reste une décision de l'utilisateur."
               if winner else "")
        ),
    }
    if store is not None:
        try:
            store.records.put(AB_COLLECTION, payload["date"], payload)
            store.records.put(AB_COLLECTION, LATEST, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("A/B volatilité non persisté (%s)", exc)
    logger.info("A/B volatilité terminé : %s", payload["note"])
    return payload
