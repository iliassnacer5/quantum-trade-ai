"""Routes de supervision des Agents (Phase 2) — état réel de la couche LLM."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.agents import llm
from app.core.config import get_settings
from app.core.deps import current_user, store_dep
from app.models.entities import User
from app.repositories.store import AppStore

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentModelRequest(BaseModel):
    role: str
    model: str


_ROLE_MODEL_FIELDS = {
    "master": "llm_model_master",
    "reasoning": "llm_model_reasoning",
    "fast": "llm_model_fast",
    "vision": "llm_model_vision",
    "grounding": "llm_model_grounding",
}

# Correspondance entre le nom de chargement d'une unité de temps (`playbook_service._TIMEFRAMES`)
# et son étiquette dans le moteur (`playbook.MIN_CANDLES`). Les deux vocabulaires coexistent depuis
# l'origine ; les relier ici évite de recopier des minima de bougies qui finiraient par diverger.
_LAYER_NAMES = {"monthly": "mensuel", "daily": "journalier", "h4": "4h", "h1": "1h", "m15": "15m"}

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
    role_models = {
        role: (llm.route(role) if llm_on else None) or "déterministe (fallback)"
        for role in _ROLE_MODEL_FIELDS
    }
    available_models = list(dict.fromkeys([
        getattr(s, field)
        for field in _ROLE_MODEL_FIELDS.values()
    ] + [s.litellm_default_model]))
    agents = [
        {**a, "weight": DEFAULT_WEIGHTS.get(a["name"]),
         "model": role_models.get(a["role"], "déterministe (fallback)"),
         # Ce que l'entraînement de la nuit a mesuré pour cet agent, et la fiche qui en découle.
         "competence": expertise.competence(a["name"]),
         "expertise": expertise.memo(a["name"]) or None}
        for a in _AGENTS
    ]
    return {
        "status": "online",
        "llm_enabled": llm_on,
        "providers": {"anthropic": bool(s.anthropic_api_key), "google": bool(s.google_api_key)},
        "available_models": available_models,
        "role_models": role_models,
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


@router.post("/model")
async def set_model(body: AgentModelRequest, _user: User = Depends(current_user)) -> dict:
    """Sélectionne le modèle LLM ciblé pour un rôle donné à l'exécution courante."""
    field = _ROLE_MODEL_FIELDS.get(body.role)
    if field is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Rôle inconnu: {body.role}")

    settings = get_settings()
    setattr(settings, field, body.model)
    return {"role": body.role, "model": body.model}


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
    from app.domain import exits as exits_mod
    from app.domain import market_structure as ms_mod
    from app.domain import playbook as pb
    from app.domain import price_action as pa_mod
    from app.domain import trend as trend_mod
    from app.domain import zones as zones_mod
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
                # LA RÈGLE EXACTE DE CHAQUE INDICATEUR, telle que `trend.trend_vote_layer` la
                # calcule. Sans elle, « EMA 28 % » ne dit pas ce que l'EMA doit VOIR pour voter +1.
                "detail": [
                    {"label": "EMA 50 / EMA 200 — comment le vote est calculé",
                     "body": (
                         "prix > EMA50 > EMA200 = +1,00 (empilement haussier complet) · prix < EMA50 "
                         "< EMA200 = −1,00 · EMA50 au-dessus de EMA200 mais prix coincé entre les "
                         "deux = ±0,30 seulement · EMA50 et EMA200 confondues = 0. Un CROISEMENT "
                         "survenu dans les 10 dernières bougies prime sur l'empilement : le vote "
                         "devient 0,5 × vote + 0,5 × (0,6 × sens du croisement), parce que "
                         "l'empilement met du temps à se reformer après un retournement."
                     )},
                    {"label": "Structure HH/HL — comment elle est lue",
                     "body": (
                         "Chaque sommet est comparé au précédent (HH s'il est plus haut, LH sinon), "
                         "chaque creux au précédent (HL / LL). Deux pivots distants de moins de "
                         f"{ms_mod.EQUAL_TOLERANCE_ATR:g} × ATR sont étiquetés « EQ » : ni plus haut "
                         "ni plus bas, c'est un double sommet, donc un range. L'état se lit sur les "
                         "QUATRE derniers labels — « haussière » exige au moins un HH ET un HL sans "
                         "aucun LL ; l'inverse pour « baissière ». Vote +1 / −1, et rien du tout si "
                         "la structure est indéterminée (elle ne vote pas au hasard). Un pivot doit "
                         f"dominer {ms_mod.DEFAULT_STRENGTH} bougies de chaque côté pour exister."
                     )},
                    {"label": "SuperTrend (10 périodes, facteur 3) ",
                     "body": (
                         "Vote ±1 selon le côté de la bande. Une bascule survenue il y a moins de "
                         f"{trend_mod.FRESH_FLIP_BARS} bougies n'a pas encore fait ses preuves : son "
                         "vote est divisé par deux."
                     )},
                    {"label": "MACD — momentum",
                     "body": (
                         "Signe de l'histogramme = sens du vote. S'il se RENFORCE (|hist| ≥ |hist "
                         "précédent|) le vote vaut ±1 ; s'il s'essouffle, il est divisé par deux. Le "
                         "MACD ne peut jamais poser de veto à lui seul."
                     )},
                    {"label": "RSI 14 — force, jamais décision",
                     "body": (
                         "Au-dessus de 55 = +0,80 · sous 45 = −0,80 · entre les deux, vote "
                         "proportionnel (RSI − 50) / 12,5. Au-delà de 70 le vote RETOMBE à +0,20 (et "
                         "à −0,20 sous 30) : la direction est bonne mais le mouvement est étiré, donc "
                         "l'entrée serait tardive. Une divergence à contresens ne l'inverse pas, elle "
                         "le PLAFONNE (× 0,25) — le RSI ne décide jamais seul d'un retournement."
                     )},
                    {"label": "Volume — et ce qui se passe quand il n'existe pas",
                     "body": (
                         "Vote = pente de l'OBV sur 10 bougies, ramenée dans [−1, +1] (pente / 5). Un "
                         "volume relatif sous 0,8 × la moyenne 20 divise ce vote par deux "
                         "(participation faible). Sur le forex au comptant, il n'y a pas de volume "
                         "centralisé : le facteur est alors RETIRÉ du calcul et les poids des cinq "
                         "autres sont renormalisés — une donnée absente n'est jamais convertie en "
                         "zéro, qui se lirait comme un avis neutre."
                     )},
                    {"label": "Agrégation et seuils",
                     "body": (
                         "Score d'une unité de temps = somme (vote × poids) ÷ somme des poids "
                         "réellement calculables. Score global S = "
                         + " + ".join(f"{w:g} × {k}" for k, w in trend_mod.TF_WEIGHTS.items())
                         + f". Une direction n'est nommée que si |S| ≥ {s.playbook_trend_min_score:g}. "
                         f"La CONFIANCE affichée vaut exactement |S| (× 100) : c'est la netteté de "
                         "l'accord entre unités de temps, rien d'autre — elle ne dépend plus de "
                         "l'ADX depuis que la mesure a montré que le pondérer par la force du "
                         "mouvement dégradait la sélection."
                     )},
                    {"label": "Ce qui rend la tendance ILLISIBLE (conflit)",
                     "body": (
                         "Un conflit ne se juge que sur les PILIERS de la direction — "
                         + ", ".join(trend_mod.CONFLICT_KEYS)
                         + f" : un pilier qui vote à contresens du résultat de sa couche avec une "
                         f"intensité ≥ {trend_mod.CONFLICT_SIGNAL:g} rend la tendance illisible et il "
                         "n'y a pas de trade. Le MACD et le RSI en sont exclus délibérément : ils "
                         "mesurent un momentum court terme qui respire à contretemps dans toute "
                         "tendance en escalier, et les compter comme conflit bloquerait la "
                         "quasi-totalité des tendances saines."
                     )},
                    {"label": "Historique exigé, sinon aucune conclusion",
                     "body": (
                         "Chaque unité de temps a besoin d'un minimum de bougies exploitables ("
                         + " · ".join(f"{k} {v}" for k, v in pb.MIN_CANDLES.items())
                         + "). S'il en manque, le statut est « insufficient » et AUCUNE direction "
                         "n'est déduite — on préfère ne rien conclure qu'extrapoler."
                     )},
                ],
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
                # CE QUE CHAQUE OUTIL DOIT VOIR pour compter, et la QUALITÉ qu'il rend. La
                # contribution d'une confirmation vaut « poids × qualité » : sans le détail de la
                # qualité, le poids seul ne dit pas ce qui a réellement fait le score.
                "detail": [
                    {"label": "La bougie de réaction, condition préalable de presque tout",
                     "body": (
                         "La dernière bougie 15 min doit s'être retournée DANS notre sens (clôture > "
                         "ouverture à l'achat). Zone d'offre/demande, support/résistance et EMA "
                         "dynamique l'exigent toutes : sans elle, on attrape un couteau qui tombe. "
                         "Une zone simplement touchée sans réaction est journalisée comme « on "
                         "attend » — elle n'a rien prouvé."
                     )},
                    {"label": "Zone d'offre / de demande (poids fort)",
                     "body": (
                         "Le prix doit être DANS une zone alignée avec le trade, ou l'avoir touchée "
                         "sur les 3 dernières bougies, avec une force ≥ 0,50 et une bougie de "
                         "réaction. La QUALITÉ retenue est la force de la zone elle-même."
                     )},
                    {"label": "Cassure de structure (BOS) ou changement de caractère (CHOCH)",
                     "body": (
                         "Un BOS confirmé compte pour une qualité de 1,00 ; à défaut, un CHOCH dans "
                         "notre sens vaut 0,70 (la correction est finie, mais la reprise n'est pas "
                         "encore prouvée). Dans les deux cas, la cassure doit être une CLÔTURE au-delà "
                         "du pivot — une mèche qui dépasse puis rentre est un piège, pas une cassure — "
                         "et dater de moins de 10 bougies."
                     )},
                    {"label": "Figure de retournement (price action)",
                     "body": (
                         "Seules ces figures sont acceptées comme confirmation d'entrée : "
                         + ", ".join(sorted(pa_mod.ENTRY_PATTERNS))
                         + ". La qualité est la netteté de la figure (0,7 pour une étoile du matin, "
                         "0,6 pour une avalée, 0,3 pour une inside bar qui ne fait que comprimer). "
                         "Un doji ou une « structure sur trois bougies » ne suffisent pas à engager "
                         "du capital et sont volontairement exclus."
                     )},
                    {"label": "Support / résistance classé",
                     "body": (
                         "Le niveau doit être du bon côté, à moins d'un ATR 15 min du prix, avec une "
                         "bougie de réaction. La qualité est la NOTE du niveau (voir « Comment le "
                         "graphique est lu » plus bas) : elle intègre déjà l'unité de temps, le "
                         "nombre de touches et la récence."
                     )},
                    {"label": "Fibonacci",
                     "body": (
                         "Correction dans la zone d'or 38,2–61,8 % d'une impulsion allant dans le "
                         "sens de la tendance, et non invalidée. La qualité est maximale au MILIEU de "
                         "la zone (50 %) et décroît vers les bords : 1 − |profondeur − 50| / 30, "
                         "plancher 0,60."
                     )},
                    {"label": "RSI, VWAP, EMA dynamiques, volume (poids faibles)",
                     "body": (
                         "RSI : sortie d'une zone extrême (35 / 65 franchi) ou divergence en notre "
                         "faveur = 0,80 ; simple retournement = 0,50. VWAP : prix du bon côté = 0,60, "
                         "porté à 1,00 en séance de forte activité. EMA 20/50 : la mèche des 3 "
                         "dernières bougies vient à moins de 0,6 × ATR de la moyenne, avec réaction = "
                         "0,70. Volume : essoufflement pendant la correction (< 0,9×) PUIS reprise "
                         "(≥ 1,2×) = 1,00, la signature d'une vraie relance ; un simple volume "
                         "supérieur à la normale = 0,60."
                     )},
                    {"label": "Les déclencheurs historiques, conservés en mode « hybrid »",
                     "body": (
                         "REPLI : contact d'une MA20/MA50 (à 0,6 × ATR près) ou de la zone d'or "
                         "Fibonacci, + bougie de reprise + RSI qui se retourne — mesuré à 58 % de "
                         "réussite et +0,77 R. CASSURE : franchissement du dernier swing confirmé par "
                         "le volume (≥ 1,2×) et le VWAP — 69 % et +1,15 R, le meilleur signal du "
                         "backtest. DIVERGENCE : RSI/MACD + bougie de reprise — 37,5 % et +0,19 R, "
                         "d'où sa désactivation. Le déclencheur retenu est journalisé tel quel, si "
                         "bien que la matrice paire × déclencheur mesure la confluence comme un "
                         "déclencheur à part entière, comparable au repli et à la cassure."
                     )},
                    {"label": "Le calcul de la décision",
                     "body": (
                         "Score = Σ (poids × qualité) sur les confirmations obtenues. Trois "
                         "conditions doivent tomber ensemble : au moins "
                         f"{ec.MIN_CONFIRMATIONS} outils DISTINCTS, dont au moins "
                         f"{ec.MIN_STRONG_CONFIRMATIONS} confirmation forte "
                         f"({', '.join(ec.STRONG_KEYS)}), et un score ≥ "
                         f"{s.playbook_confluence_min_score:g}. Le motif du refus nomme toujours "
                         "laquelle des trois a manqué, avec le détail chiffré des confirmations "
                         "obtenues."
                     )},
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
                    "Le stop ne va pas au niveau le plus PROCHE mais au plus SOLIDE : chaque "
                    "candidat reçoit une fiabilité = poids du type de niveau × solidité MESURÉE sur "
                    "le graphique (note du S/R, force de la zone, nombre de pivots alignés). Seuls "
                    "les candidats dont la distance tombe dans la bande de risque sont éligibles ; "
                    "le plus fiable gagne, et la distance ne sert qu'à départager deux niveaux "
                    "aussi solides (le plus serré l'emporte alors, il donne le meilleur R/R). Une "
                    f"marge de {exits_mod.LEVEL_BUFFER_ATR:g} × ATR 15 min est laissée DERRIÈRE le "
                    "niveau, sinon la première mèche qui vient le tester le déclenche."
                ),
                "stop_weights": [
                    {"label": "Support / résistance classé", "weight": exits_mod.LEVEL_WEIGHT,
                     "why": "sa note intègre déjà les touches et la confirmation multi-unités de temps"},
                    {"label": "Au-delà d'un pool de liquidité", "weight": exits_mod.LIQUIDITY_WEIGHT,
                     "why": "le seul placement qui protège d'un balayage des stops"},
                    {"label": "Zone d'offre / de demande d'entrée", "weight": exits_mod.ZONE_WEIGHT,
                     "why": "sa traversée prouve que les ordres attendus n'étaient pas là"},
                    {"label": "Niveau cassé par le BOS / CHOCH", "weight": exits_mod.BREAK_WEIGHT,
                     "why": "on est entré parce qu'il a cédé ; pondéré par la fraîcheur de la cassure"},
                    {"label": "Dernier creux plus haut (ou sommet plus bas)", "weight": exits_mod.PIVOT_WEIGHT,
                     "why": "solide, mais un seul point de contact"},
                    {"label": "Malus « stop exposé à un balayage »",
                     "weight": exits_mod.SWEEP_PENALTY,
                     "why": ("un stop placé ENTRE le prix et un pool de liquidité sera le premier "
                             "servi quand le marché ira chercher cet amas : sa fiabilité est "
                             "multipliée par ce coefficient plutôt qu'interdite — mieux vaut un stop "
                             "exposé que pas de trade")},
                ],
                "detail": [
                    {"label": "La bande de risque, calculée avant toute chose",
                     "body": (
                         "Le stop doit tomber entre un PLANCHER et un PLAFOND, tous deux calculés sur "
                         "le symbole du moment. Plancher = le plus grand de : "
                         f"{pb.MIN_STOP_ATR15:g} × ATR 15 min (le bruit du graphique d'entrée), "
                         f"{s.playbook_min_stop_atr_daily:g} × ATR journalier, et « de quoi rendre le "
                         "plancher d'objectif atteignable au R/R maximum ». Plafond = le plus petit "
                         f"de : {s.playbook_max_stop_pips:.0f} pips (garde-fou absolu, forex et "
                         f"métaux) et {s.playbook_max_stop_atr_daily:g} × ATR journalier. C'est le "
                         "PLAFOND qui contient le drawdown, et il est appliqué à la pose du stop, pas "
                         "après coup."
                     )},
                    {"label": "Le repli, quand aucun niveau ne tient dans la bande",
                     "body": (
                         "On retombe sur la structure 4 h. Si elle est elle-même au-delà du plafond, "
                         "le stop est ramené SUR le plafond plutôt que le trade refusé — un stop "
                         "plafonné reste un stop de taille saine, alors qu'un refus perd "
                         "l'opportunité pour une raison de forme. En tout dernier recours, le stop "
                         "est posé à la distance minimale et le motif l'écrit franchement : "
                         "« distance minimale (aucun niveau exploitable) »."
                     )},
                    {"label": "Comment TP1 et TP2 sont choisis",
                     "body": (
                         "Tous les obstacles situés DEVANT nous sont listés — résistances/supports "
                         "classés, bord PROCHE des zones opposées, extensions de Fibonacci, dernier "
                         "sommet ou creux de structure, pools de liquidité — puis triés du plus "
                         "proche au plus lointain. TP1 est le PREMIER qui paie au moins le R/R "
                         f"minimum (1:{s.playbook_min_rr:g}) sans dépasser le maximum "
                         f"(1:{s.playbook_max_rr:g}) : le plus atteignable de ceux qui valent le "
                         "risque. TP2 est le dernier candidat de la bande, plafonné par le niveau "
                         "majeur opposé. Quand TP2 se confond avec TP1, il n'est PAS affiché — mieux "
                         "vaut aucun second objectif qu'un doublon inventé."
                     )},
                    {"label": "Les pools de liquidité, à l'endroit et à l'envers",
                     "body": (
                         "Les sommets et creux ÉGAUX (au moins "
                         f"{ms_mod.MIN_POOL_TOUCHES} pivots alignés à "
                         f"{ms_mod.EQUAL_TOLERANCE_ATR:g} × ATR près) sont les endroits où reposent "
                         "les stops de tout le monde. DEVANT nous, c'est un aimant : une destination "
                         "probable, donc un bon objectif — visé JUSTE AVANT l'amas, pas dedans. "
                         "DERRIÈRE nous, c'est un piège : le stop doit passer AU-DELÀ du pool, jamais "
                         "entre le prix et lui."
                     )},
                    {"label": "Ce qu'aucun objectif ne fait",
                     "body": (
                         "Aucun objectif n'est posé « à N pips » : le plancher en pips vaut "
                         f"{s.playbook_min_target_pips:.0f} et l'ATR ne participe pas au calcul. Le "
                         "seul encadrement est le rapport risque/rendement. Quand vraiment aucun "
                         "niveau ne tombe dans la bande, TP1 est posé arithmétiquement au R/R minimum "
                         "et le motif le DIT, au lieu d'inventer un niveau."
                     )},
                ],
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
                "detail": [
                    {"label": "Ce que « le momentum confirme » veut dire exactement",
                     "body": (
                         "Trois questions posées sur le 15 min, toutes nécessaires : le RSI n'est pas "
                         "épuisé dans notre sens (≤ 70 à l'achat, ≥ 30 à la vente), l'histogramme "
                         "MACD pousse encore dans notre sens, et aucun changement de caractère "
                         "(CHOCH) à contresens n'est apparu sur les 6 dernières bougies. Si l'une "
                         "manque — ou si l'historique ne suffit pas à en juger — on ne prolonge PAS "
                         "le risque : le stop est amené sur TP1, ce qui revient à encaisser le gain "
                         "au prochain retour du prix."
                     )},
                    {"label": "Le garde-fou qui évite un stop posé sur l'objectif",
                     "body": (
                         f"La sécurisation à +{s.playbook_secure_at_r:g}R suppose que l'objectif soit "
                         "AU-DELÀ de ce niveau. À un R/R de 1:2 exactement, l'objectif EST +2R : "
                         "déplacer le stop dessus ne protégerait rien et produirait un panier d'ordres "
                         "incohérent (stop au-dessus de l'objectif pour un achat). Ce déplacement-là "
                         "est donc refusé, à un millionième de R près. Les trades à R/R > 2 sont "
                         "strictement inchangés."
                     )},
                    {"label": "Comment la position est surveillée",
                     "body": (
                         f"Toutes les {s.position_monitor_interval} secondes, chaque position ouverte "
                         "est relue avec un prix FRAIS (jamais un cache) : d'abord la sécurisation "
                         "+2R, puis la gestion TP1 → TP2, et seulement ensuite la vérification des "
                         "clôtures. Cet ordre est délibéré — un trade qui atteint +2R doit voir son "
                         "stop remonté au MÊME passage, sinon un repli dans la même minute le rendrait "
                         "perdant."
                     )},
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
        # --- CE QUI TOURNE EN PERMANENCE, et sur quelles données ------------------------------
        # La stratégie n'est pas une page qu'on rafraîchit : c'est une boucle. Sans ces cadences,
        # « pourquoi ce trade s'est-il ouvert à 3 h du matin ? » n'a pas de réponse lisible.
        "pipeline": {
            "loops": [
                {"name": "Balayage complet de l'univers",
                 "cadence": f"toutes les {s.playbook_snapshot_interval} s",
                 "body": (
                     f"La stratégie est rejouée intégralement sur les {len(universe)} instruments du "
                     f"balayage, {s.playbook_max_parallel} symboles en parallèle. Le résultat est "
                     f"l'instantané qui alimente les pages et l'auto-entrée ; passé "
                     f"{s.playbook_snapshot_max_age} s, il est signalé comme PÉRIMÉ au lieu d'être "
                     "servi comme frais."
                 )},
                {"name": "Veille d'auto-entrée",
                 "cadence": f"toutes les {s.playbook_auto_entry_interval} s",
                 "body": (
                     "Reprend les setups ARMÉS et RECALCULE la stratégie pour chacun sur les données "
                     "du moment — jamais sur les niveaux d'un instantané. Dès qu'un déclencheur 15 "
                     "min est actif, la position part en compte démo. Si l'instantané est absent ou "
                     "périmé, il est recalculé plutôt que de conclure « aucun setup » : c'est ce "
                     "silence-là qui faisait cesser le robot de trader sans rien signaler."
                 )},
                {"name": "Surveillance des positions",
                 "cadence": f"toutes les {s.position_monitor_interval} s",
                 "body": (
                     "Sécurisation +2R, puis gestion TP1 → TP2, puis clôture automatique dès qu'un "
                     "SL ou un TP est atteint. Une passe de quarantaine sur 10 neutralise les "
                     "clôtures que le marché n'a pas pu produire, pour qu'un P&L inventé ne "
                     "contamine ni le portefeuille ni l'apprentissage."
                 )},
                {"name": "Entraînement nocturne",
                 "cadence": f"chaque nuit à {s.playbook_training_hour} h UTC",
                 "body": (
                     f"Walk-forward complet sur ~{s.playbook_training_bars} bougies : réussite "
                     "mesurée par symbole, par type de déclencheur et par fenêtre de session, "
                     "justesse de chaque facteur. C'est de là que sort la « fiabilité mesurée » qui "
                     "classe les trades du jour — de l'historique, pas une opinion. Une statistique "
                     f"n'est crue qu'au-delà de {s.playbook_training_min_trades} trades."
                 )},
                {"name": "Backtest hebdomadaire",
                 "cadence": (f"chaque semaine (jour {s.playbook_backtest_weekday}, "
                             f"{s.playbook_backtest_hour} h UTC)"),
                 "body": (
                     "Rejoue la stratégie sur tout le catalogue — pas seulement sur le balayage en "
                     "ligne — pour chercher l'edge ailleurs, et produit le VERDICT 🟢/🟡/🔴 de chaque "
                     "paire. Une passe longue monte toute l'échelle d'un cran (contexte mensuel + "
                     "hebdomadaire, entrée journalière) pour mesurer la MÉTHODE sur cinq ans de "
                     "données réelles, là où une entrée 15 min ne remonte qu'à ~60 jours."
                 )},
            ],
            "timeframes": [
                {"tf": interval,
                 "role": {
                     "1M": "biais de fond — peut opposer son veto, ne vote pas dans le score",
                     "1d": "40 % du score de tendance",
                     "4h": "30 % du score — accord EXIGÉ",
                     "1h": "20 % du score — accord EXIGÉ, et dernière confirmation avant l'entrée",
                     "15m": "10 % du score — la SEULE unité de temps d'entrée, elle donne le timing",
                 }.get(interval, ""),
                 "candles": limit,
                 "cache_s": playbook_service._TTL.get(interval, 300),
                 "min_candles": pb.MIN_CANDLES.get(_LAYER_NAMES.get(name, name), 60)}
                for name, interval, limit in playbook_service._TIMEFRAMES
            ],
            "notes": [
                "Les cinq unités de temps sont chargées EN PARALLÈLE, avec un cache par unité : "
                "une bougie mensuelle ne change pas plus vite qu'une bougie mensuelle. Le 15 min a "
                "le cache le plus court, parce que c'est lui qui déclenche.",
                "Le calcul est déporté hors de la boucle réseau : cinq couches d'indicateurs sur des "
                "centaines de bougies, multipliées par des dizaines de symboles, bloquaient l'API et "
                "faisaient tomber les flux temps réel.",
                "Sur données non réelles (repli de démonstration), AUCUN trade n'est affirmé et le "
                "playbook n'oppose pas non plus son veto aux autres agents : il se déclare "
                "« insuffisant » plutôt que d'avoir un avis sur des bougies inventées.",
            ],
        },
        # --- LA CHECKLIST TELLE QUE LE MOTEUR LA REMPLIT --------------------------------------
        # Onze cases, qui n'ont PAS le même pouvoir. Sans cette distinction, un ❌ informatif se
        # lirait comme un refus de trade.
        "checklist": [
            {"n": 1, "step": 1, "label": "Tendance multi-indicateurs validée",
             "blocking": True,
             "why": "sans direction nommée, il n'y a rien à trader"},
            {"n": 2, "step": 1, "label": "Supports / résistances majeurs fixés",
             "blocking": True,
             "why": "ce sont eux qui bornent l'objectif ; sans repère majeur, la cible est aveugle"},
            {"n": 3, "step": 2, "label": "Le journalier confirme",
             "blocking": bool(s.playbook_require_daily_confirmation),
             "why": ("il pèse 40 % du score de tendance mais ne pose plus de veto à lui seul "
                     "(28/07/2026) — un ❌ réduit la conviction, il ne refuse pas l'entrée")},
            {"n": 4, "step": 3, "label": "Le 4 h confirme",
             "blocking": True, "why": "l'une des deux unités de temps dont l'accord est exigé"},
            {"n": 5, "step": 4, "label": "Le 1 h confirme",
             "blocking": True,
             "why": ("dernière unité consultée avant l'entrée : elle filtre les cas où le 4 h est "
                     "encore orienté dans notre sens alors que le mouvement s'est déjà retourné "
                     "en dessous")},
            {"n": 6, "step": 5, "label": "Déclencheur d'entrée en 15 min",
             "blocking": True, "why": "sans déclencheur, le setup est ARMÉ, pas exécutable"},
            {"n": 7, "step": 6, "label": "Stop structurel cohérent",
             "blocking": bool(s.playbook_block_on_stop_width),
             "why": ("le R/R reste calculé sur le stop RÉEL, et c'est la taille de position — "
                     "dimensionnée sur la distance au stop — qui absorbe l'écart")},
            {"n": 8, "step": 7,
             "label": f"Risque / rendement dans la bande 1:{s.playbook_min_rr:g}–1:{s.playbook_max_rr:g}",
             "blocking": True, "why": "la condition la plus dure de la méthode, jamais un ajustement"},
            {"n": 9, "step": 7, "label": "Objectif posé sur un niveau du marché",
             "blocking": True,
             "why": "un objectif plus court que le plancher configuré n'est pas pris"},
            {"n": 10, "step": 8, "label": "Objectif atteignable avant le niveau majeur opposé",
             "blocking": bool(s.playbook_block_on_major_level),
             "why": ("TP2 reste plafonné par ce niveau, donc la position ne vise jamais AU-DELÀ de "
                     "lui : la case signale un objectif ambitieux, pas un setup invalide")},
            {"n": 11, "step": 8, "label": "Objectif compatible avec la volatilité",
             "blocking": bool(s.playbook_block_on_atr_reach),
             "why": ("devenue un indicateur d'HORIZON — combien de journées moyennes le mouvement "
                     "demande — depuis que l'ATR est sorti du calcul de l'objectif")},
            {"n": 12, "step": 9, "label": "Fenêtre de session favorable",
             "blocking": False,
             "why": ("hors des créneaux liquides la CONVICTION est réduite (elle multiplie le score "
                     "final), mais le setup reste valable")},
        ],
        "checklist_note": (
            "Une case supplémentaire apparaît quand la restriction horaire est activée "
            f"({'activée' if s.playbook_trade_only_when_open else 'désactivée'} actuellement) : "
            "« marché ouvert à l'ouverture de position », qui bloque alors l'entrée hors des heures "
            "de Londres et de New York. L'ANALYSE, elle, tourne toujours — c'est ainsi qu'on arrive "
            "préparé à l'ouverture."
        ),
        # --- COMMENT LE GRAPHIQUE EST LU ------------------------------------------------------
        # Les mêmes quatre outils servent aux trois étapes. Les décrire une fois évite de laisser
        # croire que « zone de demande » veut dire quelque chose de différent à l'entrée et au stop.
        "toolbox": [
            {"name": "Zones d'offre et de demande",
             "purpose": "d'où les ordres sont réellement partis la dernière fois",
             "body": (
                 "Signature recherchée : une bougie de BASE (corps < 50 % de son amplitude — le "
                 "marché piétine) immédiatement suivie d'une IMPULSION d'au moins 1,5 × ATR sur les "
                 "trois bougies suivantes. Le départ brutal est la preuve qu'il restait des ordres à "
                 "ce prix ; sans lui, ce n'est qu'une pause sans intérêt. La force ∈ [0,1] combine "
                 "l'ampleur de l'impulsion et l'USURE : chaque retour du prix consomme des ordres, "
                 "donc une zone déjà retestée trois fois vaut moins qu'une zone vierge. Calculées "
                 "sur le 15 min ET le 1 h."
             )},
            {"name": "Supports / résistances classés",
             "purpose": "tous les niveaux ne se valent pas, et la stratégie doit pouvoir en juger",
             "body": (
                 "Les pivots du 15 min, du 1 h et du 4 h sont REGROUPÉS (un même prix vu sur deux "
                 "unités est UN niveau, pas deux), puis notés : note = poids d'unité de temps × "
                 "poids de touches × poids de récence. Poids d'unité : "
                 + " · ".join(f"{k} {v:g}" for k, v in zones_mod.TIMEFRAME_WEIGHT.items())
                 + ". Poids de touches : min(1 ; 0,4 + 0,2 × nombre de touches) — et une « touche » "
                 "est une VISITE, pas une bougie : dix bougies collées au niveau pendant une "
                 "consolidation comptent pour une seule, sinon un range trivial vaudrait un support "
                 "majeur. Poids de récence : 1,0 jusqu'à 50 bougies, puis décroissance jusqu'à 0,5 à "
                 "250 bougies. Un niveau ne peut porter un stop qu'au-dessus d'une note de "
                 f"{exits_mod.MIN_LEVEL_SCORE:g}."
             )},
            {"name": "Structure de marché (HH/HL, BOS, CHOCH)",
             "purpose": "ce que le graphique dit, avant tout indicateur",
             "body": (
                 "BOS (cassure de structure) : le dernier pivot formé PENDANT la correction a été "
                 "CLÔTURÉ au-delà — la clôture est exigée, une mèche qui dépasse puis rentre est un "
                 "piège. CHOCH (changement de caractère) : la micro-tendance à contresens vient "
                 "d'être cassée, c'est-à-dire que la correction est terminée. Les deux sont bornés à "
                 "10 bougies : une cassure vieille de vingt bougies n'est plus un déclencheur, le "
                 "mouvement est parti sans nous."
             )},
            {"name": "Pools de liquidité (sommets et creux égaux)",
             "purpose": "là où reposent les stops de tout le monde",
             "body": (
                 f"Au moins {ms_mod.MIN_POOL_TOUCHES} pivots alignés à "
                 f"{ms_mod.EQUAL_TOLERANCE_ATR:g} × ATR près, cherchés sur le 15 min ET le 1 h (un "
                 "amas visible sur deux unités de temps est bien mieux défendu). Le prix retenu est "
                 "l'EXTRÊME du groupe et non sa moyenne : c'est lui qu'il faut franchir pour que la "
                 "liquidité soit réellement prise."
             )},
            {"name": "Niveaux MAJEURS (mensuel + journalier)",
             "purpose": "ceux que tout le marché surveille",
             "body": (
                 "Sommets et creux de swing MENSUELS, plus les extrêmes journaliers de référence "
                 "(120 jours ≈ 6 mois et 20 jours ≈ 1 mois). On ne retient délibérément PAS chaque "
                 "micro-swing journalier : un niveau que personne ne surveille ne borne rien, et "
                 "les empiler ici bloquerait tous les objectifs."
             )},
            {"name": "Fibonacci",
             "purpose": "la profondeur de la correction, pas un signal de retournement",
             "body": (
                 "Les retracements situent la correction par rapport au dernier mouvement. La zone "
                 "38,2–61,8 % (« zone d'or ») est celle où les tendances reprennent le plus souvent : "
                 "c'est une zone d'ENTRÉE. Les extensions, elles, servent de candidats d'objectif à "
                 "l'étape 3."
             )},
        ],
        # --- DE LA DÉCISION À L'ORDRE ---------------------------------------------------------
        "execution": {
            "sizing": [
                "Taille = (capital × % de risque) ÷ distance au stop. La position est donc "
                "dimensionnée pour que le montant perdu si le stop est touché vaille EXACTEMENT le "
                "pourcentage voulu du capital — un stop plus large donne une position plus petite, "
                "jamais un risque plus grand.",
                "Risque de base selon le profil : conservateur 0,5 % · modéré 1 % · agressif 2 %.",
                (f"Modulé par le verdict MESURÉ de la paire : ×{s.conviction_green_mult:g} sur une "
                 f"🟢 à échantillon solide (n ≥ {s.conviction_green_min_trades}), "
                 f"×{s.conviction_yellow_mult:g} sur une 🟡 ou une 🔴, ×1 sur une paire non mesurée. "
                 f"Plafond absolu {s.conviction_risk_cap_pct:g} % du capital par trade.")
                if s.conviction_sizing_enabled else "Sizing par conviction désactivé.",
                "Le dimensionnement se fait sur un prix FRAIS, celui auquel l'ordre sera réellement "
                "rempli — le lire dans un cache fausserait à la fois la taille, le contrôle « le "
                "prix est-il déjà sorti de la zone » et tout le P&L qui en découle.",
            ],
            "guards": [
                {"label": "Gel après pertes",
                 "body": (f"journée à −{s.daily_loss_freeze_pct:g} % ou semaine à "
                          f"−{s.weekly_loss_freeze_pct:g} % du capital : plus aucune nouvelle entrée. "
                          "Les positions ouvertes continuent d'être gérées.")
                 if s.loss_freeze_enabled else "désactivé"},
                {"label": "Position identique déjà ouverte",
                 "body": "le même symbole dans le même sens n'est jamais empilé"},
                {"label": "Anti-doublon",
                 "body": (f"le même symbole/sens ouvert il y a moins de "
                          f"{s.playbook_auto_entry_cooldown_min} min est refusé, même si la position "
                          "précédente s'est déjà refermée. Un déclencheur reste souvent actif "
                          "plusieurs passages : sans ce délai, quatre entrées identiques partent en "
                          "quatre minutes — quatre fois le risque prévu pour un seul pari.")},
                {"label": "Corrélation",
                 "body": (f"au maximum {s.max_positions_per_currency} positions de CHANGE partageant "
                          "une même devise. Ne s'applique PAS à la crypto : y compter deux paires en "
                          "USDT comme deux paris sur USDT plafonnait de fait tout le portefeuille "
                          "crypto à deux positions.")
                 if s.correlation_guard_enabled else "désactivée"},
                {"label": "Le prix a bougé pendant le calcul",
                 "body": "si le prix a déjà franchi le stop ou atteint l'objectif, l'ordre n'est pas "
                         "passé — le plan n'existe plus"},
                {"label": "Marché fermé",
                 "body": ("filtre PAR SYMBOLE : quand une place est fermée, les fournisseurs servent "
                          "encore la dernière bougie connue (celle de vendredi soir un samedi), "
                          "réelle mais plus d'actualité. La crypto en est exemptée, elle cote en "
                          "continu.")
                 if s.playbook_trade_only_when_open else
                 "restriction horaire désactivée — le desk travaille 24 h/24"},
                {"label": "Verdict de paire",
                 "body": (f"seules les paires 🟢 sont auto-tradées (espérance ≥ "
                          f"+{s.playbook_verdict_min_expectancy:g} R sur n ≥ "
                          f"{s.playbook_verdict_min_trades}, confirmée "
                          f"{s.playbook_verdict_green_streak} semaines de suite)")
                 if s.playbook_pair_gating else
                 ("désactivé : le verdict reste calculé, affiché, et module la TAILLE de position — "
                  "il ne décide plus s'il y a trade. C'est ce filtre qui écartait en silence des "
                  "setups prêts, et qui expliquait « le trade est dans les trades du jour mais rien "
                  "ne s'est passé ».")},
            ],
            "journal": (
                "Chaque position ouverte emporte le contexte qui l'a motivée : verdict de la paire "
                "et multiplicateur appliqué, % de risque réel, type de déclencheur, fenêtre de "
                "session, ATR du jour, le VOTE de chaque facteur à l'ouverture, la justification du "
                "stop et celle de l'objectif en toutes lettres, les niveaux TP1/TP2 et le stop de "
                "verrouillage, et le risque d'ORIGINE figé sur le prix réellement rempli. Un niveau "
                "qu'on ne sait pas justifier ne peut être ni discuté, ni appris — et sans ces "
                "champs, impossible de savoir après coup quelles conditions produisent les gagnants."
            ),
            "no_silent_refusal": (
                "Aucun refus n'est silencieux. Chaque passage de veille rend, symbole par symbole, "
                "le motif de ce qui a été écarté — garde-fou de portefeuille, corrélation, "
                "anti-doublon, marché fermé, verdict — et le rapport est horodaté puis réécrit à "
                "CHAQUE passage, y compris quand rien ne s'ouvre. Un rapport figé finit par décrire "
                "un état qui n'existe plus."
            ),
            "paper_only": (
                "L'auto-entrée ne touche QUE des connexions en mode papier. Une connexion réelle est "
                "ignorée quel que soit son statut KYC : aucun argent réel ne peut être engagé par "
                "cette boucle."
            ),
        },
        # --- COMMENT LES TRADES SONT CHOISIS ET CLASSÉS ---------------------------------------
        "selection": {
            "tiers": [
                {"label": "PRÊT", "body": "les quatre étapes validées ET le déclencheur 15 min "
                                          "actif — exécutable maintenant"},
                {"label": "ARMÉ", "body": "contexte validé (tendance + niveaux + 4 h + 1 h), en "
                                          "attente du déclencheur 15 min. L'auto-entrée s'en charge : "
                                          "la position part dès qu'il se forme"},
                {"label": "INSUFFISANT", "body": "données trop pauvres — le playbook ne conclut rien "
                                                 "et n'oppose pas non plus de veto"},
            ],
            "ranking": [
                "1. exécutable maintenant — un déclencheur actif passe devant tout le reste",
                "2. fiabilité MESURÉE — le taux de réussite du walk-forward nocturne pour CE symbole "
                "et CE type de déclencheur : de l'historique, pas une opinion",
                "3. score de fiabilité affiché (celui du trade s'il est prêt, celui du contexte s'il "
                "est armé)",
                "4. confiance de la stratégie, puis R/R, puis taille de l'objectif",
            ],
            "floor": (
                f"Seul filtre restant : un plancher de fiabilité de {s.daily_min_reliability}/5. "
                "« Tous les trades qui ont un potentiel fiable » n'est pas « tous les trades » — mais "
                "les setups écartés restent comptabilisés dans les verdicts, ils ne sont simplement "
                "pas proposés."
            ),
            "no_quota": (
                f"{'Aucun plafond' if not s.daily_top_trades_count else str(s.daily_top_trades_count)}"
                " de trades proposés par jour : le classement sert à ORDONNER, plus à éliminer. Un "
                "setup conforme qui arrivait 6e était auparavant supprimé alors que la méthode ne dit "
                "rien de tel."
            ),
            "universe_order": (
                "Ordre de balayage — les grands indices d'abord (liquides sur de larges plages "
                "horaires), puis les paires de la ou des sessions ouvertes avec priorité au "
                "chevauchement Londres/New York, puis le reste du catalogue. C'est une priorité, "
                "pas une exclusion."
            ),
        },
        # --- CE QUE MESURE CHAQUE INDICATEUR --------------------------------------------------
        # Servi depuis le glossaire du moteur : le même texte que celui affiché à côté de chaque
        # score dans l'analyse d'un symbole.
        "glossary": [
            {"term": {
                "ma": "MA 20 / MA 50", "rsi": "RSI 14", "macd": "MACD", "vwap": "VWAP",
                "structure": "Structure de marché", "volume": "Volume",
                "divergence": "Divergences", "fibonacci": "Fibonacci",
            }.get(key, key), "body": body}
            for key, body in pb.GLOSSARY.items()
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
