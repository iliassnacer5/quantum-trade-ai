"""Rejeu de prix : un trade (entrée + SL + TP) a-t-il gagné, perdu, ou est-il encore ouvert ?

Logique partagée par le paper trading (clôture automatique) et le Journal d'apprentissage
(résolution des signaux). C'est le module qui décide si de l'argent a été gagné ou perdu : il doit
donc être le plus prudent du projet.

RÈGLES DE PRUDENCE — chacune corrige une façon dont un rejeu naïf INVENTE des résultats :

1. **Jamais de conclusion sans données RÉELLES.** Le repli sur des bougies synthétiques clôturait
   des trades à des prix qui n'ont jamais existé : le générateur horodate ses bougies jusqu'à
   « maintenant », elles passaient donc toutes pour postérieures à l'entrée et l'une d'elles
   finissait par franchir l'objectif. C'est l'origine des « gagnants » à des niveaux jamais atteints.
2. **Seules les bougies POSTÉRIEURES à l'entrée comptent.** Une bougie antérieure ne peut pas
   valider un objectif : le trade n'existait pas encore. L'ancien repli « à défaut, prends la
   dernière bougie de la série » violait cette règle en permanence, en particulier marchés fermés.
3. **La bougie qui CONTIENT l'entrée est écartée.** Son extrême a pu être atteint avant l'entrée ;
   l'utiliser reviendrait à s'accorder un gain qu'on n'aurait pas pu prendre.
4. **Le stop l'emporte** quand stop et objectif tombent dans la même bougie : on ignore l'ordre réel
   des ticks à l'intérieur, et l'hypothèse défavorable est la seule honnête.
5. **Aucune clôture antérieure à l'entrée.** Un trade clôturé avant d'être ouvert n'est pas un
   résultat, c'est une corruption — cf. `is_impossible_closure`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.data import ohlcv as _ohlcv

logger = logging.getLogger(__name__)

# Unité de rejeu : plus elle est fine, plus la décision colle au marché réel. Le 15 min est aussi
# l'unité d'entrée de la stratégie du desk.
DEFAULT_INTERVAL = "15m"

# Résultats possibles. `undetermined` est le verdict HONNÊTE quand on ne peut pas savoir : la
# position reste ouverte au lieu d'être clôturée sur une supposition.
WON, LOST, OPEN, UNDETERMINED = "won", "lost", "open", "undetermined"


def iso_to_unix(iso: str | None) -> float | None:
    """Horodatage ISO -> epoch. Retourne None si absent ou illisible (JAMAIS 0).

    Renvoyer 0 en cas d'échec faisait passer TOUT l'historique pour « postérieur à l'entrée » :
    n'importe quelle bougie ancienne pouvait alors valider l'objectif.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


async def replay_outcome(
    symbol: str, direction: str, entry: float,
    stop_loss: float | None, take_profit: float | None,
    since_iso: str | None, interval: str = DEFAULT_INTERVAL,
) -> dict:
    """Rejoue le prix DEPUIS l'entrée et retourne un verdict motivé.

    Retourne ``{outcome, exit_price, closed_ts, reason, data_real, covered_until, bars}`` avec
    ``outcome`` ∈ {won, lost, open, undetermined} :

    - ``open``          : données réelles disponibles, ni le stop ni l'objectif n'ont été touchés ;
    - ``undetermined``  : impossible de trancher (pas de données réelles, ou aucune bougie depuis
      l'entrée — typiquement marchés fermés). **L'appelant ne doit pas clôturer.**
    """
    is_buy = str(direction).lower() in ("buy", "long")
    since = iso_to_unix(since_iso)
    if since is None:
        return _verdict(UNDETERMINED, None, None,
                        "date d'ouverture illisible : impossible de rejouer le prix depuis l'entrée")

    payload = await _ohlcv.get_ohlcv_with_source(symbol, interval=interval, limit=500)
    rows, data_real = payload["candles"], payload["real"]
    if not rows or not data_real:
        return _verdict(
            UNDETERMINED, None, None,
            f"aucune donnée de marché RÉELLE pour {symbol} — le résultat ne peut pas être établi",
        )

    covered_until = rows[-1]["time"]
    # Règles 2 et 3 : uniquement les bougies qui S'OUVRENT après l'entrée.
    after = [r for r in rows if r["time"] >= since]
    if not after:
        return _verdict(
            UNDETERMINED, None, None,
            "aucune bougie depuis l'ouverture du trade (marché fermé ou données en retard) — "
            "position laissée ouverte",
            data_real=True, covered_until=covered_until,
        )

    for r in after:
        hi, lo = r["high"], r["low"]
        hit_sl = stop_loss is not None and (lo <= stop_loss if is_buy else hi >= stop_loss)
        hit_tp = take_profit is not None and (hi >= take_profit if is_buy else lo <= take_profit)
        if hit_sl:      # règle 4 : le stop l'emporte en cas d'ambiguïté intra-bougie
            return _verdict(LOST, stop_loss, r["time"], "stop touché",
                            True, covered_until, len(after))
        if hit_tp:
            return _verdict(WON, take_profit, r["time"], "objectif atteint",
                            True, covered_until, len(after))

    return _verdict(OPEN, after[-1]["close"], None,
                    f"ni stop ni objectif touchés sur {len(after)} bougie(s) depuis l'entrée",
                    True, covered_until, len(after))


def _verdict(outcome: str, exit_price: float | None, closed_ts: float | None, reason: str,
             data_real: bool = False, covered_until: float | None = None, bars: int = 0) -> dict:
    return {"outcome": outcome, "exit_price": exit_price, "closed_ts": closed_ts,
            "reason": reason, "data_real": data_real, "covered_until": covered_until,
            "bars": bars}


def is_impossible_closure(order: dict) -> str | None:
    """Motif d'incohérence d'un ordre DÉJÀ clôturé, ou None s'il est sain.

    Sert à repérer les positions fermées par l'ancien rejeu défectueux : elles portent un résultat
    que le marché n'a jamais produit. Mieux vaut les invalider que les compter dans les statistiques.
    """
    if order.get("outcome") not in (WON, LOST):
        return None
    opened = iso_to_unix(order.get("created_at"))
    closed = iso_to_unix(order.get("closed_at"))
    if opened is not None and closed is not None and closed < opened:
        return (f"clôturée le {order.get('closed_at')} alors qu'elle a été ouverte le "
                f"{order.get('created_at')} — une position ne peut pas se clôturer avant d'exister")
    if order.get("outcome") == WON and order.get("exit_price") is None:
        return "clôturée gagnante sans prix de sortie"
    return None
