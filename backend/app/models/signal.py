"""Schéma de la Signal Card — sortie structurée et stricte du Signal Engine.

Ce schéma est le contrat partagé entre les agents, l'API et le frontend.
"""

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Timeframe(str, Enum):
    """Unités de temps, nommées par leur DURÉE et non par un style de trading.

    « Scalp », « intraday », « swing » décrivent une façon de trader, pas une échelle de temps :
    deux personnes ne mettent pas la même durée derrière ces mots. Le desk ne raisonne qu'en unités
    de temps explicites — ce sont exactement celles de la stratégie (mensuel/journalier → 4 h → 1 h
    → entrée 15 min).
    """

    M15 = "15min"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1week"
    MN = "1month"


class SignalCard(BaseModel):
    """Unité d'information centrale présentée au trader."""

    id: str | None = Field(default=None, description="Id de la prédiction persistée (consultable)")
    asset: str = Field(..., examples=["BTC/USDT"])
    direction: Direction
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    risk_reward: float = Field(..., description="Ratio risque/rendement, ex. 3.2")
    confidence: int = Field(..., ge=0, le=100, description="Score de confiance 0-100%")
    timeframe: Timeframe
    rationale: str = Field(..., description="Justification IA en langage naturel")
    # Dimensionnement (Risk Management) — utile pour l'exposition / P&L
    position_size: float | None = Field(default=None, description="Quantité d'actif")
    position_value: float | None = Field(default=None, description="Valeur de la position en devise")
    risk_amount: float | None = Field(default=None, description="Montant risqué en devise")
    risk_warning: str | None = Field(default=None, description="Avertissement de risque éventuel")
    # Détail par agent (transparence / explicabilité)
    agents: list[dict] = Field(default_factory=list, description="Sorties des agents [{name,score,confidence,rationale}]")
    # News utilisées par l'agent sentiment au moment de la prédiction (consultables par le trader).
    news: list[dict] = Field(default_factory=list, description="Titres analysés [{headline, sentiment}]")
    # Tableau de bord des indicateurs techniques (RSI, MACD, ADX, EMA, Bollinger, ATR, supports…)
    metrics: dict = Field(default_factory=dict, description="Indicateurs techniques consolidés")
    consensus_pct: int = Field(default=0, ge=0, le=100, description="Consensus pondéré des agents (%)")
    # Confirmation multi-timeframe (1h/4h/1j) et marqueur haute-conviction.
    mtf: dict = Field(default_factory=dict, description="Alignement multi-timeframe {aligned,total,details}")
    high_conviction: bool = Field(default=False, description="ADX>25 + consensus>=70% + MTF aligné")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
