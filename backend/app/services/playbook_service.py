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
# Le 1 h ne sert PAS de confirmation supplémentaire : il fournit les niveaux qui BORNENT l'objectif
# (cf. domain.playbook.target_barrier). Le stop, lui, vient toujours du 15 min.
_TIMEFRAMES = [
    ("monthly", "1M", 120), ("daily", "1d", 300), ("h4", "4h", 300),
    ("h1", "1h", 300), ("m15", "15m", 300),
]
# Le 15 min a le TTL le plus court : c'est l'unité d'ENTRÉE, celle qui déclenche l'auto-entrée.
_TTL = {"1M": 21_600, "1d": 3_600, "4h": 900, "1h": 600, "15m": 60}

_cache: dict[tuple[str, str], tuple[float, list[Candle], str]] = {}


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
        # Le 1 h ne fait que BORNER l'objectif : son absence dégrade la précision de la cible
        # (repli sur le 4 h), elle n'invalide pas la qualité des données du trade.
        optional = name == "h1"
        if isinstance(res, Exception):
            logger.warning("Playbook %s %s : chargement échoué (%s)", symbol, interval, res)
            out[name], out["sources"][name] = [], "error"
            if not optional:
                out["real"] = False
            continue
        candles, source = res
        out[name] = candles
        out["sources"][name] = source
        if source not in ("live", "real") and not optional:
            out["real"] = False
    return out


async def build_setup(symbol: str, *, now: datetime | None = None) -> PlaybookSetup:
    """Applique la stratégie complète à `symbol` et retourne le setup (ou le refus motivé)."""
    s = get_settings()
    data = await load_mtf(symbol)
    session = sessions_mod.session_context(now)
    # `playbook.build` est du calcul PUR (5 couches d'indicateurs sur ~200 bougies) : exécuté
    # directement dans la boucle d'événements, il la bloque pendant toute sa durée. Multiplié par
    # les dizaines de symboles des boucles de fond, cela affamait l'API — les requêtes passaient de
    # 10 ms à plusieurs secondes et les WebSockets tombaient en « pong timeout ». On le déporte
    # donc dans un thread : la boucle reste libre de servir les requêtes pendant le calcul.
    setup = await asyncio.to_thread(
        playbook.build,
        symbol,
        data["monthly"], data["daily"], data["h4"], data["m15"],
        h1=data.get("h1") or None,
        session=session,
        min_rr=s.playbook_min_rr,
        min_target_pips=s.playbook_min_target_pips,
        max_stop_pips=s.playbook_max_stop_pips,
        target_level_buffer=s.playbook_target_level_buffer,
        max_rr=s.playbook_max_rr,
        max_atr_multiple=s.playbook_max_atr_multiple,
        # Marchés fermés -> on analyse quand même, mais aucun setup n'est déclaré exécutable.
        can_trade=(not s.playbook_trade_only_when_open) or bool(session.get("can_trade")),
        # Filtres issus du backtest (divergence peu fiable, volatilité excessive).
        allow_divergence=s.playbook_allow_divergence_entry,
        volatility_filter=s.playbook_volatility_filter,
        volatility_mode=s.playbook_volatility_mode,
        max_atr_pct=s.playbook_max_atr_pct,
        volatility_max_widen=s.playbook_volatility_max_widen,
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
def focus_classes() -> set[str]:
    """Classes d'actifs sur lesquelles le desk travaille (forex + métaux par défaut)."""
    raw = get_settings().playbook_focus_classes or ""
    return {c.strip() for c in raw.split(",") if c.strip()}


def daily_universe(now: datetime | None = None, limit: int | None = None) -> list[dict]:
    """Univers à scanner — FOREX et OR d'abord, dans l'ordre de pertinence du moment.

    La stratégie est calibrée pour ces marchés : objectifs exprimés en pips, niveaux majeurs
    mensuels, horaires de Londres et de New York. Un objectif de « 200 pips » n'a pas le même sens
    sur une action ou une crypto, d'où la priorité donnée aux devises et aux métaux.

    Ordre de balayage :
    1. l'or et l'argent — travaillés sur toute la plage Londres/New York ;
    2. les paires de la (des) session(s) ouverte(s), avec priorité au chevauchement ;
    3. le reste du catalogue FOREX ;
    4. les autres classes d'actifs, seulement si `playbook_focus_only` est désactivé.
    """
    from app.data import symbols as symbols_catalog

    s = get_settings()
    limit = limit or s.playbook_universe_limit
    focus = focus_classes()
    ctx = sessions_mod.session_context(now)
    universe: list[dict] = []
    seen: set[str] = set()

    def _add(items: list[dict]) -> None:
        for item in items:
            cls = item.get("asset_class", "")
            if s.playbook_focus_only and focus and cls not in focus:
                continue
            if item["symbol"] not in seen:
                seen.add(item["symbol"])
                universe.append(item)

    # 1. Métaux précieux : le cœur du desk avec le forex, liquides sur toute la plage Londres/NY.
    _add([{"symbol": sym, "asset_class": "commodity"} for sym in ("XAU/USD", "XAG/USD")])
    # 2. Paires de la session en cours (chevauchement en priorité).
    if ctx["overlap"]:
        _add(sessions_mod.overlap_universe())
    for name in ctx["active"] or ["london"]:
        _add(sessions_mod.session_universe(name))
    # 3-4. Reste du catalogue, filtré (ou non) sur les classes du desk.
    _add([{"symbol": i["symbol"], "asset_class": i["asset_class"]}
          for i in symbols_catalog.all_symbols()])
    return universe[:limit]


def _rank_key(item: dict) -> tuple:
    """Classement des trades : du plus fiable au moins fiable, sur des critères MESURÉS.

    Ordre des critères, du plus décisif au moins décisif :
    1. **exécutable maintenant** — un setup dont le déclencheur 15 min est actif passe devant ;
    2. **fiabilité mesurée** — taux de réussite du walk-forward nocturne pour CE symbole et CE type
       de déclencheur (cf. `training_service`) : c'est de l'historique, pas une opinion ;
    3. **score de fiabilité affiché** — celui du trade s'il est prêt, celui du contexte s'il est armé ;
    4. **confiance** de la stratégie, puis R/R, puis taille de l'objectif.
    """
    reliability = abs(item.get("reliability_score") or item.get("context_reliability") or 0)
    return (
        1 if item["tier"] == "ready" else 0,
        round(item.get("edge_score") or 0.0, 3),
        reliability,
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
    from app.services import training_service

    universe = universe or daily_universe(now)
    session = sessions_mod.session_context(now)
    sem = asyncio.Semaphore(max(1, get_settings().playbook_max_parallel))

    async def _one(item: dict) -> dict | None:
        async with sem:
            try:
                setup = await build_setup(item["symbol"], now=now)
            except Exception as exc:  # noqa: BLE001 — un symbole KO ne casse pas la sélection
                logger.warning("Playbook %s échoué (%s)", item["symbol"], exc)
                return None
        d = setup.as_dict()
        d["asset_class"] = item["asset_class"]
        if setup.insufficient:
            d["tier"] = "insufficient"
        elif setup.ready:
            d["tier"] = "ready"
        elif setup.context_ok:
            # Étapes 1-3 validées : il ne manque que le déclencheur 15 min -> à surveiller.
            # L'auto-entrée s'en charge : dès que le déclencheur se forme, la position s'ouvre.
            d["tier"] = "armed"
            d["direction"] = "BUY" if setup.bias > 0 else "SELL"
        else:
            d["tier"] = "none"
        # Fiabilité MESURÉE : ce que le walk-forward nocturne a constaté sur ce symbole et ce
        # déclencheur. C'est le premier critère de classement après « exécutable maintenant ».
        d["edge"] = training_service.edge_for(item["symbol"], trigger_type=_trigger_type(setup))
        d["edge_score"] = (d["edge"] or {}).get("score", 0.0)
        d["summary"] = setup.summary()
        return d

    raw = await asyncio.gather(*(_one(i) for i in universe))
    evaluated = [r for r in raw if r]
    # VERDICT PAR PAIRE (plan, tâche 2.1/2.7) : chaque symbole évalué porte sa note 🟢/🟡/🔴 issue
    # du backtest hebdomadaire — c'est elle que l'interface affiche et que l'auto-entrée applique.
    try:
        from app.repositories.store import get_store
        from app.services import verdict_service

        _store = get_store()
        for r in evaluated:
            r["pair_verdict"] = verdict_service.brief_for(_store, r["symbol"])
    except Exception as exc:  # noqa: BLE001 — un verdict manquant ne casse pas la sélection
        logger.warning("Verdicts par paire indisponibles (%s)", exc)
    found = [r for r in evaluated if r["tier"] in ("ready", "armed")]
    if not include_armed:
        found = [r for r in found if r["tier"] == "ready"]
    found.sort(key=_rank_key, reverse=True)
    picks = found[:count]
    for rank, p in enumerate(picks, 1):
        p["rank"] = rank

    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "session": session,
        "strategy": (
            "Playbook MTF — Mensuel/Journalier → 4 h → entrée 15 min · stop sur la structure "
            "15 min · objectif borné par le niveau 1 h · R/R 1:1,2 à 1:1,3"
        ),
        "scanned": len(universe),
        "ready": sum(1 for p in picks if p["tier"] == "ready"),
        "armed": sum(1 for p in picks if p["tier"] == "armed"),
        "requested": count,
        "picks": picks,
        # Verdict de la stratégie pour CHAQUE symbole balayé, pas seulement pour les 5 retenus.
        # C'est ce qui permet au scanner et aux pages d'analyse de parler exactement le même
        # langage que les trades du jour, sans relancer un seul calcul.
        "verdicts": {r["symbol"]: _verdict_of(r) for r in evaluated},
        "auto_entry": get_settings().playbook_auto_entry_enabled,
        "note": _selection_note(picks, count, session),
    }


def _verdict_of(row: dict) -> dict:
    """Vue LÉGÈRE du verdict de la stratégie pour un symbole (sans les couches ni la narration)."""
    return {
        "symbol": row["symbol"],
        "asset_class": row.get("asset_class"),
        "tier": row["tier"],
        "direction": row.get("direction"),
        "bias": row.get("bias", 0),
        "context_ok": row.get("context_ok", False),
        "veto": row.get("veto", False),
        "reliability_score": row.get("reliability_score", 0),
        "context_reliability": row.get("context_reliability", 0),
        "confidence": row.get("confidence", 0.0),
        "risk_reward": row.get("risk_reward", 0.0),
        "entry": row.get("entry"),
        "stop_loss": row.get("stop_loss"),
        "take_profit_1": row.get("take_profit_1"),
        "edge_score": row.get("edge_score", 0.0),
        "pair_verdict": row.get("pair_verdict"),
        "reason": (row.get("reasons") or [""])[0],
    }


def _trigger_type(setup: PlaybookSetup) -> str | None:
    """Type de déclencheur (`repli` / `cassure` / `divergence`) extrait du libellé du setup."""
    if not setup.trigger:
        return None
    return setup.trigger.split(" — ", 1)[0].strip() or None


def _selection_note(picks: list[dict], count: int, session: dict) -> str:
    ready = sum(1 for p in picks if p["tier"] == "ready")
    armed = len(picks) - ready
    auto = get_settings().playbook_auto_entry_enabled
    if not picks:
        return (f"Aucun setup conforme à la stratégie en ce moment ({session['label']}). "
                "S'abstenir est une décision de trading à part entière.")
    parts = []
    if ready:
        parts.append(f"{ready} setup(s) exécutable(s) maintenant (déclencheur 15 min actif)")
    if armed:
        parts.append(
            f"{armed} setup(s) armé(s) : contexte validé, "
            + ("ouverture AUTOMATIQUE en démo dès que le déclencheur 15 min se forme"
               if auto else "en attente du déclencheur 15 min")
        )
    txt = " · ".join(parts)
    if len(picks) < count:
        txt += (f". Seulement {len(picks)}/{count} : le marché n'en offre pas davantage qui respecte "
                "les 4 étapes, la bande de R/R 1,2–1,3 et un objectif logeable avant le niveau 1 h. "
                "Forcer les 5 reviendrait à dégrader la stratégie.")
    return txt
