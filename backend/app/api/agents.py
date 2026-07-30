"""Routes de supervision des Agents (Phase 2) — état réel de la couche LLM."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agents import llm
from app.core.config import get_settings
from app.core.deps import current_user, store_dep
from app.models.entities import User
from app.repositories.store import AppStore

router = APIRouter(prefix="/api/agents", tags=["agents"])

_AGENTS = [
    {"name": "playbook", "role": "reasoning",
     "desc": "LA stratégie du desk : tendance multi-indicateurs (D1/4h/1h/15min) → entrée par "
             "confluence pondérée → stop et objectifs posés sur des niveaux · R/R 1:2–1:3 · "
             "sécurisation +2R et TP1→TP2 (droit de veto)"},
    {"name": "technical", "role": "fast", "desc": "Indicateurs (RSI14, MA20/MA50, MACD, VWAP, divergences)"},
    {"name": "volume", "role": "fast", "desc": "Volume relatif, OBV, tendance VWAP"},
    {"name": "sentiment", "role": "fast", "desc": "NLP news + Fear & Greed"},
    {"name": "pattern", "role": "vision", "desc": "Figures chartistes"},
    {"name": "fundamental", "role": "reasoning", "desc": "Ratios financiers (actions)"},
    {"name": "macro", "role": "grounding", "desc": "Régime de marché"},
    {"name": "risk", "role": "deterministic", "desc": "Contrainte de capital (sans LLM)"},
    {"name": "master", "role": "master", "desc": "Arbitrage & pondération dynamique (soumis au veto du playbook)"},
]


@router.get("/status")
async def status(_user: User = Depends(current_user)) -> dict:
    """État des agents et de la couche LLM (modèle routé par rôle)."""
    from app.agents.master import DEFAULT_WEIGHTS
    from app.data import sessions as sessions_mod

    from app.agents import expertise

    llm_on = llm.available()
    s = get_settings()
    agents = [
        {**a, "weight": DEFAULT_WEIGHTS.get(a["name"]),
         "model": (llm.route(a["role"]) if llm_on else None) or "déterministe (fallback)",
         # Ce que l'entraînement de la nuit a mesuré pour cet agent, et la fiche qui en découle.
         "competence": expertise.competence(a["name"]),
         "expertise": expertise.memo(a["name"]) or None}
        for a in _AGENTS
    ]
    return {
        "status": "online",
        "llm_enabled": llm_on,
        "providers": {"anthropic": bool(s.anthropic_api_key), "google": bool(s.google_api_key)},
        "agents": agents,
        "strategy": {
            "name": "Playbook MTF",
            "enabled": s.playbook_enabled,
            "veto": s.playbook_veto,
            "steps": [
                "1 — Tendance : EMA 50/200, structure HH/HL, SuperTrend, MACD, RSI et volume sur "
                "D1, 4 h, 1 h et 15 min. L'accord du 4 h et du 1 h suffit à la valider ; le "
                "journalier pèse 40 % du score sans être obligatoire. Une fois validée, elle est "
                "figée",
                "2 — Entrée (15 min, 1 h en appui) : zones offre/demande, Fibonacci, BOS, CHOCH, "
                "EMA 20/50, VWAP, volume, figures de retournement, RSI, supports/résistances "
                "classés — au moins 3 confirmations pondérées",
                f"3 — Sortie : stop sur le niveau qui invalide le scénario, objectifs devant le "
                f"premier obstacle réel et d'au moins {s.playbook_min_target_pips:.0f} pips "
                f"(l'ATR n'entre pas dans ce calcul), R/R entre 1:2 et 1:3 (bloquant)",
                "4 — Sécurisation : stop sur +2R dès qu'il est parcouru, et à 80 % de TP1 dès que "
                "TP1 est touché avec un momentum qui confirme la suite",
            ],
            "min_risk_reward": s.playbook_min_rr,
            "max_risk_reward": s.playbook_max_rr,
            "min_target_pips": s.playbook_min_target_pips,
            "entry_timeframe": s.playbook_entry_timeframe,
            "confirm_timeframe": s.playbook_confirm_timeframe,
            "trend_timeframes": ["1d", "4h", "1h", "15m"],
            "trend_min_score": s.playbook_trend_min_score,
            "entry_mode": s.playbook_entry_mode,
            "confluence_min_score": s.playbook_confluence_min_score,
            "secure_at_r": s.playbook_secure_at_r,
            "tp1_lock_fraction": s.playbook_tp1_lock_fraction,
            "secure_profit": s.playbook_secure_profit_enabled,
            "trade_only_when_open": s.playbook_trade_only_when_open,
            # 0 = aucun plafond : tous les setups conformes sont proposés, pas les N premiers.
            "daily_trades": s.daily_top_trades_count,
            "min_reliability": s.daily_min_reliability,
            "universe_limit": s.playbook_universe_limit,
            "auto_entry": s.playbook_auto_entry_enabled,
            "auto_entry_mode": "paper",
        },
        "training": _training_summary(),
        "session": sessions_mod.session_context(),
    }


@router.get("/strategy")
async def strategy_spec(_user: User = Depends(current_user)) -> dict:
    """LA STRATÉGIE DU DESK, décrite de A à Z — chaque valeur lue dans la configuration réelle.

    Cette route est la SOURCE UNIQUE de la description : la page ne recopie aucun seuil, elle
    affiche ce que le moteur applique vraiment. Une doc recopiée à la main finit toujours par
    décrire une version de la stratégie qui n'existe plus.

    Le détail est organisé comme le code l'exécute : trois étapes (tendance figée → confluence
    d'entrée → sorties posées sur des niveaux), puis la gestion de la position, le risque, et enfin
    ce que la mesure a tranché.
    """
    from app.domain import entry_confluence as ec
    from app.domain import trend as trend_mod
    from app.services import playbook_service

    s = get_settings()
    tw = trend_mod.parse_weights(s.playbook_trend_weights)
    cw = ec.parse_weights(s.playbook_confluence_weights)
    universe = playbook_service.daily_universe()

    def _pct(weights: dict[str, float], key: str) -> float:
        total = sum(weights.values()) or 1.0
        return round(weights.get(key, 0.0) / total * 100, 1)

    return {
        "name": "Playbook MTF — la stratégie du desk",
        "one_liner": (
            "Une seule méthode, appliquée à tous les marchés : la tendance est mesurée par six "
            "indicateurs sur quatre unités de temps puis FIGÉE ; l'entrée n'est autorisée que "
            "lorsque plusieurs confirmations pondérées se rejoignent ; le stop et les objectifs "
            "sont posés sur des niveaux du graphique, jamais sur une distance choisie d'avance."
        ),
        "enabled": s.playbook_enabled,
        "veto": s.playbook_veto,
        "principles": [
            {
                "title": "La direction se mesure, elle ne se devine pas",
                "body": (
                    "Aucun indicateur ne décide seul, et aucune unité de temps ne décide seule. Six "
                    "indicateurs votent sur quatre unités de temps, et c'est le score agrégé qui "
                    "nomme une direction — ou refuse d'en nommer une."
                ),
            },
            {
                "title": "Une fois la tendance validée, elle est figée",
                "body": (
                    "Ni la recherche du point d'entrée, ni le calcul du stop, ni celui de "
                    "l'objectif ne peuvent la remettre en cause. Sans cette règle, chaque outil "
                    "re-débat de la direction et le système finit par se contredire."
                ),
            },
            {
                "title": "Ce que le prix FAIT pèse le double de ce qui le commente",
                "body": (
                    "Une zone de demande défendue par une avalée haussière vaut plus que trois "
                    "indicateurs dérivés du prix qui sont d'accord entre eux. Les poids de "
                    "l'étape 2 traduisent exactement cela."
                ),
            },
            {
                "title": "Ce qui n'est pas mesurable ne vote pas",
                "body": (
                    "Le forex au comptant n'a pas de volume centralisé : le facteur volume est "
                    "alors RETIRÉ du calcul et les poids restants renormalisés. Une donnée absente "
                    "n'est jamais remplacée par un zéro, qui se lirait comme un avis neutre."
                ),
            },
            {
                "title": "S'abstenir est une décision de trading",
                "body": (
                    "Aucun quota de trades à remplir. Si les trois étapes ne sont pas réunies, il "
                    "n'y a pas de position — et la page le dit au lieu de proposer un second choix."
                ),
            },
        ],
        "steps": [
            {
                "n": 1,
                "title": "Établir la tendance, puis la figer",
                "timeframes": ["1d", "4h", "1h", "15m"],
                "summary": (
                    "Six indicateurs votent sur quatre unités de temps. Le score agrégé doit "
                    f"dépasser ±{s.playbook_trend_min_score:g} pour qu'une direction soit nommée, et "
                    f"les unités de temps exigées ({s.playbook_trend_required_tfs}) doivent dire la "
                    "même chose."
                ),
                "required_timeframes": trend_mod.parse_required_tfs(s.playbook_trend_required_tfs),
                "inputs": [
                    {"key": "ema", "label": "EMA 50 / EMA 200", "weight": tw.get("ema"),
                     "weight_pct": _pct(tw, "ema"), "role": "la tendance principale"},
                    {"key": "structure", "label": "Structure HH/HL — LH/LL", "weight": tw.get("structure"),
                     "weight_pct": _pct(tw, "structure"), "role": "ce que le graphique dit vraiment"},
                    {"key": "supertrend", "label": "SuperTrend", "weight": tw.get("supertrend"),
                     "weight_pct": _pct(tw, "supertrend"), "role": "confirmation, filtre les faux signaux"},
                    {"key": "macd", "label": "MACD", "weight": tw.get("macd"),
                     "weight_pct": _pct(tw, "macd"), "role": "momentum"},
                    {"key": "rsi", "label": "RSI 14", "weight": tw.get("rsi"),
                     "weight_pct": _pct(tw, "rsi"), "role": "force du mouvement — jamais seul, jamais décisif"},
                    {"key": "volume", "label": "Volume", "weight": tw.get("volume"),
                     "weight_pct": _pct(tw, "volume"), "role": "la tendance est-elle soutenue ?"},
                ],
                "timeframe_weights": trend_mod.TF_WEIGHTS,
                "blocking": [
                    "le 4 h et le 1 h doivent être alignés — le 15 min ne bloque PAS l'alignement, "
                    "sans quoi l'entrée sur repli deviendrait impossible ; le JOURNALIER non plus "
                    "depuis le 28/07/2026, mais il porte 40 % du score, donc un journalier "
                    "franchement contraire empêche encore la direction d'atteindre le seuil",
                    "deux piliers qui se contredisent (EMA, structure, SuperTrend) = tendance "
                    "illisible, donc pas de trade",
                    "un mensuel franchement à contresens (score sous −0,30) interdit la tendance",
                ],
                "not_used": (
                    "L'ADX ne décide RIEN. Essayé comme verrou, il a été mesuré comme coupant "
                    "surtout de bons trades : sur le même échantillon, l'écarter fait passer "
                    "l'espérance de +0,12 R à +0,33 R et multiplie le volume par 2,6. Il mesure la "
                    "force d'une tendance déjà installée, or les meilleures entrées se prennent "
                    "quand elle redémarre. Il reste calculé et affiché, à titre informatif."
                ),
            },
            {
                "n": 2,
                "title": "Choisir le moment — confluence pondérée",
                "timeframes": [s.playbook_entry_timeframe, s.playbook_confirm_timeframe],
                "summary": (
                    f"On entre dès que la somme pondérée des confirmations atteint "
                    f"{s.playbook_confluence_min_score:g} points, avec au moins "
                    f"{ec.MIN_CONFIRMATIONS} outils distincts dont au moins "
                    f"{ec.MIN_STRONG_CONFIRMATIONS} confirmation forte. Exiger que TOUS les signaux "
                    "s'alignent ne produirait presque jamais d'opportunité."
                ),
                "mode": s.playbook_entry_mode,
                "mode_explained": {
                    "hybrid": ("déclencheur historique OU confluence. La cassure confirmée reste le "
                               "signal le mieux mesuré du backtest (69 % de réussite, +1,15 R) : on "
                               "ne s'en prive pas, la confluence ajoute des occasions par-dessus."),
                    "confluence": "uniquement la règle du minimum de confirmations",
                    "legacy": "uniquement les déclencheurs historiques — sert de référence à l'A/B",
                }.get(s.playbook_entry_mode, ""),
                "inputs": [
                    {"key": k, "label": label, "weight": cw.get(k),
                     "strong": k in ec.STRONG_KEYS, "role": role}
                    for k, label, role in (
                        ("supply_demand", "Zone d'offre / de demande",
                         "l'endroit d'où les ordres sont réellement partis la dernière fois"),
                        ("structure", "Cassure de structure (BOS) / changement de caractère (CHOCH)",
                         "la reprise de tendance, ou la fin de la correction"),
                        ("price_action", "Figure de retournement",
                         "avalée, marteau, étoile — ce que la bougie raconte au niveau"),
                        ("support_resistance", "Support / résistance classé",
                         "un niveau que le marché a déjà défendu, noté par sa force"),
                        ("fibonacci", "Retracement de Fibonacci",
                         "la zone d'or 0,5–0,618 d'une impulsion de la tendance en cours"),
                        ("rsi", "RSI 14", "évite d'acheter un mouvement déjà épuisé"),
                        ("vwap", "VWAP", "le prix moyen pondéré que suivent les institutionnels"),
                        ("ema_dynamic", "EMA 20 / 50 dynamiques", "le support mobile d'une tendance"),
                        ("volume", "Volume", "le mouvement est-il accompagné ?"),
                    )
                ],
                "blocking": [
                    f"RSI au-delà de {ec.RSI_EXHAUSTED_HIGH:g} à l'achat (ou sous "
                    f"{ec.RSI_EXHAUSTED_LOW:g} à la vente) : épuisement caractérisé, refus absolu",
                    f"RSI au-delà de {ec.RSI_OVERBOUGHT:g} (ou sous {ec.RSI_OVERSOLD:g}) : refusé, "
                    f"sauf tendance exceptionnelle (confiance ≥ {ec.STRONG_TREND_CONFIDENCE:g})",
                    f"un niveau majeur opposé plus proche que {ec.MIN_ROOM_RR:g} fois le risque "
                    "estimé : le potentiel est bouché avant d'avoir payé le risque",
                    ("le déclencheur « divergence » est DÉSACTIVÉ à l'entrée — mesuré à 37,5 % de "
                     "réussite et +0,19 R contre 69 % et +1,15 R pour la cassure. Il reste affiché "
                     "comme avertissement.") if not s.playbook_allow_divergence_entry else
                    "le déclencheur « divergence » est actif (réglage non standard)",
                ],
            },
            {
                "n": 3,
                "title": "Poser les sorties AVANT de passer l'ordre",
                "timeframes": [s.playbook_entry_timeframe, s.playbook_confirm_timeframe, "4h"],
                "summary": (
                    "Le stop n'est jamais une distance, c'est un NIVEAU : celui qui rend le "
                    "scénario faux. L'objectif se pose devant le premier obstacle réel, pas au bout "
                    "d'un calcul — viser au-delà d'une résistance surveillée, c'est parier en plus "
                    "qu'elle sera traversée."
                ),
                "stop_candidates": [
                    "la zone d'offre / de demande d'où l'on entre — si le prix la traverse, les "
                    "ordres attendus n'étaient pas là, l'idée est morte",
                    "le dernier creux plus haut (ou sommet plus bas) — sa perte casse la séquence "
                    "HH/HL qui définissait la tendance",
                    "un support / résistance classé suffisamment solide",
                    "à défaut, la structure 4 h",
                ],
                "stop_rule": (
                    "Le premier candidat dont la distance tombe dans la fourchette autorisée gagne : "
                    "c'est le stop le plus SERRÉ qui ait encore un sens de marché. Une marge de "
                    f"{0.25:g} × ATR 15 min est laissée sous le niveau, sinon la première mèche qui "
                    "vient le tester le déclenche."
                ),
                "blocking": [
                    f"le rapport risque/rendement doit tomber entre 1:{s.playbook_min_rr:g} et "
                    f"1:{s.playbook_max_rr:g} — hors de cette bande, il n'y a pas de trade. C'est "
                    "une condition bloquante, jamais un ajustement",
                    f"objectif d'au moins {s.playbook_min_target_pips:.0f} pips — c'est le SEUL "
                    f"plancher d'objectif, l'ATR journalier n'entre pas dans ce calcul",
                ],
                "informative": [
                    f"stop visé entre {s.playbook_min_stop_atr_daily:g} et "
                    f"{s.playbook_max_stop_atr_daily:g} × l'ATR journalier — affiché, plus "
                    f"bloquant depuis le 28/07/2026",
                    f"objectif ≤ {s.playbook_max_atr_multiple:g} × l'ATR journalier — devenu un "
                    f"indicateur d'HORIZON (combien de journées moyennes le mouvement demande), "
                    f"plus une condition",
                    "marge jusqu'au niveau majeur opposé — affichée ; TP2 reste plafonné par ce "
                    "niveau, donc la position ne vise jamais au-delà de lui",
                    "confirmation du journalier — affichée ; elle réduit la conviction quand elle "
                    "manque, elle ne refuse plus le trade",
                ],
                "scale_explained": (
                    f"Le plancher d'objectif est un nombre de PIPS ({s.playbook_min_target_pips:.0f}), "
                    "et il reste comparable d'un marché à l'autre : hors forex et métaux, 1 pip "
                    "vaut 1 point de base du prix, donc ce plancher représente le même pourcentage "
                    "de mouvement sur un indice que sur une paire de devises. Le plafond de risque "
                    f"({s.playbook_max_stop_atr_daily:g} × ATR journalier) reste appliqué à la POSE "
                    "du stop — c'est lui qui contient le drawdown : l'oublier l'avait fait doubler "
                    "(17,0 R contre 8,9 R) pour une espérance équivalente. Un stop trop large ne "
                    "perd pas plus souvent, il perd beaucoup plus gros."
                ),
            },
            {
                "n": 4,
                "title": "Gérer la position — deux sécurisations qui coexistent",
                "summary": (
                    f"Dès que le trade a parcouru {s.playbook_secure_at_r:g} fois son risque, le "
                    f"stop est remonté SUR ce niveau et n'en redescend plus : la position ne peut "
                    "plus redevenir perdante. Séparément, quand TP1 est touché ET que le momentum "
                    f"confirme, le stop monte à {round(s.playbook_tp1_lock_fraction * 100)} % du "
                    "chemin parcouru et la position part chercher TP2."
                ),
                "rules": [
                    f"sécurisation à +{s.playbook_secure_at_r:g}R : le stop se place exactement sur "
                    f"+{s.playbook_secure_stop_at_r:g}R (pas au point d'entrée)",
                    f"TP1 touché + momentum confirmé : stop à "
                    f"{round(s.playbook_tp1_lock_fraction * 100)} % de TP1, direction TP2",
                    "les deux règles coexistent — la plus favorable au trade s'applique et le stop "
                    "ne recule JAMAIS",
                    "le risque d'origine est figé à l'ouverture : sinon le niveau dérive à chaque "
                    "passage du moniteur",
                ],
                "measured": (
                    "Coût mesuré de la sécurisation +2R : −0,04 R par trade. La règle reste "
                    "appliquée — c'est une mesure, pas un argument pour la retirer."
                ),
            },
        ],
        "risk": {
            "sizing": (
                f"Risque de base issu du profil (1 % en modéré), modulé par le verdict MESURÉ de la "
                f"paire : ×{s.conviction_green_mult:g} sur une paire 🟢 à échantillon solide "
                f"(n ≥ {s.conviction_green_min_trades}), ×{s.conviction_yellow_mult:g} sur une 🟡. "
                f"Plafond absolu {s.conviction_risk_cap_pct:g} % du capital par trade."
            ) if s.conviction_sizing_enabled else "Risque fixe issu du profil.",
            "correlation": (
                f"Au maximum {s.max_positions_per_currency} positions ouvertes partageant une même "
                "devise : EUR/USD et EUR/JPY ne sont pas deux paris indépendants, c'est deux fois "
                "le même pari sur l'euro."
            ) if s.correlation_guard_enabled else "Garde de corrélation désactivée.",
            "freeze": (
                f"Journée à −{s.daily_loss_freeze_pct:g} % ou semaine à −{s.weekly_loss_freeze_pct:g} % "
                "du capital (P&L réalisé) : plus aucune nouvelle entrée jusqu'à la période suivante. "
                "Les positions déjà ouvertes continuent d'être gérées — on arrête d'empiler, pas de gérer."
            ) if s.loss_freeze_enabled else "Gel des entrées sur perte désactivé.",
            "gating": (
                f"L'auto-entrée ne trade QUE les paires 🟢 : espérance ≥ +{s.playbook_verdict_min_expectancy:g} R "
                f"sur n ≥ {s.playbook_verdict_min_trades} trades, confirmée sur "
                f"{s.playbook_verdict_green_streak} passages hebdomadaires consécutifs. Les refus "
                "sont journalisés pour qu'on puisse vérifier après coup s'ils avaient raison."
            ) if s.playbook_pair_gating else "Aucun filtre de verdict sur l'auto-entrée.",
            "volatility": (
                f"Au-delà de {s.playbook_max_atr_pct:g} % d'ATR journalier, le stop est ÉLARGI "
                f"proportionnellement (jusqu'à ×{s.playbook_volatility_max_widen:g}) au lieu de "
                "refuser le trade : on garde l'opportunité, on paie le vrai prix du risque. Motif "
                "mesuré : les trades stoppés ont un ATR journalier 21 % supérieur à celui des "
                "gagnants — c'est le seul facteur qui les distingue nettement."
            ) if s.playbook_volatility_filter and s.playbook_volatility_mode == "adapt" else
            f"Filtre de volatilité : {s.playbook_volatility_mode if s.playbook_volatility_filter else 'désactivé'}.",
        },
        "scope": {
            "markets": [
                m for m in ["Forex", "Métaux précieux", "Indices", "Actions", "Crypto"]
                if not (m == "Métaux précieux" and "commodity" in playbook_service.excluded_classes())
            ],
            "excluded_markets": sorted(playbook_service.excluded_classes()),
            "universe_size": len(universe),
            "universe_capped": bool(s.playbook_universe_limit or s.playbook_watchlist_only),
            "universe_note": (
                f"Balayage restreint à {len(universe)} instruments mesurés rentables "
                f"(watchlist du 28/07/2026) — le backtest complet, lui, continue de balayer tout "
                f"le catalogue pour chercher l'edge ailleurs."
                if s.playbook_watchlist_only else
                "Balayage limité par `playbook_universe_limit`."
                if s.playbook_universe_limit else
                f"Tout le catalogue est balayé ({len(universe)} instruments), à l'exception des "
                f"classes exclues du desk : {s.playbook_excluded_classes or 'aucune'}."
            ),
            "hours": "24 h/24, tous marchés" if not s.playbook_trade_only_when_open
                     else "uniquement quand Londres ou New York est ouverte",
            "entry_timeframe": s.playbook_entry_timeframe,
            "confirm_timeframe": s.playbook_confirm_timeframe,
            "trend_timeframes": ["1d", "4h", "1h", "15m"],
            "proposals_per_day": s.daily_top_trades_count or "aucun plafond",
            "min_reliability": s.daily_min_reliability,
            "auto_entry": s.playbook_auto_entry_enabled,
            "auto_entry_mode": "compte DÉMO uniquement — aucune position réelle sans action humaine",
            "why_all_markets": (
                "La stratégie ne suppose rien de propre à un marché : elle lit une tendance et des "
                "niveaux, ce que tout graphique fournit, et son échelle de trade s'exprime en ATR "
                "journalier — la même règle vaut donc sur EUR/USD et sur le DAX. Élargir l'univers "
                "est aussi le seul levier de volume qui ne coûte rien en qualité : aucune règle "
                "n'est assouplie, chaque trade reste soumis aux trois étapes."
            ),
        },
        "data_honesty": [
            "Aucune donnée fictive en production : quand une source réelle est indisponible, le "
            "connecteur renvoie une série vide et la page affiche « données indisponibles ». Une "
            "donnée inventée présentée comme vraie est pire qu'une page vide.",
            "Aucun trade n'est affirmé sur des bougies synthétiques, et aucun résultat n'est établi "
            "à partir d'un repli de données : mieux vaut un verdict « indéterminé » et une position "
            "laissée ouverte.",
            "Profondeur réellement servie par les fournisseurs gratuits : 15 min ≈ 60 jours, 1 h et "
            "4 h ≈ 730 jours, journalier / hebdomadaire / mensuel ≈ 10 ans. Un backtest de cinq ans "
            "avec entrée 15 min est donc impossible — la passe longue monte toute l'échelle d'un "
            "cran (contexte mensuel + hebdomadaire, entrée journalière) pour mesurer la MÉTHODE sur "
            "cinq ans de données réelles.",
        ],
        "settings": {
            "min_risk_reward": s.playbook_min_rr,
            "max_risk_reward": s.playbook_max_rr,
            "trend_min_score": s.playbook_trend_min_score,
            "confluence_min_score": s.playbook_confluence_min_score,
            "min_confirmations": ec.MIN_CONFIRMATIONS,
            "min_strong_confirmations": ec.MIN_STRONG_CONFIRMATIONS,
            "min_stop_atr_daily": s.playbook_min_stop_atr_daily,
            "max_stop_atr_daily": s.playbook_max_stop_atr_daily,
            "min_target_atr_daily": s.playbook_min_target_atr_daily,
            "secure_at_r": s.playbook_secure_at_r,
            "tp1_lock_fraction": s.playbook_tp1_lock_fraction,
            "entry_mode": s.playbook_entry_mode,
            "allow_divergence": s.playbook_allow_divergence_entry,
        },
    }


@router.post("/auto-trade")
async def set_auto_trade(
    enabled: bool = True,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Active/désactive le FORWARD TEST automatique de la stratégie du desk.

    Chaque signal du playbook ouvre alors un trade PAPIER (risque 1 %, avec ses SL/TP et sa clôture
    automatique) : c'est le juge final de l'edge, des semaines de trades simulés sans intervention.
    """
    store.records.put("auto_trade", user.tenant_id, {"enabled": enabled}, tenant_id=user.tenant_id)
    return {"auto_trade": enabled}


@router.get("/auto-trade")
async def get_auto_trade(
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    return {"auto_trade": bool((store.records.get("auto_trade", user.tenant_id) or {}).get("enabled"))}


def _training_summary() -> dict:
    """Résumé du dernier entraînement quotidien (sans le détail complet, réservé à /training)."""
    from app.services import training_service

    snap = training_service.snapshot()
    if not snap:
        return {"trained": False,
                "note": "Entraînement pas encore passé — les agents utilisent leurs poids de base."}
    return {
        "trained": True,
        "date": snap.get("date"),
        "trades_replayed": snap.get("trades"),
        "symbols": snap.get("symbols_trained"),
        "overall": snap.get("overall"),
        "agent_multipliers": snap.get("agent_multipliers"),
        "duration_s": snap.get("duration_s"),
    }


@router.get("/training")
async def training(_user: User = Depends(current_user)) -> dict:
    """Détail de l'ENTRAÎNEMENT QUOTIDIEN des agents sur la stratégie du desk.

    Contient le walk-forward complet : réussite mesurée par symbole, par type de déclencheur et par
    fenêtre de session, justesse de chaque facteur, multiplicateurs de poids qui en découlent, et
    les fiches d'expertise du jour.
    """
    from app.services import training_service

    snap = training_service.snapshot()
    if not snap:
        return {"trained": False, "note": "Aucun entraînement disponible pour l'instant."}
    return {"trained": True, **snap}


@router.post("/training/run")
async def training_run(
    _user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> dict:
    """Lance un entraînement immédiat (walk-forward complet + fiches). Opération longue."""
    from app.services import training_service

    return await training_service.run_training(store)
