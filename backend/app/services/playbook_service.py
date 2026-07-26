"""Exécution du PLAYBOOK sur les marchés réels — chargement multi-unités de temps + sélection.

Deux responsabilités :
1. `build_setup(symbol)` — charge Mensuel / Journalier / 4 h / 15 min (avec cache TTL adapté à
   chaque unité) puis applique `domain.playbook.build`.
2. `top_trades(n)` — balaie l'univers pertinent pour la session en cours (avec priorité au
   chevauchement Londres/New York) et retourne les N meilleurs setups, classés.

Le cache évite de recharger le mensuel toutes les minutes : une bougie mensuelle ne change pas
plus vite qu'une bougie mensuelle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from app.core.config import get_settings
from app.data import markets
from app.data import sessions as sessions_mod
from app.domain import playbook
from app.domain.indicators import Candle
from app.domain.playbook import PlaybookSetup

logger = logging.getLogger(__name__)

# Unités de temps de la stratégie et durée de vie du cache (secondes).
_TIMEFRAMES = [("monthly", "1M", 120), ("daily", "1d", 300), ("h4", "4h", 300), ("m15", "15m", 300)]
_TTL = {"1M": 21_600, "1d": 3_600, "4h": 900, "15m": 90}

_cache: dict[tuple[str, str], tuple[float, list[Candle], str]] = {}
_MAX_PARALLEL = 4  # symboles analysés en parallèle (borne la charge réseau)


def clear_cache() -> None:
    """Vide le cache multi-UT (utile en test et au rafraîchissement forcé)."""
    _cache.clear()


async def _load(symbol: str, interval: str, limit: int) -> tuple[list[Candle], str]:
    key = (symbol.upper(), interval)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1], hit[2]
    candles = await markets.load_candles(symbol, interval=interval, limit=limit)
    source = markets.data_source(symbol)
    _cache[key] = (now + _TTL.get(interval, 300), candles, source)
    return candles, source


async def load_mtf(symbol: str) -> dict:
    """Charge les 4 unités de temps de la stratégie, en parallèle, avec cache."""
    results = await asyncio.gather(
        *(_load(symbol, interval, limit) for _, interval, limit in _TIMEFRAMES),
        return_exceptions=True,
    )
    out: dict = {"sources": {}, "real": True}
    for (name, interval, _), res in zip(_TIMEFRAMES, results, strict=True):
        if isinstance(res, Exception):
            logger.warning("Playbook %s %s : chargement échoué (%s)", symbol, interval, res)
            out[name], out["sources"][name], out["real"] = [], "error", False
            continue
        candles, source = res
        out[name] = candles
        out["sources"][name] = source
        if source not in ("live", "real"):
            out["real"] = False
    return out


async def build_setup(symbol: str, *, now: datetime | None = None) -> PlaybookSetup:
    """Applique la stratégie complète à `symbol` et retourne le setup (ou le refus motivé)."""
    s = get_settings()
    data = await load_mtf(symbol)
    session = sessions_mod.session_context(now)
    setup = playbook.build(
        symbol,
        data["monthly"], data["daily"], data["h4"], data["m15"],
        session=session,
        min_rr=s.playbook_min_rr,
        min_target_pips=s.playbook_min_target_pips,
        max_stop_pips=s.playbook_max_stop_pips,
        max_rr=s.playbook_max_rr,
        max_atr_multiple=s.playbook_max_atr_multiple,
    )
    setup.levels["data_sources"] = data["sources"]
    # Garde-fou d'honnêteté : sur données de démo (synthétiques), on n'affirme AUCUN trade —
    # et on n'oppose pas non plus de veto aux autres agents (`insufficient` neutralise le veto).
    if s.playbook_require_real_data and not data["real"]:
        setup.insufficient = True
        setup.direction = "NO_TRADE"
        setup.ready = False
        setup.reasons = ["données de marché non réelles (repli démo) — aucun trade affirmé"] + setup.reasons
    return setup


# ---------------------------------------------------------------------------------------
# Sélection du jour
# ---------------------------------------------------------------------------------------
def daily_universe(now: datetime | None = None, limit: int = 24) -> list[dict]:
    """Univers à scanner selon le moment : priorité au chevauchement Londres/New York.

    - Pendant le chevauchement -> paires travaillées par les DEUX desks (majeures) + or + crypto.
    - Sinon -> univers de la (des) session(s) ouverte(s), crypto incluse (24/7).
    """
    ctx = sessions_mod.session_context(now)
    if ctx["overlap"]:
        universe = sessions_mod.overlap_universe()
    else:
        universe = []
        seen: set[str] = set()
        for name in ctx["active"] or ["london"]:
            for item in sessions_mod.session_universe(name):
                if item["symbol"] not in seen:
                    seen.add(item["symbol"])
                    universe.append(item)
        universe += [{"symbol": s, "asset_class": "commodity"} for s in ("XAU/USD", "XAG/USD")
                     if s not in seen]
    return universe[:limit]


def _rank_key(item: dict) -> tuple:
    """Classement : setups prêts d'abord, puis confiance, puis R/R, puis objectif en pips."""
    return (
        1 if item["tier"] == "ready" else 0,
        item.get("confidence", 0.0),
        item.get("risk_reward", 0.0),
        item.get("reward_pips", 0.0),
    )


async def top_trades(
    count: int = 5,
    *,
    universe: list[dict] | None = None,
    now: datetime | None = None,
    include_armed: bool = True,
) -> dict:
    """Les `count` meilleurs trades du moment, strictement selon la stratégie.

    Deux niveaux, explicitement étiquetés (on ne maquille jamais un setup incomplet) :
    - ``ready`` : les 4 étapes validées ET le déclencheur 15 min actif -> exécutable maintenant.
    - ``armed`` : contexte mensuel/journalier/4 h validé, en attente du déclencheur 15 min ->
      à surveiller, l'alerte partira dès que le déclencheur se forme.
    """
    universe = universe or daily_universe(now)
    session = sessions_mod.session_context(now)
    sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def _one(item: dict) -> dict | None:
        async with sem:
            try:
                setup = await build_setup(item["symbol"], now=now)
            except Exception as exc:  # noqa: BLE001 — un symbole KO ne casse pas la sélection
                logger.warning("Playbook %s échoué (%s)", item["symbol"], exc)
                return None
        if setup.insufficient:
            return None
        d = setup.as_dict()
        d["asset_class"] = item["asset_class"]
        if setup.ready:
            d["tier"] = "ready"
        elif setup.context_ok:
            # Étapes 1-3 validées : il ne manque que le déclencheur 15 min -> à surveiller.
            d["tier"] = "armed"
            d["direction"] = "BUY" if setup.bias > 0 else "SELL"
        else:
            return None
        d["summary"] = setup.summary()
        return d

    raw = await asyncio.gather(*(_one(i) for i in universe))
    found = [r for r in raw if r]
    if not include_armed:
        found = [r for r in found if r["tier"] == "ready"]
    found.sort(key=_rank_key, reverse=True)
    picks = found[:count]

    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "session": session,
        "strategy": "Playbook MTF — Mensuel/Journalier → 4 h → entrée 15 min · R/R ≥ 1:2 · ≥ 100 pips",
        "scanned": len(universe),
        "ready": sum(1 for p in picks if p["tier"] == "ready"),
        "requested": count,
        "picks": picks,
        "note": _selection_note(picks, count, session),
    }


def _selection_note(picks: list[dict], count: int, session: dict) -> str:
    ready = sum(1 for p in picks if p["tier"] == "ready")
    armed = len(picks) - ready
    if not picks:
        return (f"Aucun setup conforme à la stratégie en ce moment ({session['label']}). "
                "S'abstenir est une décision de trading à part entière.")
    parts = []
    if ready:
        parts.append(f"{ready} setup(s) exécutable(s) maintenant (déclencheur 15 min actif)")
    if armed:
        parts.append(f"{armed} setup(s) armé(s) : contexte validé, en attente du déclencheur 15 min")
    txt = " · ".join(parts)
    if len(picks) < count:
        txt += (f". Seulement {len(picks)}/{count} : le marché n'en offre pas davantage qui respecte "
                "les 4 étapes + R/R 1:2 + 100 pips. Forcer les 5 reviendrait à dégrader la stratégie.")
    return txt
