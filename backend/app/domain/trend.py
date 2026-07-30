"""ÉTAPE 1 de la stratégie — détection de tendance par confluence de plusieurs indicateurs.

La direction du marché n'est pas décidée par un indicateur, ni sur une seule unité de temps. Elle
est mesurée simultanément sur le **journalier, le 4 h, le 1 h et le 15 min** par six indicateurs
qui regardent le marché sous des angles différents (moyennes, structure, suiveur de tendance,
momentum, force du mouvement, participation), puis agrégée en un **score de confiance**.

Trois principes en découlent :

1. **La tendance est calculée UNE FOIS.** Une fois validée, elle est figée : ni la recherche du
   point d'entrée ni le calcul du stop et de l'objectif ne peuvent la remettre en cause. Sans cette
   règle, chaque outil re-débattrait de la direction et le système finirait par se contredire.
2. **L'ADX est un verrou, pas un vote.** En dessous du seuil, il n'y a pas de tendance à suivre :
   aucune position, quel que soit l'enthousiasme des autres indicateurs. Un marché sans direction
   fait sauter les stops des deux côtés.
3. **Ce qui n'est pas mesurable ne vote pas.** Le forex spot n'a pas de volume centralisé : dans ce
   cas le facteur volume est retiré du calcul et les poids restants sont renormalisés. On ne
   remplace jamais une donnée absente par un zéro, qui se lirait comme un avis neutre.

**L'ADX ne fait pas partie de la stratégie.** Il a été essayé comme verrou (pas de trade sous un
seuil de force) et la mesure l'a condamné : sur le même échantillon, l'écarter fait passer
l'espérance de +0,12 R à +0,33 R et multiplie le volume par 2,6. Il coupait surtout de bons trades,
parce qu'il mesure la force d'une tendance déjà installée alors que les meilleures entrées se
prennent quand elle redémarre. Il reste calculé et affiché à titre informatif, il ne décide rien.
"""

from __future__ import annotations

from app.domain import indicators as ind
from app.domain import market_structure as ms
from app.domain.indicators import Candle

# Poids de chaque indicateur dans le vote d'une unité de temps. Surchargeables par configuration
# (`playbook_trend_weights`), renormalisés sur les seuls facteurs réellement calculables.
TREND_WEIGHTS = {
    "ema": 0.28,          # EMA 50 / EMA 200 : la tendance principale
    "structure": 0.20,    # HH/HL/LH/LL : ce que le graphique dit vraiment
    "supertrend": 0.16,   # confirmation, filtre les faux signaux
    "macd": 0.16,         # momentum
    "rsi": 0.10,          # force du mouvement — jamais seul, jamais décisif
    "volume": 0.10,       # la tendance est-elle soutenue ?
}

# Poids des unités de temps dans le score global. Le journalier donne la direction de fond, le
# 15 min ne pèse presque rien : il sert au TIMING, pas à la direction.
TF_WEIGHTS = {"journalier": 0.40, "4h": 0.30, "1h": 0.20, "15m": 0.10}

# Libellés tels qu'ils sont écrits partout ailleurs dans le desk (jamais « 4h » ni « m15 »).
_TF_LABELS = {"mensuel": "mensuel", "journalier": "journalier", "4h": "4 h", "1h": "1 h",
              "15m": "15 min"}

TREND_MIN_SCORE = 0.10      # |score agrégé| minimal pour oser nommer une direction

# Unités de temps qui doivent RÉELLEMENT s'aligner pour valider la tendance.
#
# Le journalier n'en fait plus partie depuis le 28/07/2026 (décision de l'utilisateur : « le D1
# n'est pas obligatoire pour la validation de tendance, il suffit de la validation du 1 h et du
# 4 h »). Ce n'est pas la même chose que « le journalier ne compte plus » : il pèse toujours 40 %
# du score agrégé, donc un journalier franchement contraire tire le score sous le seuil de netteté
# et empêche encore la tendance d'être nommée. Ce qu'il ne peut plus faire, c'est poser un veto à
# lui seul alors que le 4 h et le 1 h — les deux unités qui portent réellement un trade dont
# l'entrée se prend en 15 min — disent la même chose.
REQUIRED_TFS: tuple[str, ...] = ("4h", "1h")

# Un « conflit important » se juge sur les PILIERS de la direction : les moyennes, la structure du
# graphique et le suiveur de tendance. Quand deux d'entre eux se contredisent, la tendance n'est pas
# lisible et il ne faut pas trader. Le MACD et le RSI, eux, mesurent un momentum de court terme qui
# respire à contretemps dans toute tendance en escalier : les compter comme conflit bloquerait la
# quasi-totalité des tendances saines. Ils pèsent donc dans le score (un momentum contraire le tire
# nettement vers le bas) sans jamais poser de veto à eux seuls.
CONFLICT_KEYS = ("ema", "structure", "supertrend")
CONFLICT_SIGNAL = 0.5
MONTHLY_VETO = -0.30        # un mensuel fortement contraire interdit la tendance
FRESH_FLIP_BARS = 3         # une bascule du SuperTrend plus récente que cela est fragile

_STATUS_REASONS = {
    "insufficient": "historique insuffisant sur une unité de temps",
    "no_direction": "aucune direction ne se dégage",
    "misaligned": "les unités de temps ne disent pas la même chose",
    "conflict": "conflit majeur entre indicateurs",
    "monthly_against": "le mensuel va franchement à contresens",
}


def parse_weights(raw: str, defaults: dict[str, float] | None = None) -> dict[str, float]:
    """Lit des poids depuis une chaîne de configuration ``"ema:0.3,rsi:0.05"``.

    Tout indicateur non cité garde son poids par défaut ; une valeur illisible est ignorée plutôt
    que de faire tomber le calcul (une faute de frappe dans un `.env` ne doit pas arrêter le desk).
    """
    out = dict(defaults or TREND_WEIGHTS)
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip()
        if key not in out:
            continue
        try:
            out[key] = max(0.0, float(value))
        except ValueError:
            continue
    return out


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def trend_vote_layer(
    candles: list[Candle], label: str, *, weights: dict[str, float] | None = None,
) -> dict:
    """Vote de tendance d'UNE unité de temps, décomposé indicateur par indicateur.

    Chaque indicateur rend un vote dans [-1, +1] ; le score de la couche est la moyenne de ces
    votes pondérée par `weights`, renormalisée sur les seuls indicateurs disponibles.

    Retourne ``{"label", "score", "bias", "ok", "adx", "plus_di", "minus_di", "factors",
    "conflict"}``. `ok=False` signifie que l'historique ne suffit pas — pas qu'il n'y a pas de
    tendance.
    """
    w = weights or TREND_WEIGHTS
    out = {"label": label, "score": 0.0, "bias": 0, "ok": False, "adx": None,
           "plus_di": None, "minus_di": None, "factors": [], "conflict": None}
    if not candles or len(candles) < 60:
        return out

    closes = [c.close for c in candles]
    price = closes[-1]
    factors: list[dict] = []

    def vote(key: str, value: str, reading: str, signal: float) -> None:
        factors.append({"key": key, "value": value, "reading": reading,
                        "signal": round(_clamp(signal), 3), "weight": w.get(key, 0.0)})

    # --- EMA 50 / EMA 200 : la tendance principale -------------------------------------------
    ema50_s = ind.ema(closes, 50)
    ema200_s = ind.ema(closes, 200) if len(closes) >= 60 else []
    if ema50_s and ema200_s:
        ema50, ema200 = ema50_s[-1], ema200_s[-1]
        if price > ema50 > ema200:
            sig, reading = 1.0, "prix > EMA50 > EMA200 : empilement haussier complet"
        elif price < ema50 < ema200:
            sig, reading = -1.0, "prix < EMA50 < EMA200 : empilement baissier complet"
        elif ema50 > ema200:
            sig, reading = 0.3, "EMA50 au-dessus de EMA200, mais le prix hésite entre les deux"
        elif ema50 < ema200:
            sig, reading = -0.3, "EMA50 sous EMA200, mais le prix hésite entre les deux"
        else:
            sig, reading = 0.0, "EMA50 et EMA200 confondues"
        # Un croisement RÉCENT est l'information la plus fraîche de la couche : il prime sur
        # l'empilement, qui met du temps à se reformer après un retournement.
        cross = _recent_cross(ema50_s, ema200_s, lookback=10)
        if cross:
            sig = _clamp(0.5 * sig + 0.5 * (0.6 * cross))
            reading += f" ; croisement {'haussier' if cross > 0 else 'baissier'} récent"
        vote("ema", f"EMA50 {ema50:.6g} · EMA200 {ema200:.6g}", reading, sig)

    # --- Structure de marché : HH/HL contre LH/LL ---------------------------------------------
    struct = ms.label_swings(candles)
    state = struct["state"]
    struct_sig = 1.0 if state == "haussière" else -1.0 if state == "baissière" else 0.0
    if state != "indéterminée":
        seq = " ".join(struct["sequence"][-4:]) or "aucun pivot étiqueté"
        vote("structure", state, f"derniers pivots : {seq}", struct_sig)

    # --- SuperTrend : confirmation ------------------------------------------------------------
    st = ind.supertrend(candles, 10, 3.0)
    if st:
        sig = float(st["direction"])
        reading = f"bande à {st['level']:.6g}"
        if st["flipped_ago"] < FRESH_FLIP_BARS:
            # Une bascule toute fraîche n'a pas encore fait ses preuves : elle compte pour moitié.
            sig *= 0.5
            reading += f" ; bascule il y a {st['flipped_ago']} bougie(s), encore fragile"
        vote("supertrend", "haussier" if st["direction"] > 0 else "baissier", reading, sig)

    # --- MACD : momentum ----------------------------------------------------------------------
    macd_all = ind.macd_series(closes)
    if macd_all is not None:
        _, _, hist_s = macd_all
        hist = hist_s[-1]
        prev = hist_s[-2] if len(hist_s) > 1 else hist
        strengthening = abs(hist) >= abs(prev)
        sig = (1.0 if hist > 0 else -1.0 if hist < 0 else 0.0) * (1.0 if strengthening else 0.5)
        vote("macd", f"histogramme {hist:+.6g}",
             "momentum " + ("qui se renforce" if strengthening else "qui s'essouffle"), sig)

    # --- RSI : force du mouvement, jamais seul ------------------------------------------------
    rsi_s = ind.rsi_series(closes, 14)
    rsi_val = next((v for v in reversed(rsi_s) if v is not None), None) if rsi_s else None
    if rsi_val is not None:
        if rsi_val > 55:
            sig, reading = 0.8, "au-dessus de 55 : les acheteurs mènent"
        elif rsi_val < 45:
            sig, reading = -0.8, "sous 45 : les vendeurs mènent"
        else:
            sig, reading = (rsi_val - 50) / 12.5, "zone neutre : le RSI ne tranche pas"
        if rsi_val > 70:
            sig, reading = 0.2, "surachat (>70) : direction haussière mais mouvement étiré"
        elif rsi_val < 30:
            sig, reading = -0.2, "survente (<30) : direction baissière mais mouvement étiré"
        # Une divergence contraire est un avertissement : elle plafonne le vote du RSI, elle ne
        # l'inverse pas (le RSI ne décide jamais seul d'un retournement).
        div = ind.divergence(candles, rsi_s)
        if div and ((div == "baissière" and sig > 0) or (div == "haussière" and sig < 0)):
            sig = _clamp(sig) * 0.25
            reading += f" ; divergence {div} en sens inverse"
        vote("rsi", f"{rsi_val:.1f}", reading, sig)

    # --- Volume : la tendance est-elle soutenue ? ----------------------------------------------
    # Absent sur le forex spot : dans ce cas le facteur ne vote pas du tout (il est retiré du
    # dénominateur), au lieu d'être compté comme un avis neutre qui diluerait les autres.
    rel_vol = ind.relative_volume(candles, 20)
    if rel_vol is not None:
        obv_s = ind.obv(candles)
        obv_slope = ind.slope_pct([float(v) for v in obv_s], 10) if len(obv_s) > 12 else None
        if obv_slope is not None:
            sig = _clamp(obv_slope / 5.0)
            reading = (f"OBV {'en hausse' if obv_slope > 0 else 'en baisse'} "
                       f"({obv_slope:+.2f} %), volume relatif {rel_vol:.2f}")
            if rel_vol < 0.8:
                sig *= 0.5
                reading += " — participation faible, mouvement peu soutenu"
            vote("volume", f"{rel_vol:.2f}×", reading, sig)

    if not factors:
        return out

    total_weight = sum(f["weight"] for f in factors)
    if total_weight <= 0:
        return out
    score = sum(f["signal"] * f["weight"] for f in factors) / total_weight
    bias = 1 if score > TREND_MIN_SCORE else -1 if score < -TREND_MIN_SCORE else 0

    # Conflit : un PILIER de la direction vote franchement contre le résultat de la couche.
    conflict = None
    for f in factors:
        if (f["key"] in CONFLICT_KEYS and score != 0
                and f["signal"] * score < 0 and abs(f["signal"]) >= CONFLICT_SIGNAL):
            conflict = f["key"]
            break

    adx_res = ind.adx_di(candles, 14)
    out.update(
        score=round(score, 4), bias=bias, ok=True, factors=factors, conflict=conflict,
        adx=round(adx_res[0], 1) if adx_res else None,
        plus_di=round(adx_res[1], 1) if adx_res else None,
        minus_di=round(adx_res[2], 1) if adx_res else None,
    )
    return out


def _recent_cross(fast: list[float], slow: list[float], *, lookback: int = 10) -> int:
    """+1 / -1 si les deux moyennes se sont croisées dans les `lookback` dernières bougies."""
    n = min(len(fast), len(slow))
    if n < lookback + 2:
        return 0
    for i in range(n - lookback, n):
        before = fast[i - 1] - slow[i - 1]
        after = fast[i] - slow[i]
        if before <= 0 < after:
            return 1
        if before >= 0 > after:
            return -1
    return 0


def parse_required_tfs(raw: str) -> tuple[str, ...]:
    """Lit ``"4h,1h"`` depuis la configuration et ne garde que des unités de temps connues.

    Une chaîne vide ou entièrement illisible retombe sur `REQUIRED_TFS` : une faute de frappe dans
    un `.env` ne doit pas supprimer silencieusement toute condition d'alignement.
    """
    wanted = tuple(
        part.strip() for part in (raw or "").split(",")
        if part.strip() in TF_WEIGHTS
    )
    return wanted or REQUIRED_TFS


def trend_confidence(
    monthly: list[Candle], daily: list[Candle], h4: list[Candle],
    h1: list[Candle], m15: list[Candle], *,
    weights: dict[str, float] | None = None, min_score: float = TREND_MIN_SCORE,
    required_tfs: tuple[str, ...] | None = None,
) -> dict:
    """LA tendance — calculée une seule fois, puis figée pour tout le reste de l'analyse.

    Le score global agrège les quatre unités de temps d'exécution :
    ``S = 0,40 × journalier + 0,30 × 4 h + 0,20 × 1 h + 0,10 × 15 min``.

    La tendance n'est déclarée **valide** que si TOUTES ces conditions tiennent :

    1. une direction se dégage (``|S| ≥ min_score``) ;
    2. les unités de temps EXIGÉES (`required_tfs`, par défaut le 4 h et le 1 h) vont toutes dans
       ce sens. Le journalier n'en fait plus partie : il pèse 40 % du score — donc un journalier
       contraire empêche encore souvent la direction d'atteindre le seuil — mais il ne pose plus de
       veto à lui seul. Le 15 min ne bloque pas non plus, et pour la raison inverse : au moment où
       l'on cherche une entrée, il est justement en correction ;
    3. aucun conflit majeur d'indicateur sur ces mêmes unités de temps exigées ;
    4. le mensuel n'est pas franchement contraire.

    Le **score de confiance** vaut ``|S|`` : la netteté de l'accord entre les unités de temps.

    Retourne un dictionnaire complet (``status``, ``direction``, ``confidence``, ``score_100``,
    ``per_tf``, ``adx``, ``reasons``, ``explanation``). ``status="insufficient"`` quand une unité
    de temps manque : on préfère ne rien conclure plutôt que d'extrapoler. L'ADX est publié dans
    ``adx`` pour information — il ne conditionne rien (voir l'en-tête du module).
    """
    w = weights or TREND_WEIGHTS
    required = tuple(required_tfs) if required_tfs else REQUIRED_TFS
    layers = {
        "mensuel": trend_vote_layer(monthly, "mensuel", weights=w),
        "journalier": trend_vote_layer(daily, "journalier", weights=w),
        "4h": trend_vote_layer(h4, "4h", weights=w),
        "1h": trend_vote_layer(h1, "1h", weights=w),
        "15m": trend_vote_layer(m15, "15m", weights=w),
    }
    base = {
        "status": "insufficient", "direction": 0, "score": 0.0, "confidence": 0.0,
        "score_100": 0, "per_tf": layers, "monthly_bias": layers["mensuel"]["bias"],
        "adx": {}, "reasons": [], "explanation": "",
    }
    missing = [_TF_LABELS[name] for name in TF_WEIGHTS if not layers[name]["ok"]]
    if missing:
        base["reasons"] = [f"{_STATUS_REASONS['insufficient']} : {', '.join(missing)}"]
        base["explanation"] = (
            "Tendance non calculable : il manque de l'historique sur " + ", ".join(missing)
            + ". Aucune direction n'est déduite — une donnée absente ne vaut pas un avis neutre."
        )
        return base

    score = sum(layers[name]["score"] * weight for name, weight in TF_WEIGHTS.items())
    direction = 1 if score > min_score else -1 if score < -min_score else 0

    # ADX publié pour information seulement : la stratégie ne s'en sert pas pour décider.
    adx_info = {
        "journalier": layers["journalier"]["adx"] or 0.0,
        "4h": layers["4h"]["adx"] or 0.0,
        "plus_di": layers["journalier"]["plus_di"] or 0.0,
        "minus_di": layers["journalier"]["minus_di"] or 0.0,
        "informatif": True,
    }

    reasons: list[str] = []
    status = "valid"
    if direction == 0:
        status = "no_direction"
        reasons.append(
            f"score agrégé {score:+.2f}, sous le seuil de {min_score:.2f} : le marché ne "
            "tranche pas"
        )
    else:
        # 2) alignement des unités de temps EXIGÉES (par défaut : 4 h et 1 h).
        # Le 15 min pèse dans le score (10 %) mais n'est pas une condition bloquante, et c'est
        # délibéré : au moment où l'on cherche une entrée, le 15 min est justement en correction —
        # c'est même ce qu'on attend pour acheter un repli sur zone. Exiger son alignement rendrait
        # impossible la stratégie d'entrée de l'étape 2. Le journalier est sorti de cette liste le
        # 28/07/2026 : il reste dans le SCORE (40 %), il n'est plus un veto (cf. `REQUIRED_TFS`).
        against = [_TF_LABELS[name] for name in required
                   if layers[name]["bias"] != direction]
        if against:
            status = "misaligned"
            reasons.append("unité(s) de temps non alignée(s) : " + ", ".join(against))
        # 3) conflits — sur les mêmes unités de temps que celles dont on exige l'alignement.
        if status == "valid":
            conflicts = [f"{_TF_LABELS[name]} ({layers[name]['conflict']})"
                         for name in required if layers[name]["conflict"]]
            if conflicts:
                status = "conflict"
                reasons.append("indicateur(s) en contradiction : " + ", ".join(conflicts))
        # 4) mensuel contraire
        if status == "valid" and layers["mensuel"]["ok"]:
            monthly_score = layers["mensuel"]["score"]
            if monthly_score * direction < MONTHLY_VETO:
                status = "monthly_against"
                reasons.append(
                    f"le mensuel penche franchement dans l'autre sens (score {monthly_score:+.2f})"
                )

    # La confiance est la NETTETÉ de l'accord entre les unités de temps, rien d'autre. Elle ne
    # dépend plus de l'ADX : la mesure a montré que le pondérer par la force du mouvement
    # dégradait la sélection au lieu de l'améliorer.
    confidence = max(0.0, min(1.0, abs(score)))

    base.update(
        status=status, direction=direction if status == "valid" else 0,
        score=round(score, 4), confidence=round(confidence, 3),
        score_100=round(confidence * 100), adx=adx_info, reasons=reasons,
        required_tfs=[_TF_LABELS[name] for name in required],
        explanation=_explain(status, score, direction, layers, adx_info, confidence, reasons,
                             required),
    )
    return base


def _explain(
    status: str, score: float, direction: int, layers: dict, adx: dict,
    confidence: float, reasons: list[str], required: tuple[str, ...] = REQUIRED_TFS,
) -> str:
    """Le calcul de la tendance, raconté comme on l'expliquerait à un collègue."""
    parts = [
        "Tendance de fond mesurée sur quatre unités de temps par six indicateurs (EMA 50/200, "
        "structure du graphique, SuperTrend, MACD, RSI, volume) : "
        + " · ".join(
            f"{_TF_LABELS[name]} {layers[name]['score']:+.2f}"
            for name in ("journalier", "4h", "1h", "15m")
        )
        + f" → score global {score:+.2f} "
        f"(40 % journalier, 30 % 4 h, 20 % 1 h, 10 % 15 min)."
    ]
    parts.append(
        "Unités de temps dont l'accord est EXIGÉ : "
        + " et ".join(_TF_LABELS[name] for name in required)
        + f". Le journalier ({layers['journalier']['score']:+.2f}) pèse le plus lourd dans le score "
        "mais n'est plus une condition à lui seul : un 4 h et un 1 h qui disent la même chose "
        "suffisent à nommer la direction."
    )
    parts.append(
        f"Le mensuel donne le biais de fond ({layers['mensuel']['score']:+.2f}) : il ne vote pas "
        "dans le score, mais un mensuel franchement contraire interdit le trade."
    )
    parts.append(
        f"Pour information, la force du mouvement (ADX) vaut {adx.get('journalier', 0):.0f} en "
        f"journalier et {adx.get('4h', 0):.0f} en 4 h : c'est une lecture, pas une condition — "
        "la mesure a montré qu'en faire un filtre coupait surtout de bons trades."
    )
    if status == "valid":
        parts.append(
            f"Tendance {'HAUSSIÈRE' if direction > 0 else 'BAISSIÈRE'} validée, confiance "
            f"{round(confidence * 100)}/100. Elle est maintenant FIGÉE : la recherche du point "
            "d'entrée et le calcul du stop et de l'objectif ne la rediscuteront pas."
        )
    else:
        parts.append("Aucune tendance exploitable — " + " ; ".join(reasons) + ".")
    return " ".join(parts)
