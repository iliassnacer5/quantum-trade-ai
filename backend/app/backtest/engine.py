"""Moteur de backtest (Event-driven / Replay)."""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import uuid4

from contextlib import contextmanager

from typing import Callable

from app.backtest.metrics import compute_metrics
from app.backtest.schemas import BacktestConfig, BacktestEquityPoint, BacktestReport, BacktestTrade
from app.core.config import get_settings
from app.domain import indicators as ind
from app.domain.indicators import Candle
from app.domain.risk import RiskParams, compute_levels
from app.models.signal import Direction
from app.signal_engine import engine as signal_engine
from app.signal_engine import quality

logger = logging.getLogger(__name__)


@contextmanager
def _llm_mode(enabled: bool):
    """Force (dé)active le LLM pour la durée du backtest.

    Par défaut un backtest tourne en mode déterministe (`use_llm=False`) : sans cela, chaque
    bougie déclencherait un appel réseau LLM par agent → exécution de plusieurs minutes et
    résultats non reproductibles. On restaure l'état global à la sortie.
    """
    settings = get_settings()
    previous = settings.llm_enabled
    settings.llm_enabled = enabled
    try:
        yield
    finally:
        settings.llm_enabled = previous


async def run_backtest(
    config: BacktestConfig,
    candles: list[Candle],
    tenant_id: str = "local",
    strategy: Callable[[list[Candle]], Direction] | None = None,
    exit_config: dict | None = None,
) -> BacktestReport:
    """
    Exécute un backtest sur l'historique fourni en rejouant les bougies.

    - Sans `strategy` : utilise le moteur multi-agents (déterministe sauf si `config.use_llm`).
    - Avec `strategy` : utilise une stratégie classique (EMA, RSI, MACD, Bollinger, Donchian…).
    - `exit_config` (A/B test des sorties) : {"trailing": bool, "trailing_mult": float,
      "breakeven_r": float, "staged_tp": bool} — sinon les réglages globaux s'appliquent.
    Dans les deux cas, SL/TP sont calculés via l'ATR pour une comparaison homogène.
    """
    with _llm_mode(config.use_llm):
        return await _run_backtest_inner(config, candles, tenant_id, strategy, exit_config)


async def _run_backtest_inner(
    config: BacktestConfig,
    candles: list[Candle],
    tenant_id: str,
    strategy: Callable[[list[Candle]], Direction] | None = None,
    exit_config: dict | None = None,
) -> BacktestReport:
    trades: list[BacktestTrade] = []
    equity_points: list[BacktestEquityPoint] = []

    capital = config.initial_capital
    peak_capital = capital
    current_position = None

    # Paramètres de risque par défaut pour le backtest
    risk = RiskParams(
        capital=capital,
        risk_per_trade_pct=config.risk_per_trade_pct,
    )

    # Horodatage robuste : utilise candle.timestamp si présent, sinon synthétise (1h/bougie).
    def cts(idx: int):
        c = candles[idx]
        return c.timestamp or (config.start_time + timedelta(hours=idx))

    # Historique de travail pour l'engine (besoin d'au moins 50 bougies pour les EMA)
    window_size = 60

    # Réalisme : coûts de transaction (frais + slippage par côté) et stops dynamiques.
    s = get_settings()
    ec = exit_config or {}
    cost_rate = (s.backtest_fee_pct + s.backtest_slippage_pct) / 100.0
    trail_on = ec.get("trailing", s.backtest_trailing_stop)
    trail_mult = ec.get("trailing_mult", s.backtest_trailing_atr_mult)
    be_r = ec.get("breakeven_r", s.backtest_breakeven_at_r)
    staged_tp = ec.get("staged_tp", False)  # TP étagé : moitié à 1,5R, le reste court avec trailing

    def _realize(pos: dict, exit_price: float, exit_idx: int, size: float, note: str = "") -> None:
        """Réalise le P&L d'une quantité `size` au prix `exit_price` (coûts inclus des deux côtés)."""
        nonlocal capital
        gross = (exit_price - pos["entry_price"]) * size
        if pos["direction"] == Direction.SELL.value:
            gross = -gross
        cost = (pos["entry_price"] + exit_price) * size * cost_rate
        pnl = gross - cost
        capital += pnl
        risk.capital = capital
        duration = (cts(exit_idx) - pos["entry_time"]).total_seconds() / 60.0
        trades.append(BacktestTrade(
            id=str(uuid4()), symbol=config.symbol, direction=pos["direction"],
            entry_time=pos["entry_time"], exit_time=cts(exit_idx),
            entry_price=pos["entry_price"], exit_price=exit_price, size=size,
            pnl=round(pnl, 2), pnl_pct=round((pnl / capital * 100) if capital else 0.0, 2),
            duration_minutes=round(duration, 2), signal_rationale=(pos["rationale"] + note),
        ))

    def close_position(pos: dict, exit_price: float, exit_idx: int) -> None:
        """Clôture TOTALE de la position restante."""
        _realize(pos, exit_price, exit_idx, pos["size"])

    for i in range(window_size, len(candles)):
        current_candle = candles[i]
        history = candles[i - window_size : i + 1]
        
        # Enregistrer l'équité à chaque étape (simplifié : mark-to-market sur position ouverte)
        current_equity = capital
        if current_position:
            # PnL latent (approximatif)
            if current_position["direction"] == Direction.BUY.value:
                latent = (current_candle.close - current_position["entry_price"]) * current_position["size"]
            else:
                latent = (current_position["entry_price"] - current_candle.close) * current_position["size"]
            current_equity += latent
        
        peak_capital = max(peak_capital, current_equity)
        drawdown = ((peak_capital - current_equity) / peak_capital) * 100 if peak_capital > 0 else 0
        
        equity_points.append(BacktestEquityPoint(
            timestamp=cts(i),
            equity=round(current_equity, 2),
            drawdown_pct=round(drawdown, 2)
        ))
        
        # Gestion position existante : stops dynamiques puis détection SL/TP.
        if current_position:
            pos = current_position
            is_buy = pos["direction"] == Direction.BUY.value
            entry_p, R, atr_p = pos["entry_price"], pos["init_risk"], pos["atr"]

            # Profit courant en multiples de R (risque initial).
            in_profit_r = ((current_candle.high - entry_p) / R if is_buy
                           else (entry_p - current_candle.low) / R) if R > 0 else 0.0

            # TP ÉTAGÉ (config c) : moitié de la position réalisée AVANT l'objectif final, le reste
            # continue de courir (stop verrouillé à +0,3R) -> capture un gain sûr ET laisse courir.
            # Le palier est calculé à 60 % du chemin vers le TP (plafonné à 1,5R) : un palier fixe
            # à 1,5R serait inatteignable dès que l'objectif est plus proche (R/R borné à 1,3).
            target_r = (abs(pos["take_profit"] - entry_p) / R) if (pos.get("take_profit") and R > 0) else 0.0
            partial_r = min(1.5, 0.6 * target_r) if target_r > 0 else 1.5
            if staged_tp and R > 0 and not pos.get("half_closed") and in_profit_r >= partial_r:
                half_price = entry_p + partial_r * R if is_buy else entry_p - partial_r * R
                half = pos["size"] / 2
                _realize(pos, half_price, i, half, note=f" [TP partiel {partial_r:.2f}R]")
                pos["size"] -= half
                pos["half_closed"] = True
                lock = entry_p + 0.3 * R if is_buy else entry_p - 0.3 * R
                pos["stop_loss"] = max(pos["stop_loss"], lock) if is_buy else min(pos["stop_loss"], lock)

            # Breakeven : après +be_r×R, on VERROUILLE un petit profit (+0.3R), pas l'entrée exacte.
            # (Sinon tout repli sort à frais = fausse perte qui plombe le win-rate.)
            if be_r > 0 and R > 0 and in_profit_r >= be_r:
                lock = entry_p + 0.3 * R if is_buy else entry_p - 0.3 * R
                pos["stop_loss"] = max(pos["stop_loss"], lock) if is_buy else min(pos["stop_loss"], lock)

            # Trailing : actif SEULEMENT une fois bien en profit (>= be_r), distance large pour
            # laisser le trade respirer et atteindre le TP (sinon il coupe les gagnants trop tôt).
            if trail_on and atr_p > 0 and in_profit_r >= be_r:
                dist = trail_mult * atr_p
                if is_buy:
                    pos["stop_loss"] = max(pos["stop_loss"], current_candle.close - dist)
                else:
                    pos["stop_loss"] = min(pos["stop_loss"], current_candle.close + dist)

            exit_price = None
            if is_buy:
                if current_candle.low <= pos["stop_loss"]:
                    exit_price = pos["stop_loss"]
                elif pos["take_profit"] and current_candle.high >= pos["take_profit"]:
                    exit_price = pos["take_profit"]
            else:
                if current_candle.high >= pos["stop_loss"]:
                    exit_price = pos["stop_loss"]
                elif pos["take_profit"] and current_candle.low <= pos["take_profit"]:
                    exit_price = pos["take_profit"]

            if exit_price is not None:
                close_position(pos, exit_price, i)
                current_position = None
                continue
        
        # Pas de position -> Chercher un signal d'entrée.
        if not current_position:
            try:
                if strategy is not None:
                    # Stratégie classique : direction d'entrée + SL/TP via ATR.
                    direction = strategy(history)
                    if direction != Direction.HOLD:
                        atr_v = ind.atr(history, 14) or (current_candle.close * 0.01)
                        levels = compute_levels(direction, current_candle.close, atr_v, risk)
                        current_position = {
                            "direction": direction.value,
                            "entry_price": current_candle.close,
                            "stop_loss": levels.stop_loss,
                            "take_profit": levels.take_profit_1,
                            "size": levels.position_size,
                            "entry_time": cts(i),
                            "rationale": f"Stratégie {direction.value}",
                            "atr": atr_v,
                            "init_risk": abs(current_candle.close - levels.stop_loss),
                        }
                else:
                    signal = await signal_engine.generate_signal(
                        asset=config.symbol,
                        candles=history,
                        risk=risk,
                        weights=config.agent_weights
                    )
                    # Filtre de qualité d'entrée : tendance (ADX) + confiance + R/R (cf. quality.py).
                    if quality.is_tradeable(signal):
                        current_position = {
                            "direction": signal.direction.value,
                            "entry_price": current_candle.close,
                            "stop_loss": signal.stop_loss,
                            "take_profit": signal.take_profit_1,
                            "size": signal.position_size or 0,
                            "entry_time": cts(i),
                            "rationale": signal.rationale,
                            "atr": ind.atr(history, 14) or (current_candle.close * 0.01),
                            "init_risk": abs(current_candle.close - signal.stop_loss),
                        }
            except Exception as e:
                logger.error(f"Erreur backtest entrée at {current_candle.timestamp}: {e}")

    # Clôture position finale si ouverte (au dernier prix, coûts inclus).
    if current_position:
        close_position(current_position, candles[-1].close, len(candles) - 1)

    metrics = compute_metrics(trades, equity_points, config.initial_capital)
    # Benchmark « acheter & garder » sur la même fenêtre + surperformance (alpha).
    bh_start = candles[window_size].close if len(candles) > window_size else candles[0].close
    bh_end = candles[-1].close
    benchmark_pct = round((bh_end - bh_start) / bh_start * 100, 2) if bh_start else 0.0
    alpha_pct = round(metrics.total_pnl_pct - benchmark_pct, 2)
    
    return BacktestReport(
        id=str(uuid4()),
        tenant_id=tenant_id,
        config=config,
        metrics=metrics,
        trades=trades,
        equity_curve=equity_points,
        benchmark_pnl_pct=benchmark_pct,
        alpha_pct=alpha_pct,
        cost_pct_per_side=round(cost_rate * 100, 4),
    )
