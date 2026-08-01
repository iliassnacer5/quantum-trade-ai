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
# Le 1 h est une véritable ÉTAPE DE CONFIRMATION (cf. domain.playbook.build, étape 4) : il doit
# aller dans le sens du biais avant que le 15 min ne soit autorisé à déclencher l'entrée. Il fournit
# aussi, avec le 15 min, les niveaux qui portent le stop et bornent l'objectif.
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
        # Le 1 h est une étape de confirmation : son absence n'a pas besoin d'être signalée ici
        # comme un défaut de QUALITÉ des données, parce que `build` refusera de toute façon le
        # setup (sa couche 1 h sera `ok=False` -> `insufficient`). On évite ainsi un double motif.
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
        # Marchés fermés -> on analyse quand même, mais aucun setup n'est déclaré exécutable.
        #
        # La disponibilité est évaluée PAR SYMBOLE (`can_trade_symbol`) et non plus à partir du
        # seul contexte de session, qui décrit Londres et New York. La crypto cote en continu :
        # lui appliquer la grille des places boursières refuserait un marché réellement ouvert,
        # avec des bougies réellement fraîches. À l'inverse, une action ou un indice le week-end se
        # négocierait sur la dernière bougie de vendredi — réelle, mais plus d'actualité.
        can_trade=(not s.playbook_trade_only_when_open)
        or sessions_mod.can_trade_symbol(symbol)[0],
        # TOUS les autres réglages viennent d'un point de vérité unique, partagé avec le backtest et
        # l'entraînement : c'est ce qui garantit que le backtest mesure bien ce qui trade.
        **playbook.settings_kwargs(s),
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


def watchlist_symbols() -> list[str]:
    """La liste restreinte (mesurée) — vide si `playbook_watchlist_only` est désactivé."""
    raw = get_settings().playbook_watchlist_symbols or ""
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def excluded_classes() -> set[str]:
    """Classes d'actifs INTERDITES au trading (`playbook_excluded_classes`).

    Par défaut `commodity` : les métaux précieux (or, argent, platine, palladium). C'est une
    exclusion dure, pas une priorité de balayage — elle vaut aussi bien pour le scan que pour
    l'ouverture d'une position.
    """
    raw = get_settings().playbook_excluded_classes or ""
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def is_excluded(symbol: str) -> str | None:
    """Motif d'exclusion de `symbol`, ou None s'il est tradable.

    Point de vérité UNIQUE de l'exclusion : le balayage l'appelle pour ne pas perdre de temps à
    analyser ce qu'on ne tradera pas, et l'exécution l'appelle pour qu'aucun autre chemin (ordre
    manuel, copy-trading, appel d'API direct) ne puisse la contourner.
    """
    from app.data import markets as markets_mod

    cls = markets_mod.asset_class(symbol)
    if cls in excluded_classes():
        label = "métaux précieux (or, argent, platine, palladium)" if cls == "commodity" else cls
        return f"marché exclu du desk : {label}"
    return None


def daily_universe(now: datetime | None = None, limit: int | None = None) -> list[dict]:
    """Univers à scanner — TOUS les marchés, dans l'ordre de pertinence du moment.

    La stratégie ne suppose plus rien de propre au forex : son échelle de trade est exprimée en ATR
    journalier, donc la même règle vaut sur EUR/USD comme sur le DAX. L'ordre ci-dessous n'est
    qu'une priorité de balayage (les plus liquides d'abord), pas une exclusion.

    `limit=0` (le réglage par défaut) ne coupe RIEN : le catalogue entier est balayé. Un plafond
    décidait d'avance où chercher l'edge ; c'est la mesure qui doit le dire.

    `playbook_excluded_classes` (métaux précieux par défaut) retire des classes ENTIÈRES du
    balayage : ce n'est pas un ordre de priorité mais une exclusion, et elle est doublée à
    l'ouverture de position pour qu'aucun chemin ne la contourne (cf. `is_excluded`).

    `playbook_watchlist_only` COURT-CIRCUITE tout ce qui suit : le balayage EN LIGNE (cette
    fonction) ne porte alors que sur les symboles mesurés rentables. Il est désactivé depuis le
    29/07/2026 — le desk trade tous les marchés, et c'est la sélection en aval (plancher de
    fiabilité, carte de l'edge, verdict de paire) qui garde les symboles rentables, pas une liste
    figée. Le backtest complet, lui, continue d'appeler `full_universe()` de son propre module — il
    ne passe jamais par ici, précisément pour continuer à chercher l'edge partout.

    Ordre de balayage (hors watchlist) :
    1. les grands indices — liquides sur de larges plages horaires ;
    2. les paires de la (des) session(s) ouverte(s), avec priorité au chevauchement ;
    3. le reste du catalogue, toutes classes confondues.
    """
    from app.data import markets as markets_mod
    from app.data import symbols as symbols_catalog

    s = get_settings()
    banned = excluded_classes()
    if s.playbook_watchlist_only:
        watch = watchlist_symbols()
        if watch:
            rows = [{"symbol": sym, "asset_class": markets_mod.asset_class(sym)} for sym in watch]
            rows = [r for r in rows if r["asset_class"] not in banned]
            return rows[:limit] if limit else rows

    limit = s.playbook_universe_limit if limit is None else limit
    focus = focus_classes()
    ctx = sessions_mod.session_context(now)
    universe: list[dict] = []
    seen: set[str] = set()

    def _add(items: list[dict]) -> None:
        for item in items:
            cls = item.get("asset_class", "")
            # Exclusion DURE d'abord : une classe interdite n'est même pas analysée. Inutile de
            # dépenser cinq requêtes réseau par symbole pour un marché qu'on ne tradera pas.
            if cls in banned:
                continue
            if s.playbook_focus_only and focus and cls not in focus:
                continue
            if item["symbol"] not in seen:
                seen.add(item["symbol"])
                universe.append(item)

    # 1. Grands indices : liquides sur de larges plages horaires. (Les métaux précieux ouvraient
    #    cette liste jusqu'au 29/07/2026 ; ils sont désormais exclus, cf. `playbook_excluded_classes`.)
    _add([{"symbol": sym, "asset_class": "index"}
          for sym in ("SPX500", "NAS100", "US30", "GER40", "JPN225")])
    # 2. Paires de la session en cours (chevauchement en priorité).
    if ctx["overlap"]:
        _add(sessions_mod.overlap_universe())
    for name in ctx["active"] or ["london"]:
        _add(sessions_mod.session_universe(name))
    # 3-4. Reste du catalogue, filtré (ou non) sur les classes du desk.
    _add([{"symbol": i["symbol"], "asset_class": i["asset_class"]}
          for i in symbols_catalog.all_symbols()])
    return universe[:limit] if limit and limit > 0 else universe


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
    count: int = 0,
    *,
    universe: list[dict] | None = None,
    now: datetime | None = None,
    include_armed: bool = True,
    min_reliability: int | None = None,
) -> dict:
    """TOUS les trades conformes à la stratégie, classés du plus fiable au moins fiable.

    `count=0` (le défaut) ne coupe rien : on rend chaque setup que la stratégie valide. Le
    classement sert à les ORDONNER, plus à les éliminer — un setup conforme qui arrivait 6e était
    auparavant supprimé alors que la méthode ne dit rien de tel. Un `count > 0` reste possible pour
    un appelant qui veut explicitement un extrait (le digest Telegram, par exemple).

    Le seul filtre qui subsiste est un plancher de FIABILITÉ (`daily_min_reliability`, 2/5 par
    défaut) : « tous les trades qui ont un potentiel fiable » n'est pas « tous les trades ». Un
    setup dont la stratégie elle-même ne dépasse pas ce niveau de confiance reste comptabilisé dans
    les verdicts, il n'est simplement pas proposé.

    Deux niveaux, explicitement étiquetés (on ne maquille jamais un setup incomplet) :
    - ``ready`` : les 4 étapes validées ET le déclencheur 15 min actif -> exécutable maintenant.
    - ``armed`` : contexte mensuel/journalier/4 h validé, en attente du déclencheur 15 min ->
      à surveiller, l'alerte partira dès que le déclencheur se forme.
    """
    from app.services import training_service

    universe = universe or daily_universe(now)
    floor = get_settings().daily_min_reliability if min_reliability is None else min_reliability
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
    conform = [r for r in evaluated if r["tier"] in ("ready", "armed")]
    if not include_armed:
        conform = [r for r in conform if r["tier"] == "ready"]
    # Plancher de fiabilité : un setup ARMÉ est jugé sur son contexte (`context_reliability`), un
    # setup PRÊT sur le trade lui-même (`reliability_score`). Les deux sont signés (+ achat / −
    # vente) : c'est la valeur absolue qui exprime le niveau de confiance.
    def _grade(row: dict) -> int:
        return abs(row.get("reliability_score") or row.get("context_reliability") or 0)

    found = [r for r in conform if _grade(r) >= floor]
    below = len(conform) - len(found)
    found.sort(key=_rank_key, reverse=True)
    picks = found[:count] if count and count > 0 else found
    for rank, p in enumerate(picks, 1):
        p["rank"] = rank
        # LE MARCHÉ DE CE SYMBOLE EST-IL OUVERT MAINTENANT ?
        #
        # L'ANALYSE tourne sur tous les marchés en permanence — c'est ainsi qu'on arrive préparé à
        # l'ouverture, et c'est voulu. Mais un setup armé sur une action ou un indice affichait
        # « la position s'ouvrira toute seule dès que le déclencheur se formera », ce qui est FAUX
        # quand la place est fermée : le garde-fou d'heures de marché l'en empêchera. La carte
        # promettait donc une exécution qui ne pouvait pas avoir lieu.
        #
        # On expose l'état réel, par symbole (la crypto cote 24 h/24, les autres non), pour que
        # l'interface distingue « je surveille et j'ouvrirai » de « j'analyse seulement ».
        tradable, motif = sessions_mod.can_trade_symbol(p.get("symbol", ""), now)
        p["tradable_now"] = bool(tradable) or not get_settings().playbook_trade_only_when_open
        p["market_status"] = motif

    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "session": session,
        # Source unique (`domain.playbook.STRATEGY_SUMMARY`) : cette phrase existait en trois
        # exemplaires, et l'un d'eux décrivait encore la méthode d'avant.
        "strategy": f"Playbook — {playbook.STRATEGY_SUMMARY}",
        "scanned": len(universe),
        "ready": sum(1 for p in picks if p["tier"] == "ready"),
        "armed": sum(1 for p in picks if p["tier"] == "armed"),
        "requested": count or "tous",
        "min_reliability": floor,
        # Transparence sur ce que le plancher de fiabilité a écarté : un filtre qu'on ne compte pas
        # est un filtre qu'on ne peut pas contester.
        "conform": len(conform),
        "below_reliability": below,
        # Setups dont le MARCHÉ EST FERMÉ : analysés, classés, affichés — mais non exécutables tant
        # que leur place n'a pas ouvert. Les compter à part évite l'ambiguïté d'un « 1 armé » qui
        # laisserait croire qu'une position va s'ouvrir alors qu'aucune ne le peut.
        "armed_market_closed": sum(1 for p in picks
                                   if p["tier"] == "armed" and not p.get("tradable_now", True)),
        "picks": picks,
        # Verdict de la stratégie pour CHAQUE symbole balayé, pas seulement pour les 5 retenus.
        # C'est ce qui permet au scanner et aux pages d'analyse de parler exactement le même
        # langage que les trades du jour, sans relancer un seul calcul.
        "verdicts": {r["symbol"]: _verdict_of(r) for r in evaluated},
        "auto_entry": get_settings().playbook_auto_entry_enabled,
        "note": _selection_note(picks, count, session, below=below, floor=floor),
    }


def _verdict_of(row: dict) -> dict:
    """Verdict de la stratégie pour un symbole — métriques ET explication du choix.

    Le scanner s'en sert pour montrer non seulement CE QUE la stratégie décide, mais POURQUOI :
    comment la tendance a été établie, quelles confirmations se sont réunies avec leur poids, ce qui
    a bloqué quand rien ne se passe, et les niveaux du trade. Les couches d'indicateurs complètes et
    la narration rédigée restent hors de cette vue (elles pèsent lourd × 88 symboles) : la page
    détaillée d'un symbole les sert à la demande.
    """
    trend = row.get("trend") or {}
    layers = row.get("layers") or {}
    confirmations = row.get("entry_confirmations") or []
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
        "take_profit_2": row.get("take_profit_2"),
        "risk_pips": row.get("risk_pips"),
        "reward_pips": row.get("reward_pips"),
        "pips_label": row.get("pips_label"),
        "horizon_label": row.get("horizon_label"),
        "edge_score": row.get("edge_score", 0.0),
        "edge": row.get("edge"),
        "pair_verdict": row.get("pair_verdict"),
        "reason": (row.get("reasons") or [""])[0],
        # Métriques de la stratégie, pour que le scanner affiche CE QU'ELLE mesure et rien d'autre.
        "trend_score": trend.get("score_100"),
        "trend_status": trend.get("status"),
        "confirmations": len(confirmations),
        "confirmation_score": round(sum(c.get("contribution", 0.0) for c in confirmations), 2),
        "trigger": row.get("trigger"),
        # --- POURQUOI : de quoi expliquer le choix sans rouvrir une page ------------------------
        "why": {
            # Étape 1 — comment la direction a été établie, et le vote de chaque unité de temps.
            "trend_explanation": row.get("trend_explanation"),
            "trend_reasons": trend.get("reasons") or [],
            "timeframes": [
                {"tf": name, "label": (layers.get(name) or {}).get("label"),
                 "score": (layers.get(name) or {}).get("score"),
                 "bias": (layers.get(name) or {}).get("bias"),
                 "aligned": (layers.get(name) or {}).get("bias") == row.get("bias")
                 and row.get("bias", 0) != 0}
                for name in ("monthly", "daily", "h4", "h1", "m15") if name in layers
            ],
            # Étape 2 — les confirmations réunies, avec leur poids : ce qui a décidé du MOMENT.
            "confirmations": [
                {"key": c.get("key"), "weight": c.get("weight"), "quality": c.get("quality"),
                 "contribution": c.get("contribution"), "strong": c.get("strong"),
                 "reading": c.get("reading")}
                for c in confirmations
            ],
            # Étape 3 — d'où viennent le stop et l'objectif (un niveau, jamais une distance).
            "stop_basis": row.get("stop_basis"),
            "target_basis": row.get("target_basis"),
            "secure_stop": row.get("secure_stop"),
            "tp1_lock_stop": row.get("tp1_lock_stop"),
            "volatility": row.get("volatility"),
            # Ce qui a BLOQUÉ, quand il n'y a pas de trade : la réponse la plus utile du scanner.
            "blocking": row.get("reasons") or [],
            "checklist": [
                {"step": c.get("step"), "label": c.get("label"), "pass": c.get("pass"),
                 "value": c.get("value")}
                for c in (row.get("checklist") or [])
            ],
        },
    }


def _trigger_type(setup: PlaybookSetup) -> str | None:
    """Type de déclencheur (`repli` / `cassure` / `divergence`) extrait du libellé du setup."""
    if not setup.trigger:
        return None
    return setup.trigger.split(" — ", 1)[0].strip() or None


def _selection_note(picks: list[dict], count: int, session: dict, *,
                    below: int = 0, floor: int = 0) -> str:
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
    if below:
        txt += (f". {below} autre(s) setup(s) conforme(s) écarté(s) : fiabilité sous le plancher de "
                f"{floor}/5 — ils restent visibles dans les verdicts par symbole.")
    if count and count > 0 and len(picks) < count:
        txt += (f". Seulement {len(picks)}/{count} demandés : le marché n'en offre pas davantage qui "
                "respecte les trois étapes et la bande de R/R. Compléter la liste reviendrait à "
                "dégrader la stratégie.")
    return txt
