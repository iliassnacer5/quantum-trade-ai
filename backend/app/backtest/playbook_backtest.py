"""BACKTEST DE LA STRATÉGIE DU DESK — forex et or, plusieurs paires, plusieurs années.

Ce module rejoue la stratégie complète (`domain.playbook`) sur l'historique réel, en walk-forward
strict : à chaque bougie évaluée, on ne reconstruit le setup qu'avec les données **déjà
disponibles** à cet instant. Aucun regard vers le futur, ni pour le contexte, ni pour l'entrée.

TROIS PASSES COMPLÉMENTAIRES, et c'est volontaire — aucune ne suffit seule :

- **Passe LONGUE (`deep`, 5 ans)** — l'échelle de temps entière est montée d'un cran : contexte
  MENSUEL + HEBDOMADAIRE, confirmations et entrée en JOURNALIER. C'est la seule façon d'obtenir
  cinq ans d'historique RÉEL, parce qu'aucun fournisseur gratuit ne sert d'intraday au-delà de deux
  ans. On y mesure la MÉTHODE (tendance multi-indicateurs → confluence pondérée → sorties posées
  sur des niveaux) à l'échelle du swing long, sur tous les marchés du catalogue.
- **Passe PORTÉE (`1h`)** — le réglage de PRODUCTION (contexte mensuel/journalier/4 h/1 h), avec le
  déclencheur évalué en 1 h sur toute la profondeur disponible (~2,3 ans). C'est elle qui note les
  paires pour l'auto-entrée.
- **Passe FIDÉLITÉ (`15m`)** — le vrai déclencheur de la stratégie, mais seulement sur les ~80 jours
  d'historique 15 min réellement disponibles. Elle sert de contrôle : le passage au 15 min
  améliore-t-il ou dégrade-t-il ce que la passe portée a mesuré ?

**Pourquoi pas 5 ans avec l'entrée 15 min ?** Parce que la donnée n'existe pas gratuitement : Yahoo
refuse explicitement le 15 min au-delà de ~60 jours et plafonne le 1 h comme le 4 h à 730 jours. Un
« backtest 5 ans en 15 min » serait donc forcément simulé, c'est-à-dire faux. La passe longue est
la transposition honnête : mêmes règles, même code, échelle de temps décalée — et elle le dit.

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

# Univers du desk : la stratégie s'applique à TOUS les marchés, donc le backtest les couvre tous.
# Elle ne suppose rien de propre au forex — la tendance se lit pareil sur un indice, et le stop
# comme l'objectif viennent de niveaux du graphique.
#: Le FOREX et les ACTIONS sont les deux marchés que l'utilisateur veut voir classés en premier :
#: chacun compte donc assez d'instruments pour qu'un « top 10 » veuille dire quelque chose. Un
#: classement des 10 meilleurs sur 8 candidats ne classe rien, il recopie la liste.
FOREX_PAIRS = [
    # Majeures
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    # Croisées les plus liquides
    "EUR/GBP", "EUR/JPY", "GBP/JPY", "EUR/CHF", "AUD/JPY", "EUR/CAD", "GBP/CHF",
]
METALS = ["XAU/USD", "XAG/USD"]
INDICES = ["SPX500", "NAS100", "US30", "GER40", "UK100", "FRA40", "JPN225", "HK50"]
STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL", "JPM",
    "NFLX", "AMD", "V", "MA", "WMT", "XOM", "CVX", "ORCL", "CRM", "ADBE", "COIN", "PLTR",
]
CRYPTO = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
#: Univers « noyau » — l'ancien défaut, conservé pour rejouer une mesure à l'identique.
CORE_UNIVERSE = METALS + FOREX_PAIRS + INDICES + STOCKS + CRYPTO
#: Univers par MARCHÉ, tel que l'interface le propose. `full_universe()` reste le défaut du
#: backtest complet ; ces listes servent à lancer une passe sur UN marché.
MARKET_UNIVERSES: dict[str, list[str]] = {
    "forex": FOREX_PAIRS,
    "stock": STOCKS,
    "commodity": METALS,
    "index": INDICES,
    "crypto": CRYPTO,
}
MARKET_LABELS = {"forex": "Forex", "commodity": "Métaux précieux", "index": "Indices",
                 "stock": "Actions", "crypto": "Crypto"}


def tradable(symbols: list[str]) -> list[str]:
    """Retire les marchés et instruments que le desk REFUSE de trader.

    Le backtest ne mesure que ce qui peut réellement être tradé, et c'est une décision (03/08/2026),
    pas une limite technique. Mesurer un marché exclu coûtait trois choses :

    1. **du temps** — 31 des 88 symboles du catalogue (la crypto entière plus XAU/USD), soit 35 % du
       calcul, sur une passe 1 h qui dure déjà plus de 40 minutes ;
    2. **du débit fournisseur** — c'est ce balayage qui continuait de marteler l'API Binance ;
    3. **la justesse du verdict**, et c'est le plus grave : l'espérance globale mélangeait des
       marchés tradés et non tradés. Un « +0,12 R, marginal » qui inclut la crypto à -0,32 R ne
       décrit pas le desk, il décrit un desk imaginaire qui traderait tout.

    Point de vérité unique : c'est `playbook_service.is_excluded` qui tranche ici comme au balayage
    des trades et à l'ouverture de position. Rouvrir un marché dans la configuration le fait donc
    revenir dans le backtest tout seul — il n'y a rien à resynchroniser à la main.
    """
    from app.services import playbook_service

    return [s for s in symbols if not playbook_service.is_excluded(s)]


def full_universe() -> list[str]:
    """Le catalogue TRADABLE — c'est l'univers par défaut du backtest.

    La fréquence d'opportunité mesurée est d'environ 0,085 trade par jour et par symbole (entrée
    évaluée en 1 h), avec un écart considérable d'un marché à l'autre. Le nombre de trades dépend
    donc AVANT TOUT du nombre de symboles balayés, et c'est le seul levier de volume qui ne coûte
    rien en qualité — élargir n'assouplit aucune règle, chaque trade reste soumis aux trois étapes.
    Élargir veut dire ajouter des symboles TRADABLES, jamais rouvrir un marché exclu.
    """
    from app.data import symbols as symbols_catalog

    return tradable([i["symbol"] for i in symbols_catalog.all_symbols()])


DEFAULT_UNIVERSE = CORE_UNIVERSE   # remplacé par `full_universe()` à l'appel (import paresseux)

# Profondeur de contexte passée à chaque unité de temps (bornée : le coût est linéaire).
_TAIL = {"monthly": 60, "daily": 200, "h4": 200, "h1": 200, "m15": 200}
_INTERVAL_SECONDS = {"15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400,
                     "1w": 604_800, "1M": 2_592_000}

# --- ÉCHELLES DE TEMPS -------------------------------------------------------------------------
# La stratégie a cinq couches : contexte long, contexte moyen, deux confirmations, entrée. Le code
# les nomme d'après le réglage de production (monthly / daily / h4 / h1 / entrée), mais RIEN dans
# `playbook.build` ne dépend de la durée réelle de ces bougies : il lit une tendance et des niveaux.
# On peut donc décaler toute l'échelle — c'est ce qui permet d'aller à cinq ans.
_LADDERS: dict[str, dict[str, str]] = {
    # PRODUCTION : mensuel / journalier / 4 h / 1 h, entrée 15 min (ou 1 h pour la passe portée).
    "intraday": {"monthly": "1M", "daily": "1d", "h4": "4h", "h1": "1h"},
    # LONGUE PORTÉE (5 ans) : mensuel / hebdomadaire / journalier, entrée journalière.
    # Réserve à ne pas cacher : faute d'une quatrième unité de temps disponible sur cinq ans, les
    # deux couches de confirmation sont portées par les MÊMES bougies journalières. Le filtre est
    # donc un cran moins sévère que la production — ces chiffres mesurent la MÉTHODE sur cinq ans,
    # pas le réglage exact de production. C'est écrit dans le résultat, pas seulement ici.
    "swing": {"monthly": "1M", "daily": "1w", "h4": "1d", "h1": "1d"},
}

# --- ÉCHELLE DU TRADE, par échelle de temps -----------------------------------------------------
# `playbook.build` exprime la taille du trade en multiples de l'ATR de sa couche de CONTEXTE MOYEN.
# Décaler l'échelle décale donc aussi cette référence : sur l'échelle swing, cette couche porte des
# bougies HEBDOMADAIRES, dont l'amplitude vaut environ 2,2 fois celle d'une journée.
#
# Le garde-fou `max_stop_pips` (150) doit être DÉSACTIVÉ dans ce cas, et ce n'est pas un
# assouplissement : c'est un plafond calibré pour une entrée 15 min. Mesuré sur XAU/USD, il rendait
# la fourchette de stop VIDE — le minimum exigé (0,55 × ATR hebdomadaire ≈ 33 $) dépassait le
# maximum autorisé (150 pips = 15 $). Aucun stop ne pouvait exister, d'où zéro trade. Le plafond en
# ATR, lui, reste actif : c'est lui qui contient réellement le drawdown, et il vaut sur tous les
# marchés là où un nombre de pips ne veut rien dire.
_LADDER_SCALE: dict[str, dict] = {
    "intraday": {},
    "swing": {"max_stop_pips": 1e9},
}

# Durée maximale de détention, par unité de temps d'entrée. La stratégie vise ~2 ATR de son unité de
# contexte : au-delà, le scénario qui a motivé l'entrée n'est plus d'actualité.
_MAX_HOLD = {"1h": 24 * 15, "15m": 4 * 24 * 15, "1d": 60}

# Bougies par AN pour convertir une profondeur d'historique en durée calendaire. Les séries chargées
# sont ré-horodatées de façon contiguë (les connecteurs ne rendent pas tous l'horodatage d'origine) :
# compter les bougies est donc plus honnête que soustraire deux dates. 252 séances par an pour les
# marchés à horaires (actions, indices, forex, métaux), 52 semaines pour l'hebdomadaire.
_BARS_PER_YEAR = {"1d": 252.0, "1w": 52.0}


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
    target2: float | None = None, tp1_lock_fraction: float = 0.8,
) -> dict:
    """Rejoue un trade bougie par bougie, AVEC les deux règles de sécurisation du profit.

    Règles appliquées, dans l'ordre où le marché les rencontrerait :
    1. si le stop courant est touché -> sortie au stop ;
    2. si +`secure_at_r` R est atteint -> le stop est remonté sur ce niveau et n'en redescend plus ;
    3. si TP1 est touché :
       - sans second objectif : sortie à TP1 ;
       - avec `target2` : le stop monte à `tp1_lock_fraction` du chemin parcouru (80 % par défaut)
         et la position continue vers TP2 ;
    4. si TP2 est touché -> sortie à TP2 ;
    5. au-delà de `max_hold` bougies -> sortie au marché (le scénario a expiré).

    Les deux règles de sécurisation coexistent : le stop retient toujours le niveau le PLUS
    FAVORABLE des deux et ne recule jamais.

    En backtest on ne vérifie PAS le momentum avant de poursuivre vers TP2 (la production, elle, le
    fait) : on mesure ici la mécanique pure, sans la mélanger à un filtre supplémentaire.

    Quand stop et objectif tombent dans la MÊME bougie, on suppose le stop touché en premier : on ne
    sait pas dans quel ordre le prix a circulé à l'intérieur de la bougie, et l'hypothèse prudente
    est la seule honnête. C'est aussi ce qui empêche un backtest de surestimer sa réussite.
    """
    buy = direction == "BUY"
    sign = 1 if buy else -1
    initial_risk = abs(entry - stop)
    if initial_risk <= 0:
        return {"outcome": "invalid", "r": 0.0, "bars": 0, "secured": False}

    current_stop = stop
    secured = False
    tp2_managed = False
    secure_level = entry + sign * secure_at_r * initial_risk
    lock_level = entry + sign * tp1_lock_fraction * abs(target - entry)
    final_target = target

    def _raise(level: float) -> None:
        """Le stop ne recule jamais : on ne retient que ce qui améliore la position."""
        nonlocal current_stop
        current_stop = max(current_stop, level) if buy else min(current_stop, level)

    for i, candle in enumerate(bars[start + 1: start + 1 + max_hold], start=1):
        hit_stop = candle.low <= current_stop if buy else candle.high >= current_stop
        if hit_stop:
            r = (current_stop - entry) / initial_risk if buy else (entry - current_stop) / initial_risk
            reason = "stop initial"
            if tp2_managed:
                reason = "stop verrouillé sur TP1"
            elif secured:
                reason = "stop sécurisé"
            return {"outcome": "won" if r > 0 else "lost", "r": round(r, 3), "bars": i,
                    "secured": secured, "tp2_managed": tp2_managed, "exit": current_stop,
                    "exit_reason": reason}
        # Sécurisation : le stop monte sur +2R et n'en redescend jamais.
        reached = (candle.high >= secure_level) if buy else (candle.low <= secure_level)
        if not secured and reached:
            _raise(secure_level)
            secured = True
        # TP1 touché : on sort, ou on verrouille et on part chercher TP2.
        if not tp2_managed:
            hit_tp1 = candle.high >= target if buy else candle.low <= target
            if hit_tp1:
                if target2 is None:
                    r = (target - entry) / initial_risk if buy else (entry - target) / initial_risk
                    return {"outcome": "won", "r": round(r, 3), "bars": i, "secured": secured,
                            "tp2_managed": False, "exit": target, "exit_reason": "objectif"}
                _raise(lock_level)
                tp2_managed = True
                final_target = target2
                continue        # la bougie qui atteint TP1 ne peut pas aussi conclure TP2
        else:
            hit_tp2 = candle.high >= final_target if buy else candle.low <= final_target
            if hit_tp2:
                r = ((final_target - entry) if buy else (entry - final_target)) / initial_risk
                return {"outcome": "won", "r": round(r, 3), "bars": i, "secured": secured,
                        "tp2_managed": True, "exit": final_target,
                        "exit_reason": "second objectif"}

    window = bars[start + 1: start + 1 + max_hold]
    if not window:
        return {"outcome": "invalid", "r": 0.0, "bars": 0, "secured": secured}
    close = window[-1].close
    r = (close - entry) / initial_risk if buy else (entry - close) / initial_risk
    return {"outcome": "expired", "r": round(r, 3), "bars": len(window), "secured": secured,
            "tp2_managed": tp2_managed, "exit": close, "exit_reason": "expiration du scénario"}


# =======================================================================================
# Backtest d'une paire
# =======================================================================================
async def backtest_symbol(
    symbol: str, *, entry_tf: str = "1h", step: int = 2, overrides: dict | None = None,
    ladder: str = "intraday", max_years: float | None = None,
) -> dict:
    """Rejoue la stratégie sur UNE paire et retourne ses trades reconstitués.

    `entry_tf` : unité de temps sur laquelle le déclencheur d'entrée est évalué ("1h" pour la passe
    portée, "15m" pour la passe fidélité, "1d" pour la passe longue).

    `ladder` : quelle ÉCHELLE de temps porte les cinq couches (cf. `_LADDERS`). "intraday" est le
    réglage de production ; "swing" monte tout d'un cran pour atteindre cinq ans d'historique réel.

    `max_years` : borne la fenêtre évaluée aux N dernières années (la passe longue vise 5 ans, même
    quand le fournisseur en sert 10 — au-delà, le régime de marché n'a plus grand-chose à voir).

    `overrides` : paramètres de `playbook.build` à surcharger pour CE run (A/B tests : mode de
    volatilité, seuil de confluence…). Le run de référence n'en passe aucun.
    """
    s = get_settings()
    tfs = _LADDERS.get(ladder, _LADDERS["intraday"])
    # Profondeur demandée : le maximum réellement servi par les fournisseurs (1 h et 4 h ≈ 730 j,
    # 15 min ≈ 60 j, journalier et hebdomadaire ≈ 10 ans). Demander plus ne renvoie pas plus.
    # Au-delà de 1 200 bougies demandées, le connecteur Yahoo bascule sur sa plage MAXIMALE
    # (`_RANGE_DEEP`). C'est indispensable pour l'hebdomadaire : en deçà il ne sert que 5 ans, or les
    # 60 premières bougies servent à chauffer les indicateurs — on n'évaluerait alors que 3,8 ans
    # sur les 5 demandées. Demander 10 ans place cette période de chauffe hors de la fenêtre mesurée.
    limits = {"1h": 20_000, "15m": 6_000, "4h": 6_000, "1d": 3_000, "1w": 1_500, "1M": 200}

    async def _load(interval: str) -> list[Candle]:
        return await _series(symbol, interval, limits.get(interval, 5_000))

    try:
        entry_bars = await _load(entry_tf)
        # Une couche dont l'unité de temps EST celle de l'entrée réutilise la série déjà chargée :
        # inutile de la redemander au fournisseur (et de risquer une limitation de débit).
        loaded: dict[str, list[Candle]] = {entry_tf: entry_bars}
        for slot in ("h1", "h4", "daily", "monthly"):
            iv = tfs[slot]
            if iv not in loaded:
                loaded[iv] = await _load(iv)
        h1, h4 = loaded[tfs["h1"]], loaded[tfs["h4"]]
        daily, monthly = loaded[tfs["daily"]], loaded[tfs["monthly"]]
    except NoRealDataError as exc:
        return {"symbol": symbol, "trades": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — une paire indisponible n'arrête pas le backtest
        logger.warning("Backtest %s : historique indisponible (%s)", symbol, exc)
        return {"symbol": symbol, "trades": [], "error": str(exc)}

    # Le minimum de bougies d'entrée dépend de l'échelle : exiger 300 bougies JOURNALIÈRES est
    # normal (~15 mois), en exiger 300 hebdomadaires écarterait tout le monde.
    min_entry = 300 if entry_tf in ("15m", "1h", "4h") else 260
    if len(entry_bars) < min_entry or len(daily) < 120 or len(monthly) < 14:
        return {"symbol": symbol, "trades": [],
                "error": f"historique insuffisant ({len(entry_bars)} bougies {entry_tf}, "
                         f"{len(daily)} en {tfs['daily']}, {len(monthly)} en {tfs['monthly']})"}

    def _stamps(c: list[Candle]) -> list[float]:
        return [x.timestamp.timestamp() if x.timestamp else 0.0 for x in c]

    ctx = {"h1": (h1, _stamps(h1)), "h4": (h4, _stamps(h4)),
           "daily": (daily, _stamps(daily)), "monthly": (monthly, _stamps(monthly))}
    max_hold = _MAX_HOLD.get(entry_tf, 300)
    pip = pips_mod.pip_size(symbol, entry_bars[-1].close)
    trades: list[dict] = []
    open_until = -1                    # une seule position à la fois par paire (réalisme)

    # Fenêtre évaluée : on démarre après 250 bougies (les indicateurs longs ont besoin de chauffer)
    # et, si `max_years` est donné, on ne remonte pas plus loin que la profondeur demandée.
    first = 250
    if max_years:
        per_year = _BARS_PER_YEAR.get(entry_tf) or (
            365.25 * 86_400 / _INTERVAL_SECONDS.get(entry_tf, 3_600))
        first = max(first, len(entry_bars) - int(max_years * per_year))

    for i in range(first, len(entry_bars) - max_hold - 1, max(1, step)):
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
            can_trade=(not s.playbook_trade_only_when_open) or tradable,
            # Mêmes filtres que la production : le backtest mesure la stratégie TELLE QU'ELLE TRADE.
            # Point de vérité unique, partagé avec `playbook_service` et `training_service`.
            **playbook.settings_kwargs(s),
        )
        # L'échelle de temps impose d'abord ses propres bornes (cf. `_LADDER_SCALE`), puis les
        # surcharges de l'appelant REMPLACENT le tout — un même mot-clé des deux côtés est normal
        # pour un A/B (`dict(a=1, **{"a": 2})` lèverait un TypeError).
        build_kwargs.update(_LADDER_SCALE.get(ladder, {}))
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
        tp2 = setup.take_profit_2 if s.playbook_tp2_management else None
        res = replay_trade(
            entry_bars, i, setup.direction, setup.entry, setup.stop_loss, setup.take_profit_1,
            max_hold=max_hold, secure_at_r=s.playbook_secure_at_r,
            target2=tp2, tp1_lock_fraction=s.playbook_tp1_lock_fraction,
        )
        if res["outcome"] == "invalid":
            continue
        # Le MÊME trade rejoué SANS la sécurisation +2R : c'est ce qui permet le comparatif
        # « sécurisation on/off » sur des trades strictement identiques (aucun autre facteur ne bouge).
        res_plain = replay_trade(
            entry_bars, i, setup.direction, setup.entry, setup.stop_loss, setup.take_profit_1,
            max_hold=max_hold, secure_at_r=float("inf"),
            target2=tp2, tp1_lock_fraction=s.playbook_tp1_lock_fraction,
        )
        # ...et rejoué en sortant à TP1, pour mesurer ce que la poursuite vers TP2 apporte
        # réellement — sur les mêmes trades, là encore.
        res_tp1_only = replay_trade(
            entry_bars, i, setup.direction, setup.entry, setup.stop_loss, setup.take_profit_1,
            max_hold=max_hold, secure_at_r=s.playbook_secure_at_r,
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
            "r_tp1_only": res_tp1_only["r"],
            "tp2": setup.take_profit_2,
            "tp2_managed": res.get("tp2_managed", False),
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

    # --- Couverture et FRÉQUENCE D'OPPORTUNITÉ ---------------------------------------------------
    # Le nombre de trades ne veut rien dire sans la durée sur laquelle il a été produit : 21 trades
    # en 564 jours et 53 en 352 jours, ce n'est pas le même métier. On rapporte donc chaque symbole
    # à un TAUX (trades par jour), seule grandeur comparable d'un marché à l'autre — et seule base
    # honnête pour estimer combien de trades un univers donné produira par semaine.
    evaluated = max(0, len(entry_bars) - max_hold - 1 - first)
    per_year = _BARS_PER_YEAR.get(entry_tf) or (
        365.25 * 86_400 / _INTERVAL_SECONDS.get(entry_tf, 3_600))
    years = round(evaluated / per_year, 2) if per_year else 0.0
    days = round(years * 365.25, 1)
    covered = ""
    if entry_bars:
        covered = (f"{entry_bars[0].timestamp:%Y-%m-%d} → {entry_bars[-1].timestamp:%Y-%m-%d}")
    return {
        "symbol": symbol, "trades": trades, "entry_timeframe": entry_tf, "ladder": ladder,
        "bars": len(entry_bars), "bars_evaluated": evaluated, "coverage": covered, "pip": pip,
        "years": years,
        "days_evaluated": days,
        # Fréquence MESURÉE : trades par jour de calendrier pour CE symbole. C'est elle qui dit si
        # un symbole mérite sa place dans l'univers balayé (l'or produit ~7× plus qu'EUR/USD).
        "trades_per_day": round(len(trades) / days, 4) if days else 0.0,
        "days_between_trades": round(days / len(trades), 1) if trades else None,
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


def opportunity_rate(frequency: dict[str, dict]) -> dict:
    """FRÉQUENCE D'OPPORTUNITÉ de la stratégie — combien de trades par jour, par semaine, par mois.

    Piège de calcul à éviter absolument : diviser le TOTAL des trades par la période du symbole le
    plus ancien. Les symboles n'ont pas tous la même profondeur d'historique (630 jours sur EUR/USD,
    147 sur SPX500) ; rapporter tous les trades à la plus longue divise par un dénominateur trop
    grand et sous-estime la fréquence — d'un facteur deux dans le cas mesuré.

    Le calcul juste est la MOYENNE DES TAUX : chaque symbole donne son propre nombre de trades par
    jour, et on fait la moyenne de ces taux. Le total attendu pour un univers de N symboles est
    alors `N × taux_moyen`, ce qui permet enfin de répondre à « combien de trades si je passe de 16
    à 40 symboles ».
    """
    rated = [f for f in frequency.values() if f["days_evaluated"] > 0]
    if not rated:
        return {"symbols": 0, "note": "aucun symbole exploitable — fréquence non mesurable"}
    rates = sorted(f["trades_per_day"] for f in rated)
    avg = sum(rates) / len(rates)
    median = rates[len(rates) // 2]
    universe_day = avg * len(rated)
    return {
        "symbols": len(rated),
        "avg_trades_per_day_per_symbol": round(avg, 4),
        "median_trades_per_day_per_symbol": round(median, 4),
        "days_between_trades_per_symbol": round(1 / avg, 1) if avg else None,
        "universe_trades_per_day": round(universe_day, 2),
        "universe_trades_per_week": round(universe_day * 7, 1),
        "universe_trades_per_month": round(universe_day * 30.4, 1),
        # Projection : ce que produirait un univers de N symboles au taux moyen mesuré. C'est le
        # levier de volume qui ne coûte RIEN en qualité — aucune règle n'est assouplie.
        "projection": {str(n): round(avg * n, 2) for n in (16, 24, 40, 60, 88)},
        "note": (
            f"{len(rated)} symboles mesurés · {avg:.3f} trade par jour et par symbole en moyenne "
            f"(médiane {median:.3f}) → {universe_day:.1f} par jour et {universe_day * 7:.0f} par "
            "semaine sur cet univers. Moyenne des TAUX par symbole, pas total ÷ période la plus "
            "longue : les historiques sont de longueurs différentes."
        ),
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


def rank_pairs(by_symbol: dict[str, dict], min_trades: int,
               frequency: dict[str, dict] | None = None) -> list[dict]:
    """Classement des paires par FIABILITÉ mesurée, du meilleur au moins bon.

    Le critère principal est l'espérance en R : elle combine le taux de réussite et le rapport
    gain/perte, là où le seul taux de réussite peut être flatteur (beaucoup de petits gains, quelques
    grosses pertes). Les paires sous le seuil d'échantillon sont classées à part : on ne les note
    pas, on dit qu'on ne sait pas.

    Chaque ligne porte aussi sa FRÉQUENCE (trades par jour, jours entre deux trades) et un gain
    espéré par mois : `espérance × trades par jour × 30,4`. Deux paires à +0,8 R d'espérance n'ont
    pas la même valeur si l'une donne un trade par semaine et l'autre un par mois et demi — c'est
    ce produit qui dit lesquelles font vivre l'univers balayé.
    """
    freq = frequency or {}
    rated, unrated = [], []
    for symbol, m in by_symbol.items():
        f = freq.get(symbol) or {}
        tpd = f.get("trades_per_day") or 0.0
        row = {
            "symbol": symbol, **m,
            "trades_per_day": round(tpd, 4),
            "days_between_trades": f.get("days_between_trades"),
            "days_evaluated": f.get("days_evaluated"),
            "r_per_month": round(m["expectancy_r"] * tpd * 30.4, 2),
        }
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


def market_tops(ranking: list[dict], top_n: int = 10) -> dict[str, dict]:
    """LES N MEILLEURS INSTRUMENTS DE CHAQUE MARCHÉ — la question « où cette stratégie marche-t-elle ».

    Un classement global mélange EUR/USD et NVDA, dont ni la volatilité ni les horaires ne se
    comparent : le premier marché finit par occuper tout le haut du tableau et l'autre devient
    invisible. On classe donc DANS chaque marché, sur le même critère qu'ailleurs — l'espérance en
    R, seule mesure qui combine taux de réussite et rapport gain/perte.

    Les instruments sous le seuil d'échantillon (`rank is None`) sont écartés du top : on ne met pas
    en tête une paire dont on ne sait rien. Ils restent comptés dans `unrated` pour que l'écart
    entre « mesuré » et « testé » soit visible.
    """
    from app.data import markets as markets_mod

    buckets: dict[str, list[dict]] = {}
    unrated: dict[str, int] = {}
    for row in ranking:
        cls = markets_mod.asset_class(row["symbol"])
        if row.get("rank"):
            buckets.setdefault(cls, []).append(row)
        else:
            unrated[cls] = unrated.get(cls, 0) + 1
            buckets.setdefault(cls, [])

    out: dict[str, dict] = {}
    for cls, rows in buckets.items():
        # `ranking` arrive déjà trié par espérance ; on re-trie pour ne dépendre de rien.
        best = sorted(rows, key=lambda r: -r["expectancy_r"])[:top_n]
        positive = [r for r in best if r["expectancy_r"] > 0]
        out[cls] = {
            "market": cls,
            "label": MARKET_LABELS.get(cls, cls),
            "rated": len(rows),
            "unrated": unrated.get(cls, 0),
            "profitable": len(positive),
            "top": [
                {**row, "market_rank": i}
                for i, row in enumerate(best, 1)
            ],
            "note": (
                f"{len(rows)} instrument(s) suffisamment mesuré(s) sur ce marché, "
                f"{unrated.get(cls, 0)} sans échantillon exploitable. "
                + (f"{len(positive)} du top {len(best)} ont une espérance positive."
                   if best else "Aucun instrument classable : pas assez de trades.")
            ),
        }
    # Marchés triés par la qualité de leur MEILLEUR instrument : c'est la lecture utile.
    return dict(sorted(
        out.items(),
        key=lambda kv: -(kv[1]["top"][0]["expectancy_r"] if kv[1]["top"] else -99),
    ))


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


def tp_management_ab(trades: list[dict]) -> dict:
    """Comparatif « poursuivre vers TP2 » contre « sortir à TP1 », sur les MÊMES trades.

    Laisser courir vers un second objectif rapporte davantage quand le mouvement continue, et rend
    une partie du gain quand il s'arrête — le stop verrouillé à 80 % de TP1 borne cette restitution.
    Seule la mesure peut dire si l'échange est favorable, d'où ce comparatif à trades identiques.
    """
    rows = [t for t in trades if t.get("r_tp1_only") is not None]
    if not rows:
        return {"note": "aucun trade comparable", "trades": 0}
    n = len(rows)
    with_tp2 = sum(t["r"] for t in rows) / n
    tp1_only = sum(t["r_tp1_only"] for t in rows) / n
    managed = sum(1 for t in rows if t.get("tp2_managed"))
    changed = sum(1 for t in rows if abs(t["r"] - t["r_tp1_only"]) > 1e-9)
    return {
        "trades": n,
        "went_for_tp2": managed,
        "changed_by_rule": changed,
        "expectancy_with_tp2_r": round(with_tp2, 3),
        "expectancy_tp1_only_r": round(tp1_only, 3),
        "delta_r": round(with_tp2 - tp1_only, 3),
        "note": (
            f"{managed}/{n} trades ont touché TP1 et poursuivi vers TP2 ; l'issue a changé pour "
            f"{changed} d'entre eux. Espérance {with_tp2:+.3f} R en poursuivant contre "
            f"{tp1_only:+.3f} R en sortant à TP1 ({with_tp2 - tp1_only:+.3f} R par trade)."
        ),
    }


async def run_pass(
    universe: list[str], *, entry_tf: str, step: int, parallel: int = 4,
    overrides: dict | None = None, ladder: str = "intraday", max_years: float | None = None,
) -> dict:
    """Une passe de backtest sur tout l'univers, pour une unité de temps d'entrée donnée."""
    started = datetime.now(UTC)
    sem = asyncio.Semaphore(max(1, parallel))

    async def _one(symbol: str) -> dict:
        async with sem:
            try:
                return await backtest_symbol(symbol, entry_tf=entry_tf, step=step,
                                             overrides=overrides, ladder=ladder,
                                             max_years=max_years)
            finally:
                # AVANCEMENT RÉEL, symbole par symbole.
                #
                # `_set_phase` n'était appelé QU'UNE FOIS par passe, avec `done=0`, et jamais
                # réactualisé : la page affichait « 0/88 » du début à la fin. Sur une passe qui dure
                # une dizaine de minutes, un compteur figé à zéro est indiscernable d'un plantage —
                # au point que je l'ai moi-même diagnostiqué à tort comme un blocage.
                # Le compteur est incrémenté dans un `finally` : un symbole en échec fait avancer la
                # progression comme un autre, sinon la barre se bloquerait au premier fournisseur KO.
                _state["done"] = _state.get("done", 0) + 1

    _state["done"] = 0
    results = await asyncio.gather(*(_one(sym) for sym in universe), return_exceptions=True)

    trades: list[dict] = []
    failures: list[dict] = []
    coverage: dict[str, str] = {}
    years: list[float] = []
    frequency: dict[str, dict] = {}
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
        frequency[symbol] = {
            "trades": len(res["trades"]),
            "days_evaluated": res.get("days_evaluated", 0.0),
            "trades_per_day": res.get("trades_per_day", 0.0),
            "days_between_trades": res.get("days_between_trades"),
        }

    s = get_settings()
    min_trades = s.playbook_training_min_trades
    by_symbol = _group(trades, "symbol")
    ranking = rank_pairs(by_symbol, min_trades, frequency=frequency)
    return {
        "entry_timeframe": entry_tf,
        "ladder": ladder,
        "generated_at": started.isoformat(),
        "duration_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "pairs_tested": len(universe) - len(failures),
        "failures": failures,
        "coverage": coverage,
        "years_covered": round(sum(years) / len(years), 2) if years else 0.0,
        "overall": metrics(trades),
        # Combien de trades cet univers produit par jour et par semaine — la question posée quand on
        # demande « comment en avoir davantage ».
        "frequency": opportunity_rate(frequency),
        "by_symbol": by_symbol,
        "ranking": ranking,
        # LES 10 MEILLEURS PAR MARCHÉ — un classement global écraserait les marchés les moins
        # volatils sous les plus volatils ; on classe donc à l'intérieur de chaque marché.
        "market_tops": market_tops(ranking, top_n=10),
        "by_trigger": _group(trades, "trigger"),
        "by_session": _group(trades, "session"),
        "by_direction": _group(trades, "direction"),
        # Matrice PAIRE × DÉCLENCHEUR (plan, tâche 2.3) : c'est elle qui permet de désactiver un
        # déclencheur là où il est mesuré perdant, sans le condamner partout.
        "by_pair_trigger": _group(trades, "pair_trigger"),
        # Comparatif sécurisation +2R on/off (plan, tâche 2.6) — mêmes trades, deux rejeux.
        "secure_ab": secure_ab(trades),
        # Comparatif « poursuivre vers TP2 » contre « sortir à TP1 » — mêmes trades, deux rejeux.
        "tp_management_ab": tp_management_ab(trades),
        "losers_profile": losers_profile(trades),
        "min_trades": min_trades,
        "trades": len(trades),
        # Journal COMPLET des trades rejoués : indispensable aux méta-analyses (simulation de
        # sizing, ventilations futures) sans avoir à relancer dix minutes de calcul.
        "trade_log": trades,
    }


async def run_symbol(
    symbol: str, *, entry_tf: str = "1h", step: int = 4, ladder: str = "intraday",
    max_years: float | None = None,
) -> dict:
    """Backtest de la stratégie sur UN SEUL instrument, avec tout ce qu'il y a à en dire.

    C'est la version « une paire » de `run_pass` : mêmes règles, mêmes réglages de production, même
    walk-forward strict — mais sur un seul symbole, donc en quelques secondes au lieu d'une dizaine
    de minutes. Elle sert le bouton « backtester ce symbole » de l'interface.

    Retourne les métriques globales, la ventilation par déclencheur / session / sens, le profil des
    perdants, la fréquence d'opportunité mesurée, et le JOURNAL COMPLET des trades rejoués — un
    backtest qui ne montre pas ses trades ne se vérifie pas.
    """
    from app.data import markets as markets_mod

    started = datetime.now(UTC)
    res = await backtest_symbol(symbol, entry_tf=entry_tf, step=max(1, step),
                                ladder=ladder, max_years=max_years)
    trades = res.get("trades") or []
    out = {
        "symbol": symbol,
        "market": markets_mod.asset_class(symbol),
        "market_label": MARKET_LABELS.get(markets_mod.asset_class(symbol), ""),
        "entry_timeframe": entry_tf,
        "ladder": ladder,
        "step": step,
        "generated_at": started.isoformat(),
        "duration_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "error": res.get("error"),
        "bars": res.get("bars", 0),
        "bars_evaluated": res.get("bars_evaluated", 0),
        "coverage": res.get("coverage", ""),
        "years_covered": res.get("years", 0.0),
        "days_evaluated": res.get("days_evaluated", 0.0),
        "trades_per_day": res.get("trades_per_day", 0.0),
        "days_between_trades": res.get("days_between_trades"),
        "overall": metrics(trades),
        "by_trigger": _group(trades, "trigger"),
        "by_session": _group(trades, "session"),
        "by_direction": _group(trades, "direction"),
        "by_outcome": _group(trades, "outcome"),
        "losers_profile": losers_profile(trades),
        "secure_ab": secure_ab(trades),
        "tp_management_ab": tp_management_ab(trades),
        "trades": trades,
        "min_trades": get_settings().playbook_training_min_trades,
    }
    o = out["overall"]
    if res.get("error"):
        out["verdict"] = f"non mesurable — {res['error']}"
    elif not o["trades"]:
        out["verdict"] = (
            "aucun trade conforme sur la période disponible : les conditions bloquantes "
            "(tendance validée, déclencheur 15 min, R/R dans la bande) n'ont jamais été réunies "
            "en même temps. C'est un résultat, pas une panne."
        )
    else:
        out["verdict"] = _verdict({**o, "symbol": symbol})
    return out


# Résultats des backtests « un seul symbole », persistés par symbole + unité d'entrée. Ils ne
# remplacent pas le backtest complet : ils répondent à « et sur CELUI-CI, ça donne quoi ? ».
SYMBOL_COLLECTION = "playbook_symbol_backtest"

#: Passages en cours, par clé (symbole + unité d'entrée). Un backtest de symbole dure de quelques
#: secondes à une minute selon la profondeur : trop long pour le fil d'une requête HTTP, assez court
#: pour qu'on n'ait pas besoin d'une file d'attente. On le lance donc en tâche de fond et
#: l'interface suit l'avancement, exactement comme pour le backtest complet.
_symbol_runs: dict[str, dict] = {}


def symbol_key(symbol: str, entry_tf: str) -> str:
    return f"{symbol.upper()}|{entry_tf}"


def symbol_run_state(symbol: str, entry_tf: str) -> dict:
    """État du backtest de ce symbole : en cours ou non, et depuis quand."""
    state = _symbol_runs.get(symbol_key(symbol, entry_tf))
    if not state:
        return {"running": False, "started_at": None}
    out = dict(state)
    if out["running"] and out["started_at"]:
        out["elapsed_s"] = round(
            (datetime.now(UTC) - datetime.fromisoformat(out["started_at"])).total_seconds(), 1)
    return out


async def run_symbol_job(
    store, symbol: str, *, entry_tf: str = "1h", step: int = 4,  # noqa: ANN001
    ladder: str = "intraday", max_years: float | None = None,
) -> dict:
    """Exécute `run_symbol` en marquant l'état et en persistant le résultat.

    L'état est remis à zéro dans un `finally` : un symbole dont le backtest plante ne doit pas
    rester « en cours » pour toujours et bloquer toute relance.
    """
    key = symbol_key(symbol, entry_tf)
    _symbol_runs[key] = {"running": True, "started_at": datetime.now(UTC).isoformat()}
    try:
        payload = await run_symbol(symbol, entry_tf=entry_tf, step=step,
                                   ladder=ladder, max_years=max_years)
    finally:
        _symbol_runs[key] = {"running": False, "started_at": None}
    if store is not None:
        try:
            store.records.put(SYMBOL_COLLECTION, key, payload)
        except Exception as exc:  # noqa: BLE001 — la persistance ne doit pas perdre le résultat
            logger.warning("Backtest %s non persisté (%s)", key, exc)
    return payload


def symbol_report(store, symbol: str, entry_tf: str = "1h") -> dict | None:  # noqa: ANN001
    """Dernier backtest persisté pour ce symbole (None s'il n'a jamais été lancé)."""
    return store.records.get(SYMBOL_COLLECTION, symbol_key(symbol, entry_tf))


async def run_backtest(store=None, *, universe: list[str] | None = None,  # noqa: ANN001
                       step_1h: int = 2, step_15m: int = 4,
                       deep: bool | None = None, deep_step: int | None = None,
                       deep_years: float | None = None) -> dict:
    """Backtest COMPLET de la stratégie : passe longue (5 ans) + portée (1 h) + fidélité (15 min).

    Le résultat contient tout ce qu'il faut pour décider : nombre de trades, gagnants, perdants,
    taux de réussite, R/R moyen, FRÉQUENCE d'opportunité par symbole, classement des paires par
    fiabilité, et le profil commun des trades qui ont touché le stop.
    """
    s = get_settings()
    universe = tradable(universe or full_universe())
    deep = s.playbook_backtest_deep_enabled if deep is None else deep
    deep_step = s.playbook_backtest_deep_step if deep_step is None else deep_step
    deep_years = s.playbook_backtest_deep_years if deep_years is None else deep_years
    if _state["running"]:
        raise RuntimeError("un backtest est déjà en cours — attendre qu'il se termine")
    _state.update(running=True, started_at=datetime.now(UTC).isoformat())
    long_pass: dict | None = None
    try:
        if deep:
            _set_phase(f"passe longue ({deep_years:g} ans, échelle swing)", 0, len(universe))
            long_pass = await run_pass(universe, entry_tf="1d", step=deep_step,
                                       ladder="swing", max_years=deep_years)
        _set_phase("passe portée (1 h)", 0, len(universe))
        scope = await run_pass(universe, entry_tf="1h", step=step_1h)
        _set_phase("passe fidélité (15 min)", 0, len(universe))
        fidelity = await run_pass(universe, entry_tf="15m", step=step_15m)
    finally:
        _state.update(running=False, phase=None)

    payload = {
        "date": datetime.now(UTC).date().isoformat(),
        "strategy": (
            "Playbook — tendance multi-indicateurs (D1/4h/1h/15min, figée) · entrée 15 min par "
            "confluence pondérée · stop et objectifs posés sur des niveaux, à l'échelle d'un swing "
            "· R/R 1:2–1:3 · sécurisation +2R puis 80 % de TP1"
        ),
        "universe": universe,
        "long": long_pass,       # 5 ans, échelle swing (mensuel/hebdo/journalier)
        "scope": scope,          # 1 h, profondeur maximale, réglage de production
        "fidelity": fidelity,    # 15 min, le vrai déclencheur, sur l'historique disponible
        "data_limits": {
            "note": (
                "Ce que les fournisseurs gratuits servent réellement, mesuré et non supposé : "
                "15 min ≈ 60 jours, 1 h et 4 h ≈ 730 jours, journalier / hebdomadaire / mensuel "
                "≈ 10 ans. Un backtest de CINQ ANS avec une entrée 15 min est donc impossible — il "
                "faudrait inventer les bougies. La passe LONGUE monte toute l'échelle d'un cran "
                "(contexte mensuel + hebdomadaire, confirmations et entrée en journalier) : mêmes "
                "règles, même code, cinq ans de données RÉELLES. Réserve assumée : faute d'une "
                "quatrième unité de temps sur cette profondeur, ses deux couches de confirmation "
                "sont portées par les mêmes bougies journalières — son filtre est donc un cran "
                "moins sévère que celui de la production."
            ),
            "m15_days": 60,
            "h1_years": 2.0,
            "h4_years": 2.0,
            "daily_years": 10,
            "weekly_years": 10,
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
        "Backtest playbook : %d trades (5 ans) / %d trades (1 h) / %d trades (15 min) sur %d paires",
        (long_pass or {}).get("trades", 0), scope["trades"], fidelity["trades"], len(universe),
    )
    return payload


def write_conclusion(payload: dict) -> dict:
    """Conclusion RÉDIGÉE, marché par marché et paire par paire — uniquement des chiffres mesurés."""
    scope, fidelity = payload["scope"], payload["fidelity"]
    o = scope["overall"]
    lines: list[str] = []

    if not o["trades"]:
        # Structure IDENTIQUE au cas nominal : une interface qui doit tester la présence de chaque
        # clé selon le résultat finit toujours par en oublier une.
        return {"headline": "Aucun trade conforme à la stratégie sur la période testée.",
                "lines": ["Les trois étapes (tendance validée, au moins trois confirmations à "
                          "l'entrée, R/R 1:2–1:3 à l'échelle d'un swing) n'ont jamais été "
                          "entièrement satisfaites sur l'historique disponible."],
                "markets": {}, "market_tops": {}, "verdict": "non mesurable",
                "long": {}, "frequency": scope.get("frequency") or {},
                "volume_levers": volume_levers(scope)}

    # `profit_factor is None` signifie « aucun perdant », donc INFINI — la même convention que
    # `_verdict`. L'écrire `(o["profit_factor"] or 0)` rétrogradait le meilleur résultat possible
    # (aucune perte) en « marginal », et l'affichait « profit factor de None ».
    pf_value = float("inf") if o["profit_factor"] is None else o["profit_factor"]
    pf_txt = "∞ (aucun perdant)" if o["profit_factor"] is None else f"{o['profit_factor']}"
    verdict = ("exploitable" if o["expectancy_r"] >= 0.25 and pf_value >= 1.5
               else "marginal" if o["expectancy_r"] > 0 else "non exploitable")
    headline = (
        f"Sur {scope['years_covered']} ans et {scope['pairs_tested']} paires, la stratégie a produit "
        f"{o['trades']} trades : {o['wins']} gagnants et {o['losses']} perdants, soit "
        f"{o['win_rate']} % de réussite, pour une espérance de {o['expectancy_r']:+.2f} R par trade "
        f"et un profit factor de {pf_txt}. Verdict global : {verdict}."
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

    # Marchés : TOUTES les classes d'actifs, pas seulement les métaux et les devises. La classe est
    # déduite du symbole par le même routeur que la production — un marché ne peut donc pas être
    # rangé ici autrement que là où il est réellement chargé.
    markets = _markets_breakdown(scope["ranking"])
    for label, m in markets.items():
        lines.append(
            f"{label} : {m['trades']} trades sur {m['pairs']} instrument(s), {m['win_rate']} % "
            f"de réussite, espérance {m['expectancy_r']:+.2f} R. Meilleur instrument : {m['best']} "
            f"({m['best_expectancy_r']:+.2f} R)."
        )

    # LES MEILLEURS INSTRUMENTS DE CHAQUE MARCHÉ, nommés. C'est la réponse à « liste-moi les
    # 10 meilleures paires par marché » — et elle est écrite, pas seulement rendue en JSON.
    tops = scope.get("market_tops") or {}
    for block in tops.values():
        if not block["top"]:
            continue
        names = " · ".join(
            f"{r['symbol']} ({r['expectancy_r']:+.2f} R, {r['trades']} tr, {r['win_rate']} %)"
            for r in block["top"]
        )
        lines.append(f"TOP {block['label']} — {names}.")

    # Fréquence : la question « combien de trades par jour » a sa propre réponse, mesurée.
    freq = scope.get("frequency") or {}
    if freq.get("symbols"):
        lines.append(
            f"FRÉQUENCE (entrée évaluée en 1 h) — {freq['avg_trades_per_day_per_symbol']:.3f} trade "
            f"par jour et par symbole en moyenne, soit un trade tous les "
            f"{freq['days_between_trades_per_symbol']:.0f} jours par symbole. Sur les "
            f"{freq['symbols']} symboles mesurés : {freq['universe_trades_per_day']:.1f} par jour et "
            f"{freq['universe_trades_per_week']:.0f} par semaine. En production l'entrée se prend en "
            "15 min, soit quatre fois plus d'occasions de déclenchement : ce chiffre est un PLANCHER."
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

    # Passe LONGUE : cinq ans, tous les marchés. Elle a sa propre section — c'est elle qui répond à
    # « où cette stratégie est-elle fiable sur la durée », avec le classement trié qui va avec.
    long_pass = payload.get("long")
    long_block: dict = {}
    if long_pass and not long_pass["overall"]["trades"]:
        # Ne JAMAIS présenter une section vide comme si la mesure avait eu lieu : une passe qui ne
        # produit aucun trade est une information, et elle a une cause qu'il faut nommer.
        long_block = {
            "measurable": False,
            "years": long_pass["years_covered"],
            "pairs_tested": long_pass["pairs_tested"],
            "note": (
                "Aucun trade sur la passe longue. Ce n'est pas une panne : l'étape d'entrée de la "
                "stratégie repose sur de la micro-structure lue SUR L'UNITÉ D'ENTRÉE (zones "
                "d'offre/demande, cassure de structure, figure de retournement, VWAP, volume). "
                "Quand cette unité devient le journalier, cette information a déjà été consommée "
                "par l'étape de tendance et il ne reste plus de confirmation indépendante à "
                "réunir. La méthode a donc besoin d'une unité d'entrée nettement plus fine que son "
                "contexte — c'est une propriété de la stratégie, pas un défaut du backtest."
            ),
        }
        lines.append(
            f"PASSE LONGUE ({long_pass['years_covered']} ans, échelle swing) : AUCUN trade. "
            "L'entrée de la stratégie a besoin d'une unité de temps nettement plus fine que son "
            "contexte ; à l'échelle journalière, ses confirmations n'existent plus. Un backtest de "
            "cinq ans de CETTE stratégie n'est donc pas réalisable avec des données gratuites, qui "
            "plafonnent l'intraday à deux ans. Le dire est plus utile que de publier un chiffre "
            "obtenu sur une méthode qui ne serait plus la sienne."
        )
    if long_pass and long_pass["overall"]["trades"]:
        lo = long_pass["overall"]
        lf = long_pass.get("frequency") or {}
        long_block = {
            "measurable": True,
            "years": long_pass["years_covered"],
            "pairs_tested": long_pass["pairs_tested"],
            "overall": lo,
            "frequency": lf,
            "markets": _markets_breakdown(long_pass["ranking"]),
            "top": [r for r in long_pass["ranking"] if r.get("rank")][:20],
        }
        lines.append(
            f"PASSE LONGUE ({long_pass['years_covered']} ans, échelle swing : contexte mensuel + "
            f"hebdomadaire, entrée journalière) — {lo['trades']} trades sur "
            f"{long_pass['pairs_tested']} instruments, {lo['win_rate']} % de réussite, espérance "
            f"{lo['expectancy_r']:+.2f} R, profit factor {lo['profit_factor']}, pire série de pertes "
            f"{lo['max_drawdown_r']} R. C'est la même méthode, mesurée un cran plus haut faute "
            "d'intraday au-delà de deux ans."
        )

    return {"headline": headline, "verdict": verdict, "lines": lines, "markets": markets,
            "market_tops": tops,
            "long": long_block, "frequency": freq,
            "volume_levers": volume_levers(scope)}


def _markets_breakdown(ranking: list[dict]) -> dict[str, dict]:
    """Ventilation par CLASSE D'ACTIF, en utilisant le routeur de données de la production."""
    from app.data import markets as markets_mod

    labels = {"forex": "Forex", "commodity": "Métaux précieux", "index": "Indices",
              "stock": "Actions", "crypto": "Crypto"}
    buckets: dict[str, list[dict]] = {}
    for row in ranking:
        if not row.get("trades"):
            continue
        buckets.setdefault(markets_mod.asset_class(row["symbol"]), []).append(row)

    out: dict[str, dict] = {}
    for cls, rows in buckets.items():
        total = sum(r["trades"] for r in rows)
        best = max(rows, key=lambda r: r["expectancy_r"])
        out[labels.get(cls, cls)] = {
            "pairs": len(rows), "trades": total,
            "win_rate": round(sum(r["wins"] for r in rows) / total * 100, 1),
            "expectancy_r": round(sum(r["expectancy_r"] * r["trades"] for r in rows) / total, 3),
            "trades_per_day": round(sum(r.get("trades_per_day") or 0.0 for r in rows), 3),
            "best": best["symbol"],
            "best_expectancy_r": best["expectancy_r"],
        }
    # Du marché le plus rentable au moins rentable : la lecture utile est celle-là.
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["expectancy_r"]))


def volume_levers(one_pass: dict) -> dict:
    """LEVIERS DE VOLUME — comment obtenir plus de trades, classés par ce qu'ils coûtent en qualité.

    Le nombre de trades n'est pas limité par le réglage `daily_top_trades_count` (jamais atteint)
    mais par la SÉLECTIVITÉ de la stratégie. Il n'y a donc que deux familles de leviers, et elles ne
    se valent pas du tout :

    1. **Gratuits** — plus de symboles, plus d'occasions de déclenchement. Aucune règle n'est
       assouplie : chaque trade reste soumis aux trois étapes, l'espérance par trade est inchangée.
       C'est le seul levier qui augmente le gain TOTAL sans dégrader le gain par trade.
    2. **Payants** — desserrer un seuil (confluence, tendance, R/R, échelle du trade). Ils ajoutent
       des trades PIRES que la moyenne actuelle, par construction : ce sont ceux que les seuils
       écartaient. Ils ne doivent jamais être appliqués sans un A/B mesuré (`run_volume_ab`).

    Cette fonction ne décide rien : elle chiffre le levier gratuit sur les données du passage et
    liste les leviers payants avec le paramètre exact à mesurer.
    """
    freq = one_pass.get("frequency") or {}
    avg = freq.get("avg_trades_per_day_per_symbol") or 0.0
    ranking = [r for r in one_pass.get("ranking", []) if r.get("trades_per_day")]
    ranking.sort(key=lambda r: -(r.get("trades_per_day") or 0.0))
    return {
        "note": (
            "Le plafond de trades proposés n'est PAS ce qui limite le volume : il n'était jamais "
            "atteint. C'est la sélectivité de la stratégie. Deux familles de leviers, à ne pas "
            "confondre — élargir l'univers ne coûte rien en qualité, desserrer un seuil si."
        ),
        "free": {
            "lever": "élargir l'univers balayé",
            "why": (
                "chaque symbole ajoute son propre taux d'opportunité sans toucher aux règles : "
                "l'espérance par trade reste identique, seul le gain total monte"
            ),
            "measured_rate_per_symbol_per_day": round(avg, 4),
            "projection_trades_per_day": {str(n): round(avg * n, 2) for n in (16, 24, 40, 60, 88)},
            "projection_trades_per_week": {str(n): round(avg * n * 7, 1) for n in (16, 24, 40, 60, 88)},
            "best_producers": [
                {"symbol": r["symbol"], "trades_per_day": r["trades_per_day"],
                 "days_between_trades": r.get("days_between_trades"),
                 "expectancy_r": r["expectancy_r"], "r_per_month": r.get("r_per_month")}
                for r in ranking[:12]
            ],
        },
        "free_secondary": {
            "lever": "entrée en 15 min plutôt qu'en 1 h",
            "why": (
                "déjà le réglage de PRODUCTION : quatre fois plus de bougies, donc quatre fois plus "
                "d'occasions pour le déclencheur de se former sur le même contexte. Les chiffres de "
                "la passe portée sont pour cette raison un plancher, pas une prévision"
            ),
        },
        "costly": [
            {"setting": "playbook_confluence_min_score", "current": get_settings().playbook_confluence_min_score,
             "direction": "baisser (3,0 → 2,5)",
             "effect": "accepte les entrées à deux confirmations fortes au lieu de trois",
             "risk": "ce sont exactement les setups que le seuil écartait : espérance plus basse"},
            {"setting": "playbook_allow_divergence_entry", "current": get_settings().playbook_allow_divergence_entry,
             "direction": "réactiver",
             "effect": "rouvre un troisième type de déclencheur",
             "risk": "MESURÉ perdant : 37,5 % de réussite et +0,19 R sur 16 trades, contre 69 % et "
                     "+1,15 R pour la cassure. Il dilue l'espérance"},
            {"setting": "playbook_min_target_atr_daily", "current": get_settings().playbook_min_target_atr_daily,
             "direction": "baisser (1,80 → 1,40)",
             "effect": "des objectifs plus proches deviennent admissibles, donc plus de setups passent",
             "risk": "mesuré à −0,05 R d'espérance pour +19 trades lors de la calibration"},
            {"setting": "playbook_max_rr", "current": get_settings().playbook_max_rr,
             "direction": "remonter (3,0 → 4,0)",
             "effect": "élargit la bande de R/R admissible",
             "risk": "mesuré perdant dans l'autre sens : 1:3 donne +0,814 R contre +0,714 R à 1:4"},
            {"setting": "playbook_trend_min_score", "current": get_settings().playbook_trend_min_score,
             "direction": "baisser (0,10 → 0,05)",
             "effect": "nomme une direction sur des tendances plus molles",
             "risk": "non mesuré — à passer par run_volume_ab avant toute application"},
        ],
    }


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
    universe = tradable(universe or DEFAULT_UNIVERSE)
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


STRATEGY_AB_COLLECTION = "playbook_strategy_ab"

# Deux stratégies COMPLÈTES, rejouées sur les mêmes données : l'ancienne (biais mensuel+journalier,
# déclencheurs seuls, R/R plafonné à 1:3 avec plancher de 200 pips) et la nouvelle (tendance
# multi-indicateurs avec verrou ADX, confluence pondérée, stop et objectif posés sur des niveaux,
# R/R 1:2 à 1:3, gestion TP1 -> TP2).
STRATEGY_VARIANTS: dict[str, dict] = {
    "legacy": {
        "trend_engine": False,
        "entry_mode": "legacy",
        "min_target_pips": 200.0,
        "max_rr": 3.0,
    },
    "refonte": {},          # les réglages de production, tels qu'ils sont
}


async def run_strategy_ab(
    store=None, *, universe: list[str] | None = None, step: int = 3,  # noqa: ANN001
) -> dict:
    """Compare la stratégie refondue à la précédente, sur les MÊMES données et la même période.

    C'est la seule façon honnête de savoir ce que la refonte apporte : rejouer les deux versions
    côte à côte plutôt que de comparer un nouveau chiffre à un souvenir. On regarde trois choses :
    l'espérance en R, le profit factor, et le NOMBRE DE TRADES — un filtre plus sévère améliore
    mécaniquement l'espérance en ne gardant que les cas faciles, ce qui n'est pas un progrès si le
    volume s'effondre.

    Comme pour l'A/B volatilité, la gagnante n'est PAS appliquée automatiquement.
    """
    universe = tradable(universe or DEFAULT_UNIVERSE)
    if _state["running"]:
        raise RuntimeError("un backtest est déjà en cours — attendre qu'il se termine")
    _state.update(running=True, started_at=datetime.now(UTC).isoformat())
    results: dict[str, dict] = {}
    try:
        for name, overrides in STRATEGY_VARIANTS.items():
            _set_phase(f"A/B stratégie — variante « {name} »", 0, len(universe))
            one = await run_pass(universe, entry_tf="1h", step=step, overrides=overrides)
            results[name] = {
                "overall": one["overall"],
                "by_symbol": one["by_symbol"],
                "failures": one["failures"],
                "trades": one["trades"],
                "secure_ab": one.get("secure_ab"),
                "tp_management_ab": one.get("tp_management_ab"),
            }
    finally:
        _state.update(running=False, phase=None)

    legacy = (results.get("legacy") or {}).get("overall") or {}
    refonte = (results.get("refonte") or {}).get("overall") or {}
    n_legacy, n_refonte = legacy.get("trades", 0), refonte.get("trades", 0)
    volume_drop = (1 - n_refonte / n_legacy) if n_legacy else 0.0
    verdict = _strategy_verdict(legacy, refonte, volume_drop)
    payload = {
        "date": datetime.now(UTC).date().isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "universe": universe,
        "step": step,
        "variants": results,
        "volume_drop_pct": round(volume_drop * 100, 1),
        "verdict": verdict,
        "note": (
            f"ancienne : {n_legacy} trades, {legacy.get('win_rate', 0)} %, "
            f"{legacy.get('expectancy_r', 0):+.2f} R · "
            f"refonte : {n_refonte} trades, {refonte.get('win_rate', 0)} %, "
            f"{refonte.get('expectancy_r', 0):+.2f} R. {verdict}"
        ),
    }
    if store is not None:
        try:
            store.records.put(STRATEGY_AB_COLLECTION, payload["date"], payload)
            store.records.put(STRATEGY_AB_COLLECTION, LATEST, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("A/B stratégie non persisté (%s)", exc)
    logger.info("A/B stratégie terminé : %s", payload["note"])
    return payload


# =======================================================================================
# A/B VOLUME — « comment avoir plus de trades », répondu par la mesure et non par l'intuition
# =======================================================================================
VOLUME_AB_COLLECTION = "playbook_volume_ab"

#: Chaque variante desserre UN seul verrou, pour qu'on sache à qui attribuer le résultat. Une
#: combinaison (« tout desserré ») ferme la marche : elle donne la borne haute du volume atteignable
#: et le prix à payer. Élargir l'univers n'est PAS ici — ce levier ne change aucun réglage, il
#: multiplie simplement le nombre de symboles balayés, et son effet se lit dans `frequency`.
VOLUME_VARIANTS: dict[str, dict] = {
    "reference": {},
    "confluence_2_5": {"confluence_min_score": 2.5},
    "confluence_2_0": {"confluence_min_score": 2.0},
    "divergence_on": {"allow_divergence": True},
    "target_1_4": {"min_target_atr_daily": 1.4},
    "rr_max_4": {"max_rr": 4.0},
    "trend_soft": {"trend_min_score": 0.05},
    "tout_desserre": {"confluence_min_score": 2.5, "allow_divergence": True,
                      "min_target_atr_daily": 1.4, "trend_min_score": 0.05},
}


async def run_volume_ab(
    store=None, *, universe: list[str] | None = None, step: int = 4,  # noqa: ANN001
    variants: dict[str, dict] | None = None,
) -> dict:
    """Mesure ce que CHAQUE assouplissement rapporte en trades et ce qu'il coûte en qualité.

    La règle du desk est qu'un réglage ne se change pas sur un raisonnement mais sur un résultat.
    Ce run rejoue la même passe autant de fois qu'il y a de variantes, sur les MÊMES symboles et la
    même période, et rapporte pour chacune : le nombre de trades, la fréquence par jour, l'espérance
    en R, le profit factor et le drawdown maximal.

    La lecture qui compte est le **gain total en R** : une variante qui double le volume en perdant
    la moitié de l'espérance ne rapporte rien de plus, elle prend juste deux fois plus de risque. Une
    variante n'est retenue que si elle augmente le total SANS dégrader le profit factor ni le
    drawdown — c'est le critère explicite du desk, et il n'est pas appliqué automatiquement.
    """
    universe = tradable(universe or full_universe())
    variants = variants or VOLUME_VARIANTS
    if _state["running"]:
        raise RuntimeError("un backtest est déjà en cours — attendre qu'il se termine")
    _state.update(running=True, started_at=datetime.now(UTC).isoformat())
    results: dict[str, dict] = {}
    try:
        for name, overrides in variants.items():
            _set_phase(f"A/B volume — variante « {name} »", 0, len(universe))
            one = await run_pass(universe, entry_tf="1h", step=step, overrides=overrides)
            results[name] = {
                "overrides": overrides,
                "overall": one["overall"],
                "frequency": one["frequency"],
                "failures": len(one["failures"]),
                "trades": one["trades"],
            }
    finally:
        _state.update(running=False, phase=None)

    ref = (results.get("reference") or {}).get("overall") or {}
    rows = []
    for name, r in results.items():
        o = r["overall"]
        rows.append({
            "variant": name,
            "overrides": r["overrides"],
            "trades": o["trades"],
            "trades_per_day": (r["frequency"] or {}).get("universe_trades_per_day"),
            "win_rate": o["win_rate"],
            "expectancy_r": o["expectancy_r"],
            "profit_factor": o["profit_factor"],
            "max_drawdown_r": o["max_drawdown_r"],
            "total_r": o["total_r"],
            "verdict": _volume_verdict(o, ref),
        })
    # Le classement se fait sur le GAIN TOTAL : c'est la seule grandeur qui arbitre entre « plus de
    # trades » et « de meilleurs trades » sans choisir d'avance.
    rows.sort(key=lambda r: r["total_r"], reverse=True)

    payload = {
        "date": datetime.now(UTC).date().isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "universe": universe, "step": step,
        "reference": ref,
        "rows": rows,
        "variants": results,
        "note": (
            "Classement par gain TOTAL en R. Une variante n'est adoptable que si elle augmente le "
            "total sans dégrader le profit factor ni le drawdown maximal — sinon elle achète du "
            "volume avec de la qualité. Aucune n'est appliquée automatiquement : changer un seuil "
            "de la stratégie reste une décision de l'utilisateur."
        ),
    }
    if store is not None:
        try:
            store.records.put(VOLUME_AB_COLLECTION, payload["date"], payload)
            store.records.put(VOLUME_AB_COLLECTION, LATEST, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("A/B volume non persisté (%s)", exc)
    logger.info("A/B volume terminé : %s",
                " · ".join(f"{r['variant']} {r['trades']} tr {r['expectancy_r']:+.2f} R" for r in rows))
    return payload


def _volume_verdict(variant: dict, ref: dict) -> str:
    """Verdict d'une variante face à la référence : le volume gagné vaut-il ce qu'il coûte ?"""
    if not ref.get("trades") or not variant.get("trades"):
        return "échantillon insuffisant — ne conclut rien"
    d_trades = variant["trades"] / ref["trades"] - 1
    d_exp = variant["expectancy_r"] - ref["expectancy_r"]
    d_total = variant["total_r"] - ref["total_r"]
    pf, pf_ref = variant["profit_factor"] or 0.0, ref["profit_factor"] or 0.0
    dd, dd_ref = variant["max_drawdown_r"], ref["max_drawdown_r"]
    if abs(d_trades) < 0.02 and abs(d_exp) < 0.02:
        return "sans effet mesurable"
    if d_total > 0 and pf >= pf_ref and dd <= dd_ref:
        return (f"✅ adoptable — {d_trades:+.0%} de trades, gain total {d_total:+.1f} R, profit "
                f"factor et drawdown au moins aussi bons")
    if d_total > 0:
        return (f"🟡 arbitrage — {d_trades:+.0%} de trades et {d_total:+.1f} R de gain total, mais "
                f"profit factor {pf} contre {pf_ref} et drawdown {dd} contre {dd_ref} : on achète "
                "du volume avec de la qualité")
    return (f"🔴 à écarter — {d_trades:+.0%} de trades pour {d_total:+.1f} R de gain total "
            f"({d_exp:+.2f} R par trade) : les trades ajoutés sont perdants")


def _strategy_verdict(legacy: dict, refonte: dict, volume_drop: float) -> str:
    """Conclusion en une phrase, sans arrondir les angles quand le résultat est mauvais."""
    if not legacy.get("trades") or not refonte.get("trades"):
        return ("Échantillon insuffisant d'un côté au moins : cette comparaison ne conclut rien.")
    delta = refonte.get("expectancy_r", 0.0) - legacy.get("expectancy_r", 0.0)
    if delta <= 0:
        return (f"La refonte fait {delta:+.2f} R par trade contre l'ancienne version : elle "
                "n'apporte rien de mesurable ici, il faut recalibrer avant de la figer.")
    if volume_drop > 0.4:
        return (f"Espérance en hausse de {delta:+.2f} R, mais le nombre de trades chute de "
                f"{volume_drop:.0%} : c'est probablement du sur-filtrage. Desserrer le verrou ADX "
                "ou le seuil de confluence avant de conclure.")
    return (f"La refonte gagne {delta:+.2f} R par trade pour "
            f"{'un volume comparable' if volume_drop < 0.15 else f'un volume en baisse de {volume_drop:.0%}'}.")
