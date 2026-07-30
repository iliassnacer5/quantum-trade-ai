"""Broker papier (simulation) — exécution par défaut, sans risque réel.

Remplit l'ordre au dernier prix de marché connu (données réelles si dispo, sinon synthétiques).
C'est le mode imposé tant que l'utilisateur n'a pas validé KYC + activé le réel.
"""

from __future__ import annotations

from app.data import markets
from app.execution.base import OrderResult


class PaperBroker:
    mode = "paper"

    def __init__(self, name: str = "paper") -> None:
        self.name = name

    async def place_order(self, symbol: str, side: str, qty: float) -> OrderResult:
        candles = await markets.load_candles(symbol, interval="1h", limit=60)
        if not candles:
            # RÉGRESSION corrigée (29/07/2026) : un repli `0.0` remplissait la position à un prix
            # FANTÔME dès que le chargement échouait (Yahoo/Binance temporairement indisponible).
            # Une entrée à 0 casse tout ce qui en dépend en aval — le calcul de pips notamment
            # (`domain/pips.py::pip_size` retombe sur un pip de 1.0 quand le prix est ≤ 0, ce qui
            # produit des distances à cinq chiffres, ex. « -83 848 pips ») — et la position
            # elle-même ne représente plus un trade réel. Refuser vaut mieux que fabriquer un prix,
            # comme partout ailleurs dans ce module (`replay.py`, `block_synthetic_orders`).
            raise RuntimeError(
                f"Aucun prix de marché réel disponible pour {symbol} — ordre refusé plutôt que "
                "rempli sur un prix inventé."
            )
        price = candles[-1].close
        return OrderResult(
            broker=self.name,
            mode=self.mode,
            symbol=symbol,
            side=side,
            qty=qty,
            status="filled",
            filled_price=round(price, 8),
            raw={"simulated": True},
        )
