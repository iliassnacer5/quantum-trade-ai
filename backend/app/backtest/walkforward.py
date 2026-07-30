"""Validation walk-forward (out-of-sample) — honnêteté de la performance.

Le moteur de signaux n'a pas de paramètres « entraînés » : la stratégie est fixe. La vraie question
n'est donc pas l'optimisation mais la **robustesse dans le temps** : la stratégie tient-elle sur
plusieurs périodes successives, ou ne « marche » que sur un segment de chance ?

On découpe l'historique en `folds` segments consécutifs, on backteste chacun INDÉPENDAMMENT, et on
mesure la **cohérence** (combien de segments sont profitables). Un edge réel est régulier ; un
sur-apprentissage / coup de chance se voit à l'incohérence entre segments. Le verdict est prudent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.backtest.engine import run_backtest
from app.backtest.schemas import BacktestConfig
from app.data.ohlcv import get_ohlcv
from app.domain.indicators import Candle

logger = logging.getLogger(__name__)

_WINDOW = 60  # bougies minimales requises par l'engine pour calculer les indicateurs
_MIN_FOLD = _WINDOW + 60  # taille de segment minimale pour produire des trades exploitables


def _cap_pf(pf: float) -> float:
    """Borne le profit factor (peut être infini si aucune perte) pour l'agrégation."""
    return min(pf, 10.0) if pf != float("inf") else 10.0


async def walk_forward(
    symbol: str, timeframe: str = "1h", folds: int = 4, initial_capital: float = 10_000.0,
    exit_config: dict | None = None,
    preloaded: tuple[list[Candle], bool] | None = None,
) -> dict:
    """Backteste `symbol` sur `folds` segments temporels successifs et juge la cohérence.

    Le desk n'a qu'UNE stratégie — celle du playbook — et c'est elle qui est validée ici, via le
    moteur multi-agents qu'elle pilote. `preloaded=(candles, data_real)` réutilise des bougies déjà
    chargées, ce qui évite un rechargement par symbole × unité de temps.
    """
    strategy = None
    if preloaded is not None:
        candles, data_real = preloaded
    else:
        rows = await get_ohlcv(symbol, interval=timeframe, limit=1000)
        data_real = len(rows) >= 100
        candles = []
        if data_real:
            candles = [
                Candle(r["open"], r["high"], r["low"], r["close"], r.get("volume", 0.0),
                       timestamp=datetime.fromtimestamp(r["time"], UTC))
                for r in rows
            ]
    if not candles:
        # Données indisponibles -> repli synthétique, signalé honnêtement.
        from datetime import timedelta

        from app.data.synthetic import generate_candles
        base = datetime.now(UTC) - timedelta(hours=1000)
        candles = [
            Candle(c.open, c.high, c.low, c.close, c.volume, timestamp=base + timedelta(hours=i))
            for i, c in enumerate(generate_candles(n=1000, seed=abs(hash(symbol)) % 10_000))
        ]

    # Ajuste le nombre de segments à l'historique disponible.
    usable = max(1, len(candles) // _MIN_FOLD)
    folds = max(1, min(folds, usable))
    fold_size = len(candles) // folds

    fold_results: list[dict] = []
    for i in range(folds):
        seg = candles[i * fold_size: (i + 1) * fold_size] if i < folds - 1 else candles[i * fold_size:]
        if len(seg) < _MIN_FOLD:
            continue
        cfg = BacktestConfig(
            symbol=symbol, timeframe=timeframe,
            start_time=seg[0].timestamp, end_time=seg[-1].timestamp,
            initial_capital=initial_capital, use_llm=False,
        )
        report = await run_backtest(cfg, seg, tenant_id="wf", strategy=strategy, exit_config=exit_config)
        m = report.metrics
        fold_results.append({
            "fold": i + 1,
            "from": seg[0].timestamp.date().isoformat(),
            "to": seg[-1].timestamp.date().isoformat(),
            "trades": m.total_trades,
            "win_rate": round(m.win_rate * 100, 1),
            "profit_factor": m.profit_factor,
            "pnl_pct": m.total_pnl_pct,
            "benchmark_pct": report.benchmark_pnl_pct,
            "alpha_pct": report.alpha_pct,
            "max_drawdown_pct": m.max_drawdown_pct,
            "profitable": m.total_pnl_pct > 0,
            "beats_hold": report.alpha_pct > 0,
        })

    return _summarize(symbol, timeframe, fold_results, data_real)


async def validate_expert_agent(market_type: str, symbols: list[str], timeframe: str = "1h", folds: int = 4) -> dict:
    """Walk-forward du MOTEUR multi-agents (avec l'expert du marché) sur plusieurs symboles.

    Mesure l'effet réel des agents experts par marché : win-rate, profit factor, alpha, verdict.
    Ex. validate_expert_agent("crypto", ["BTC/USDT","ETH/USDT","SOL/USDT"])."""
    results = []
    for sym in symbols:
        try:
            results.append(await walk_forward(sym, timeframe, folds=folds))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Validation expert %s échouée (%s)", sym, exc)
    n = len(results) or 1
    avg = lambda k: round(sum(r.get(k, 0) for r in results) / n, 2)  # noqa: E731
    robust = sum(1 for r in results if r["verdict"] == "robuste")
    return {
        "market_type": market_type, "symbols": len(results),
        "robust": robust, "avg_win_rate": avg("avg_win_rate"),
        "avg_profit_factor": avg("avg_profit_factor"), "avg_alpha_pct": avg("avg_alpha_pct"),
        "verdict": "✅ Fiable" if robust >= max(1, len(results) // 2) else "⚠️ À renforcer",
        "results": results,
    }


def _summarize(symbol: str, timeframe: str, folds: list[dict], data_real: bool) -> dict:
    """Agrège les segments en un verdict honnête et prudent."""
    evaluated = [f for f in folds if f["trades"] > 0]
    n = len(evaluated)
    total_trades = sum(f["trades"] for f in folds)
    profitable = sum(1 for f in evaluated if f["profitable"])
    consistency = round(profitable / n, 2) if n else 0.0
    avg_win_rate = round(sum(f["win_rate"] for f in evaluated) / n, 1) if n else 0.0
    avg_pf = round(sum(_cap_pf(f["profit_factor"]) for f in evaluated) / n, 2) if n else 0.0
    avg_pnl = round(sum(f["pnl_pct"] for f in evaluated) / n, 2) if n else 0.0
    avg_alpha = round(sum(f.get("alpha_pct", 0.0) for f in evaluated) / n, 2) if n else 0.0
    beats_hold = sum(1 for f in evaluated if f.get("beats_hold"))

    # Verdict prudent (coûts inclus) : régularité out-of-sample ET valeur ajoutée vs « buy & hold ».
    if not data_real:
        verdict = "non_prouve"  # données synthétiques : aucune preuve possible
        label = "⚠️ Non prouvé — données synthétiques (pas de marché réel)"
    elif total_trades < 20 or n < 2:
        verdict = "insuffisant"
        label = "⏳ Échantillon insuffisant — pas assez de trades pour conclure"
    elif consistency >= 0.75 and avg_pf >= 1.3 and avg_win_rate >= 50 and avg_alpha > 0:
        verdict = "robuste"
        label = "✅ Robuste — régulier, rentable APRÈS frais et meilleur que « buy & hold »"
    elif consistency >= 0.5 and avg_pf >= 1.0:
        verdict = "fragile"
        label = "⚠️ Fragile — mitigé selon les périodes / n'ajoute pas clairement de valeur"
    else:
        verdict = "non_prouve"
        label = "🔴 Non prouvé — incohérent, pas d'edge démontré après frais"

    return {
        "symbol": symbol, "timeframe": timeframe, "strategy_id": "playbook",
        "folds": folds, "folds_evaluated": n,
        "total_trades": total_trades,
        "profitable_folds": profitable,
        "beats_hold_folds": beats_hold,
        "consistency": consistency,
        "avg_win_rate": avg_win_rate,
        "avg_profit_factor": avg_pf,
        "avg_pnl_pct": avg_pnl,
        "avg_alpha_pct": avg_alpha,
        "data_real": data_real,
        "verdict": verdict,
        "label": label,
    }
