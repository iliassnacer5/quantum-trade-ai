"""Calcul des KPIs de backtesting."""

from __future__ import annotations

import math
from datetime import datetime

from app.backtest.schemas import BacktestEquityPoint, BacktestMetrics, BacktestTrade


def compute_metrics(
    trades: list[BacktestTrade], 
    equity_curve: list[BacktestEquityPoint],
    initial_capital: float
) -> BacktestMetrics:
    """Calcule les KPIs globaux à partir de la liste des trades et de la courbe d'équité."""
    
    total_trades = len(trades)
    if total_trades == 0:
        # AUCUN TRADE N'EST UNE ABSENCE DE MESURE, PAS UNE MESURE À ZÉRO.
        #
        # Cette branche rendait auparavant `win_rate=0.0` et `profit_factor=0.0`, strictement
        # indistinguables d'une stratégie mesurée à 0 % de réussite — c'est la source des « backtests
        # à 0 % » et des « profit factor à 0 » constatés à l'écran. Un taux de réussite n'existe pas
        # sans trade : il vaut `None` (« indisponible »), et l'interface l'affiche comme tel.
        #
        # Les montants, eux, valent RÉELLEMENT zéro : sans trade, aucun argent n'a bougé et aucun
        # drawdown n'a été subi. Zéro est ici la bonne réponse, pas un bouche-trou.
        return BacktestMetrics(
            total_trades=0,
            win_rate=None,
            profit_factor=None,
            total_pnl=0.0,
            total_pnl_pct=0.0,
            max_drawdown_pct=0.0,
            sharpe_ratio=None,
            average_win=None,
            average_loss=None,
            expectancy=None,
        )

    winning_trades = [t for t in trades if t.pnl > 0]
    losing_trades = [t for t in trades if t.pnl <= 0]

    win_rate = len(winning_trades) / total_trades
    
    gross_profit = sum(t.pnl for t in winning_trades)
    gross_loss = abs(sum(t.pnl for t in losing_trades))
    
    # `None` = INDÉFINI (aucune perte : la division n'a pas de sens), et non « zéro ».
    # L'ancien `float("inf")` se sérialisait en `Infinity` — un littéral que JSON n'admet pas et que
    # `response.json()` refuse côté navigateur : toute la réponse du backtest devenait illisible dès
    # qu'un segment ne perdait aucun trade. `total_trades > 0` distingue ce cas de « pas de mesure ».
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    
    total_pnl = sum(t.pnl for t in trades)
    total_pnl_pct = (total_pnl / initial_capital) * 100

    max_drawdown_pct = max((p.drawdown_pct for p in equity_curve), default=0.0)

    average_win = gross_profit / len(winning_trades) if winning_trades else 0.0
    average_loss = gross_loss / len(losing_trades) if losing_trades else 0.0
    
    expectancy = (win_rate * average_win) - ((1 - win_rate) * average_loss)

    # Sharpe (simplifié, daily returns)
    # Dans un vrai système, on extrairait les rendements journaliers de la courbe d'équité
    # Ici, nous faisons une approximation sur les trades si peu de points
    sharpe_ratio = 0.0
    if len(equity_curve) > 2:
        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i-1].equity
            curr = equity_curve[i].equity
            if prev > 0:
                returns.append((curr - prev) / prev)
        
        if returns:
            avg_ret = sum(returns) / len(returns)
            variance = sum((r - avg_ret)**2 for r in returns) / len(returns)
            std_dev = math.sqrt(variance)
            if std_dev > 0:
                # Annualized (assuming points are hours -> * sqrt(252*24) approx)
                # On reste basique : ratio brut
                sharpe_ratio = avg_ret / std_dev * math.sqrt(len(returns)) # Approx grossière
    
    return BacktestMetrics(
        total_trades=total_trades,
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
        total_pnl=round(total_pnl, 2),
        total_pnl_pct=round(total_pnl_pct, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        sharpe_ratio=round(sharpe_ratio, 2),
        average_win=round(average_win, 2),
        average_loss=round(average_loss, 2),
        expectancy=round(expectancy, 2)
    )
