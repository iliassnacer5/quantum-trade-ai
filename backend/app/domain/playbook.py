"""PLAYBOOK — la stratégie de trading imposée à TOUS les agents du projet.

Méthode, dans cet ordre STRICT (aucune étape ne peut être sautée) :

1. **Mensuel + Journalier** — établir la tendance de fond et FIXER les supports / résistances
   MAJEURS. Sans tendance de fond claire : pas de trade.
2. **Journalier (détail)** — RSI 14, MA 20, MA 50, volume, tendance VWAP, divergences RSI et MACD,
   et retracements de Fibonacci **en cas de correction**. Le journalier doit CONFIRMER le biais.
3. **4 heures** — exactement les mêmes facteurs, pour confirmer une seconde fois.
4. **1 heure** — DERNIÈRE confirmation avant de chercher l'entrée. Mêmes facteurs encore une fois :
   le 1 h doit lui aussi aller dans le sens du biais.
5. **15 minutes** — la SEULE unité de temps où l'on ENTRE. Déclencheur d'entrée : repli sur MA/zone
   d'or Fibonacci avec bougie de reprise, cassure confirmée par le volume, ou divergence.

NIVEAUX DU TRADE — **objectif ≥ 200 pips, R/R entre 1:2 et 1:3** :
- L'**objectif** vaut au minimum 200 pips, et le gain visé représente 2 à 3 fois le risque.
- Conséquence arithmétique : le **stop** vaut entre 200/3 ≈ 67 pips et 200/2 = 100 pips. Un stop de
  cette taille ne peut pas venir de la structure 15 min (quelques pips) : il est placé derrière la
  **structure 4 h / journalière**. Le 15 min ne porte donc pas le risque, il donne le TIMING.
- L'objectif doit rester atteignable **avant le niveau majeur opposé** et compatible avec la
  volatilité journalière (l'horizon estimé du trade est calculé et affiché).

SÉCURISATION DU PROFIT — dès que le trade a parcouru **+2R**, le stop est automatiquement remonté
**sur +2R** : la position ne peut plus redevenir perdante, et on la laisse courir vers le R/R
maximum (1:3). Voir `secured_stop` ici et `execution_service.secure_open_profits`.
- Le timing de session (ouverture Londres, ouverture New York, et surtout leur CHEVAUCHEMENT)
  note la qualité de la fenêtre d'entrée.

**Tout est expliqué, jamais un score brut sans justification.** Chaque unité de temps produit la
liste de ses facteurs avec, pour chacun : la valeur mesurée, sa lecture en français, son poids, sa
CONTRIBUTION signée au score, et ce que l'indicateur mesure. Le score de la couche est donc
décomposable : on voit exactement d'où viennent les -0,47.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.domain import indicators as ind
from app.domain import pips as pips_mod
from app.domain.indicators import Candle

# --- Contraintes de la stratégie (surchargables par la configuration) ---
# Le risque/rendement est encadré dans une BANDE [1,2 ; 1,3] : un objectif proche du risque est
# atteint beaucoup plus souvent, et le plafond empêche un stop trop serré (qui gonflerait le R/R
# affiché tout en faisant sauter le trade au premier soubresaut).
MIN_RR = 2.0                 # risque/rendement minimum : le gain vaut au moins 2 fois le risque
MAX_RR = 3.0                 # risque/rendement maximum
MIN_TARGET_PIPS = 200.0      # objectif minimum, en pips (cf. domain.pips hors forex)
# Conséquence arithmétique : viser 200 pips avec un R/R plafonné à 1:3 impose un stop d'au moins
# 200 / 3 ≈ 67 pips. Un stop de cette taille vient de la structure 4 h / journalière, PAS du 15 min.
MAX_STOP_PIPS = 150.0        # garde-fou absolu en pips
TARGET_LEVEL_BUFFER = 0.15   # marge (× ATR) laissée avant le niveau majeur qui borne l'objectif
MAX_ATR_MULTIPLE = 4.0       # objectif <= N × ATR journalier (sinon hors de portée)
MIN_LAYER_SCORE = 0.08       # en-deçà, une unité de temps est « neutre » (pas de confirmation)
# Sécurisation du profit : dès +2R atteint, le stop est remonté sur +2R (le trade ne peut plus
# redevenir perdant) et on laisse courir vers le R/R maximum.
SECURE_AT_R = 2.0
# Filtre de volatilité — seuil issu du backtest (perdants : ATR journalier 1,39 % ; gagnants 1,15 %).
MAX_ATR_PCT = 1.3
VOLATILITY_MAX_WIDEN = 1.6   # élargissement maximal du stop, en multiple de la distance initiale

# Échelle de fiabilité affichée à l'utilisateur : +1 à +5 pour un ACHAT, -1 à -5 pour une VENTE,
# 0 quand aucun trade n'est justifié. C'est la seule note montrée dans l'interface.
RELIABILITY_LABELS = {
    5: "très fiable", 4: "fiable", 3: "correct", 2: "fragile", 1: "très fragile",
    0: "ne tranche pas",
}


def to_five(signal: float) -> int:
    """Convertit un vote interne [-1, +1] en score de fiabilité sur 5.

    Positif = argument en faveur d'un ACHAT (+1 à +5), négatif = en faveur d'une VENTE (-1 à -5),
    0 = le facteur ne tranche pas.

    Arrondi « à l'unité la plus proche, en s'éloignant de zéro » (0,5 → 3 et non 2) : c'est ce
    qu'attend un lecteur humain, contrairement à l'arrondi bancaire de `round`.
    """
    raw = signal * 5
    rounded = math.floor(raw + 0.5) if raw >= 0 else math.ceil(raw - 0.5)
    return int(max(-5, min(5, rounded)))


def reliability_label(score: int) -> str:
    """Libellé d'un FACTEUR : le signe donne le camp, la valeur absolue donne la solidité.

    Un score de -5 signifie « argument de vente très fiable » — sans cette précision, « très
    fiable » sur un score négatif serait ambigu.
    """
    s = int(score)
    base = RELIABILITY_LABELS.get(abs(s), "")
    if s == 0:
        return base
    return f"argument de {'hausse' if s > 0 else 'baisse'} {base}"


def trade_reliability_label(score: int) -> str:
    """Libellé d'une UNITÉ DE TEMPS ou d'un TRADE (le camp est déjà donné par la direction)."""
    s = int(score)
    if s == 0:
        return "aucun signal exploitable"
    return RELIABILITY_LABELS.get(abs(s), "")


def fmt_score(score: int) -> str:
    """Score sur 5 tel qu'affiché : « +4/5 », « -3/5 », « 0/5 » (jamais « +0/5 »)."""
    s = int(score)
    return f"{s:+d}/5" if s else "0/5"

# Nombre minimum de bougies exploitables par unité de temps.
MIN_CANDLES = {"mensuel": 12, "journalier": 60, "4h": 60, "1h": 60, "15m": 60}

# Pédagogie : ce que MESURE chaque facteur. Affiché tel quel dans l'interface, à côté de sa valeur,
# pour que le score ne soit jamais un chiffre opaque.
GLOSSARY: dict[str, str] = {
    "ma": (
        "Les moyennes mobiles 20 et 50 périodes situent le prix par rapport à sa tendance récente. "
        "Prix au-dessus des deux ET MA20 au-dessus de MA50 : les acheteurs contrôlent (empilement "
        "haussier). L'inverse pour les vendeurs. Entremêlées : le marché est en range."
    ),
    "rsi": (
        "Le RSI 14 mesure la force du mouvement sur 14 périodes, de 0 à 100. Au-dessus de 55 le "
        "momentum est haussier, sous 45 il est baissier, entre les deux il est neutre. Au-delà de 70 "
        "(ou sous 30) le mouvement est étiré : la direction est bonne mais l'entrée devient tardive, "
        "d'où un vote volontairement faible."
    ),
    "macd": (
        "Le MACD compare une moyenne rapide et une moyenne lente. Son histogramme positif signale "
        "une accélération haussière, négatif une accélération baissière. On regarde aussi s'il se "
        "renforce (le mouvement prend de la vitesse) ou s'essouffle (il ralentit)."
    ),
    "vwap": (
        "Le VWAP est le prix moyen pondéré par les volumes : au-dessus, les acheteurs paient plus "
        "cher que la moyenne payée par le marché. Sa PENTE donne la direction du flux d'ordres."
    ),
    "structure": (
        "La structure de marché lit les sommets et les creux : ascendants = tendance haussière, "
        "descendants = tendance baissière, mélangés = range, donc pas de tendance exploitable."
    ),
    "volume": (
        "Le volume ne donne aucune direction : il VALIDE. Un mouvement accompagné d'un volume "
        "supérieur à la moyenne 20 est porté par de vrais participants ; un volume faible signale un "
        "mouvement creux. Il agit donc comme multiplicateur du score, pas comme vote."
    ),
    "divergence": (
        "Une divergence apparaît quand le prix fait un nouvel extrême que l'oscillateur ne confirme "
        "pas (prix plus bas mais RSI plus haut, par exemple) : le mouvement s'essouffle et un "
        "retournement devient probable. C'est un contre-signal fort."
    ),
    "fibonacci": (
        "Les retracements de Fibonacci mesurent la PROFONDEUR d'une correction par rapport au "
        "dernier mouvement. La zone 38,2–61,8 % (« zone d'or ») est celle où les tendances "
        "reprennent le plus souvent : c'est une zone d'entrée, pas un signal de retournement."
    ),
}

# Échelle de lecture d'un score de couche (identique partout dans l'application).
SCORE_SCALE = [
    (0.60, "biais très fort"),
    (0.30, "biais net"),
    (MIN_LAYER_SCORE, "biais léger"),
    (0.0, "neutre (aucun biais exploitable)"),
]


def score_strength(score: float) -> str:
    """Traduit un score [-1,1] en force lisible (« biais net », « neutre »…)."""
    a = abs(score)
    for threshold, label in SCORE_SCALE:
        if a >= threshold:
            return label
    return "neutre"


# --------------------------------------------------------------------------------------
# Couche d'analyse commune — les facteurs exigés par la stratégie, appliqués à N'IMPORTE
# quelle unité de temps (journalier, 4h, 15m). C'est ce qui garantit que l'analyse 15 min
# « prend en considération les mêmes facteurs que l'analyse 4h ».
# --------------------------------------------------------------------------------------
@dataclass
class Layer:
    label: str
    score: float                       # biais directionnel de l'unité de temps [-1, +1]
    bias: int                          # -1 / 0 / +1
    notes: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    ok: bool = True                    # False si données insuffisantes
    factors: list[dict] = field(default_factory=list)   # détail expliqué, facteur par facteur
    breakdown: dict = field(default_factory=dict)       # décomposition arithmétique du score
    explanation: str = ""              # phrase qui explique le score obtenu

    @property
    def score_5(self) -> int:
        """Score de fiabilité de l'unité de temps sur l'échelle affichée (-5 à +5)."""
        return to_five(self.score)

    def as_dict(self) -> dict:
        return {"label": self.label, "score": round(self.score, 3), "bias": self.bias,
                "score_5": self.score_5, "reliability": trade_reliability_label(self.score_5),
                "strength": score_strength(self.score), "notes": self.notes,
                "metrics": self.metrics, "ok": self.ok, "factors": self.factors,
                "breakdown": self.breakdown, "explanation": self.explanation}


def _bias_of(score: float) -> int:
    return 1 if score > MIN_LAYER_SCORE else -1 if score < -MIN_LAYER_SCORE else 0


def _verdict(signal: float) -> str:
    return "haussier" if signal > 0.1 else "baissier" if signal < -0.1 else "neutre"


def structure_bias(candles: list[Candle], lookback: int = 60) -> tuple[int, str]:
    """Structure de marché : sommets/creux ascendants (haussier) ou descendants (baissier)."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    sw = ind.swing_points(window, 2, 2)
    highs, lows = sw["highs"], sw["lows"]
    if len(highs) < 2 or len(lows) < 2:
        return 0, "structure indéterminée (pas assez de swings)"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    if hh and hl:
        return 1, "structure haussière (sommets et creux ascendants)"
    if not hh and not hl:
        return -1, "structure baissière (sommets et creux descendants)"
    return 0, "structure en range (sommets/creux non alignés)"


def fibonacci_context(candles: list[Candle], lookback: int = 100) -> dict | None:
    """Fibonacci **uniquement en cas de correction** — comme l'exige la stratégie.

    Mesure le retracement du dernier swing : 0 % = sur l'extrême, 100 % = swing entièrement effacé.
    On ne parle de correction qu'entre 15 % et 85 %, et de zone d'entrée qu'entre 38,2 % et 65 %.
    """
    fib = ind.fibonacci_levels(candles, lookback)
    if not fib:
        return None
    price = candles[-1].close
    hi, lo = fib["high"], fib["low"]
    span = hi - lo
    if span <= 0:
        return None
    retr = (hi - price) / span if fib["swing"] == "haussier" else (price - lo) / span
    retr = max(0.0, min(1.5, retr))
    return {
        "swing": fib["swing"],
        "high": fib["high"], "low": fib["low"], "levels": fib["levels"],
        "retracement_pct": round(retr * 100, 1),
        "in_correction": 0.15 <= retr <= 0.85,
        "golden_zone": 0.382 <= retr <= 0.65,   # zone 38,2 % – 61,8 % (+ marge)
        "invalidated": retr > 0.9,              # swing effacé : le scénario ne tient plus
    }


def _num(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}g}"


def factor_layer(candles: list[Candle], label: str, symbol: str = "") -> Layer:
    """Applique LES facteurs de la stratégie à une unité de temps, en EXPLIQUANT chaque score.

    RSI 14 · MA 20 · MA 50 · volume · tendance VWAP · divergences RSI et MACD · Fibonacci si
    correction · structure de marché.

    Chaque facteur produit : sa valeur mesurée, sa lecture, son vote [-1,+1], son poids et sa
    contribution signée au score final. Le score de la couche est la somme des contributions,
    ajustée des divergences puis multipliée par la qualité du volume — et cette arithmétique est
    renvoyée telle quelle dans `breakdown` / `explanation`.
    """
    need = MIN_CANDLES.get(label, 60)
    if len(candles) < max(need, 30):
        return Layer(label, 0.0, 0, [f"Données {label} insuffisantes ({len(candles)} bougies)"], {}, ok=False)

    closes = [c.close for c in candles]
    price = closes[-1]
    m: dict = {"price": round(price, 8)}
    factors: list[dict] = []

    def add(key: str, lbl: str, value: str, reading: str, signal: float, weight: float) -> None:
        clamped = max(-1.0, min(1.0, signal))
        factors.append({
            "key": key, "label": lbl, "value": value, "reading": reading,
            "signal": round(clamped, 3), "weight": weight,
            # Score affiché à l'utilisateur : +1..+5 = argument haussier, -1..-5 = argument baissier.
            "score": to_five(clamped), "reliability": reliability_label(to_five(clamped)),
            "verdict": _verdict(signal), "explain": GLOSSARY.get(key, ""),
        })

    # --- MA 20 / MA 50 (moyennes SIMPLES, telles que nommées dans la stratégie) — poids 0,30 ---
    ma20_s = ind.sma_series(closes, 20)
    ma50_s = ind.sma_series(closes, 50)
    ma20 = ma20_s[-1] if ma20_s else None
    ma50 = ma50_s[-1] if ma50_s else None
    ma_slope = ind.slope_pct(ma20_s, 5) if ma20_s else None
    if ma20 is not None:
        m["ma20"] = round(ma20, 6)
        if ma50 is not None:
            m["ma50"] = round(ma50, 6)
            if price > ma20 > ma50:
                ma_signal, reading = 1.0, "prix > MA20 > MA50 : empilement haussier complet"
            elif price < ma20 < ma50:
                ma_signal, reading = -1.0, "prix < MA20 < MA50 : empilement baissier complet"
            elif ma20 > ma50:
                ma_signal, reading = 0.4, "MA20 au-dessus de MA50 mais le prix hésite entre les deux"
            elif ma20 < ma50:
                ma_signal, reading = -0.4, "MA20 sous MA50 mais le prix hésite entre les deux"
            else:
                ma_signal, reading = 0.0, "MA20 et MA50 confondues : range"
        else:
            ma_signal = 0.5 if price > ma20 else -0.5
            reading = f"prix {'au-dessus' if price > ma20 else 'sous'} la MA20 (MA50 indisponible)"
        if ma_slope is not None:
            m["ma20_slope_pct"] = round(ma_slope, 3)
            slope_txt = "montante" if ma_slope > 0.02 else "descendante" if ma_slope < -0.02 else "plate"
            ma_signal = 0.75 * ma_signal + 0.25 * max(-1.0, min(1.0, ma_slope * 20))
            reading += f", MA20 {slope_txt} ({ma_slope:+.2f} % sur 5 bougies)"
        add("ma", "MA 20 / MA 50",
            f"MA20 {_num(ma20)}" + (f" · MA50 {_num(ma50)}" if ma50 is not None else ""),
            reading, ma_signal, 0.30)

    # --- RSI 14 — poids 0,20 ---
    rsi_s = ind.rsi_series(closes, 14)
    rsi_val = next((v for v in reversed(rsi_s) if v is not None), None) if rsi_s else None
    if rsi_val is not None:
        m["rsi"] = round(rsi_val, 1)
        prev_rsi = next((v for v in reversed(rsi_s[:-1]) if v is not None), rsi_val)
        m["rsi_rising"] = rsi_val >= prev_rsi
        if rsi_val > 70:
            rsi_sig, reading = 0.2, "surachat (>70) : direction haussière mais mouvement étiré, entrée tardive"
        elif rsi_val > 55:
            rsi_sig, reading = 0.8, "momentum haussier franc (>55)"
        elif rsi_val < 30:
            rsi_sig, reading = -0.2, "survente (<30) : direction baissière mais mouvement étiré, rebond possible"
        elif rsi_val < 45:
            rsi_sig, reading = -0.8, "momentum baissier franc (<45)"
        else:
            rsi_sig = (rsi_val - 50) / 12.5
            reading = "zone neutre (45–55) : aucun camp ne domine, le RSI ne tranche pas"
        reading += f" — {'en hausse' if rsi_val >= prev_rsi else 'en baisse'} (précédent {prev_rsi:.0f})"
        add("rsi", "RSI 14", f"{rsi_val:.1f}", reading, rsi_sig, 0.20)

    # --- MACD — poids 0,18 ---
    macd_all = ind.macd_series(closes)
    hist_series: list[float] = []
    if macd_all is not None:
        line, sig, hist_series = macd_all
        hist = hist_series[-1]
        m["macd"] = {"line": round(line[-1], 6), "signal": round(sig[-1], 6), "hist": round(hist, 6)}
        rising = len(hist_series) > 1 and hist > hist_series[-2]
        strengthening = rising == (hist > 0)
        macd_sig = (1.0 if hist > 0 else -1.0) * (1.0 if strengthening else 0.5)
        dynamic = "qui se renforce" if strengthening else "qui s'essouffle → vote réduit de moitié"
        sense = "positif (haussier)" if hist > 0 else "négatif (baissier)"
        reading = f"histogramme {sense}, {dynamic}"
        add("macd", "MACD", f"histogramme {hist:+.6g}", reading, macd_sig, 0.18)

    # --- Tendance VWAP — poids 0,16 ---
    vwap_s = ind.rolling_vwap(candles, 20)
    vwap_val = next((v for v in reversed(vwap_s) if v is not None), None)
    if vwap_val is not None:
        m["vwap"] = round(vwap_val, 6)
        vslope = ind.slope_pct(vwap_s, 5)
        above = price > vwap_val
        v_sig = 1.0 if above else -1.0
        reading = f"prix {'au-dessus' if above else 'sous'} le VWAP"
        if vslope is not None:
            m["vwap_slope_pct"] = round(vslope, 3)
            slope_txt = ("orienté à la hausse" if vslope > 0.01
                         else "orienté à la baisse" if vslope < -0.01 else "plat (flux sans direction)")
            v_sig = 0.7 * v_sig + 0.3 * max(-1.0, min(1.0, vslope * 20))
            reading += f", VWAP {slope_txt} ({vslope:+.2f} %)"
        add("vwap", "VWAP (niveau + tendance)", _num(vwap_val), reading, v_sig, 0.16)
    else:
        m["vwap"] = None

    # --- Structure de marché — poids 0,16 ---
    st_bias, st_txt = structure_bias(candles)
    m["structure"] = st_txt
    add("structure", "Structure de marché",
        {1: "sommets/creux ascendants", -1: "sommets/creux descendants"}.get(st_bias, "non alignés"),
        st_txt, float(st_bias), 0.16)

    # --- Agrégation des votes pondérés ---
    total_w = sum(f["weight"] for f in factors) or 1.0
    for f in factors:
        f["contribution"] = round(f["signal"] * f["weight"] / total_w, 3)
        f["weight_pct"] = round(f["weight"] / total_w * 100)
    base = sum(f["signal"] * f["weight"] for f in factors) / total_w

    # --- Divergences RSI et MACD : AJUSTEMENT (contre-signal), pas un vote pondéré ---
    rsi_div = ind.divergence(candles, rsi_s) if rsi_s else None
    macd_div = ind.divergence(candles, hist_series) if hist_series else None
    m["rsi_divergence"] = rsi_div
    m["macd_divergence"] = macd_div
    div_adj = 0.0
    div_details: list[str] = []
    for name, div in (("RSI", rsi_div), ("MACD", macd_div)):
        if div:
            step = 0.18 if div == "haussière" else -0.18
            div_adj += step
            div_details.append(f"divergence {div} {name} ({step:+.2f})")
    if div_details:
        div_signal = max(-1.0, min(1.0, div_adj / 0.36))
        factors.append({
            "key": "divergence", "label": "Divergences RSI / MACD", "value": ", ".join(div_details),
            "reading": "le mouvement en cours n'est plus confirmé par l'oscillateur",
            "signal": round(div_signal, 3), "weight": 0.0, "weight_pct": 0,
            "score": to_five(div_signal), "reliability": reliability_label(to_five(div_signal)),
            "contribution": round(div_adj, 3), "verdict": _verdict(div_adj),
            "explain": GLOSSARY["divergence"],
        })

    # --- Volume : MULTIPLICATEUR de conviction (jamais directionnel) ---
    rel_vol = ind.relative_volume(candles, 20)
    vol_factor = 1.0
    if rel_vol is not None:
        m["relative_volume"] = round(rel_vol, 2)
        if rel_vol >= 1.5:
            vol_factor, reading = 1.15, f"volume {rel_vol:.1f}× la moyenne 20 : mouvement soutenu (+15 %)"
        elif rel_vol >= 1.0:
            vol_factor, reading = 1.05, f"volume normal ({rel_vol:.1f}×) : mouvement crédible (+5 %)"
        else:
            vol_factor, reading = 0.85, f"volume faible ({rel_vol:.1f}×) : mouvement peu soutenu (−15 %)"
        factors.append({
            "key": "volume", "label": "Volume relatif", "value": f"{rel_vol:.2f}× la moyenne 20",
            "reading": reading, "signal": 0.0, "weight": 0.0, "weight_pct": 0,
            "score": 0, "reliability": "confirme (sans direction propre)",
            "contribution": 0.0, "multiplier": vol_factor, "verdict": "multiplicateur",
            "explain": GLOSSARY["volume"],
        })
    else:
        factors.append({
            "key": "volume", "label": "Volume relatif", "value": "indisponible",
            "reading": "marché sans volume centralisé (forex spot) : le volume ne peut rien valider ici",
            "signal": 0.0, "weight": 0.0, "weight_pct": 0, "contribution": 0.0,
            "score": 0, "reliability": "non mesurable sur ce marché",
            "multiplier": 1.0, "verdict": "multiplicateur", "explain": GLOSSARY["volume"],
        })

    # --- Fibonacci si correction : contexte, jamais un vote ---
    fib_ctx = fibonacci_context(candles)
    if fib_ctx:
        m["fibonacci"] = fib_ctx
        if fib_ctx["invalidated"]:
            reading = (f"swing {fib_ctx['swing']} effacé à {fib_ctx['retracement_pct']} % : "
                       "le scénario ne tient plus")
        elif fib_ctx["golden_zone"]:
            reading = (f"correction de {fib_ctx['retracement_pct']} % : prix dans la ZONE D'OR "
                       f"(38,2–61,8 %) du swing {fib_ctx['swing']} → zone de reprise, entrée privilégiée")
        elif fib_ctx["in_correction"]:
            reading = (f"correction en cours ({fib_ctx['retracement_pct']} % du swing "
                       f"{fib_ctx['swing']}), pas encore dans la zone d'or")
        else:
            reading = (f"retracement de seulement {fib_ctx['retracement_pct']} % : pas de correction, "
                       "Fibonacci ne s'applique pas ici")
        factors.append({
            "key": "fibonacci", "label": "Fibonacci (si correction)",
            "value": f"retracement {fib_ctx['retracement_pct']} % du swing {fib_ctx['swing']}",
            "reading": reading, "signal": 0.0, "weight": 0.0, "weight_pct": 0,
            "contribution": 0.0, "score": 0, "reliability": "situe la zone d'entrée",
            "verdict": "contexte", "explain": GLOSSARY["fibonacci"],
        })

    # --- Contexte : ATR / ADX ---
    atr_val = ind.atr(candles, 14)
    if atr_val:
        m["atr"] = round(atr_val, 8)
        m["atr_pct"] = round(atr_val / price * 100, 3) if price else 0.0
    adx_val = ind.adx(candles, 14)
    if adx_val is not None:
        m["adx"] = round(adx_val, 1)

    # --- Score final + explication arithmétique complète ---
    score = max(-1.0, min(1.0, (base + div_adj) * vol_factor))
    voting = [f for f in factors if f["weight"] > 0]
    breakdown = {
        "votes": [{"label": f["label"], "signal": f["signal"], "weight_pct": f["weight_pct"],
                   "contribution": f["contribution"]} for f in voting],
        "sum_of_votes": round(base, 3),
        "divergence_adjustment": round(div_adj, 3),
        "volume_multiplier": vol_factor,
        "final": round(score, 3),
        "bias_threshold": MIN_LAYER_SCORE,
    }
    parts = " ".join(
        f"{f['label']} {f['contribution']:+.2f}" for f in voting
    )
    explanation = (
        f"Score {score:+.2f} ({score_strength(score)}) = somme des votes pondérés "
        f"[{parts}] = {base:+.2f}"
    )
    if div_adj:
        explanation += f", ajusté de {div_adj:+.2f} (divergences)"
    if vol_factor != 1.0:
        explanation += f", multiplié par {vol_factor:.2f} (volume)"
    explanation += (
        f" → {score:+.2f}. Il faut dépasser ±{MIN_LAYER_SCORE:.2f} pour parler de biais : "
        f"cette unité de temps est donc "
        f"{'HAUSSIÈRE' if score > MIN_LAYER_SCORE else 'BAISSIÈRE' if score < -MIN_LAYER_SCORE else 'NEUTRE'}."
    )
    notes = [f"{f['label']} : {f['reading']}" for f in factors]
    if symbol:
        m["symbol"] = symbol
    return Layer(label, score, _bias_of(score), notes, m, True, factors, breakdown, explanation)


# --------------------------------------------------------------------------------------
# Étape 1 — niveaux MAJEURS (mensuel + journalier)
# --------------------------------------------------------------------------------------
def major_levels(monthly: list[Candle], daily: list[Candle], price: float) -> dict:
    """Supports / résistances MAJEURS, fixés sur le mensuel puis affinés sur le journalier.

    « Majeur » veut dire *que tout le marché regarde* : sommets et creux de swing MENSUELS, plus
    les extrêmes journaliers de référence (120 jours ≈ 6 mois, et 20 jours ≈ 1 mois). On ne
    retient délibérément PAS chaque micro-swing journalier : un niveau que personne ne surveille
    n'est pas un niveau majeur, et l'empiler ici reviendrait à bloquer tous les objectifs.
    """
    candidates: list[tuple[float, str]] = []
    if len(monthly) >= 6:
        sw = ind.swing_points(monthly[-36:], 1, 1)
        candidates += [(p, "mensuel") for _, p in sw["highs"]]
        candidates += [(p, "mensuel") for _, p in sw["lows"]]
        window = monthly[-24:]
        candidates.append((max(c.high for c in window), "mensuel"))
        candidates.append((min(c.low for c in window), "mensuel"))
    for lookback, tag in ((120, "journalier (6 mois)"), (20, "journalier (1 mois)")):
        if len(daily) >= min(lookback, 30):
            window = daily[-lookback:]
            candidates.append((max(c.high for c in window), tag))
            candidates.append((min(c.low for c in window), tag))

    above = sorted([c for c in candidates if c[0] > price * 1.0005], key=lambda c: c[0])
    below = sorted([c for c in candidates if c[0] < price * 0.9995], key=lambda c: c[0], reverse=True)
    resistance = above[0] if above else None
    support = below[0] if below else None
    return {
        "major_resistance": round(resistance[0], 6) if resistance else None,
        "resistance_source": resistance[1] if resistance else None,
        "major_support": round(support[0], 6) if support else None,
        "support_source": support[1] if support else None,
        "all_resistances": [round(p, 6) for p, _ in above[:4]],
        "all_supports": [round(p, 6) for p, _ in below[:4]],
    }


# --------------------------------------------------------------------------------------
# Étape 4 — déclencheur d'entrée en 15 minutes (la SEULE unité de temps d'entrée)
# --------------------------------------------------------------------------------------
def entry_trigger(candles: list[Candle], direction: int, layer: Layer,
                  *, allow_divergence: bool = False) -> dict:
    """Cherche un déclencheur d'entrée 15 min dans le sens du biais confirmé.

    Déclencheurs valides, dans l'ordre de qualité MESURÉE par le backtest :
    1. **Repli** sur MA20/MA50 ou zone d'or Fibonacci + bougie de reprise + RSI qui se retourne
       (58 % de réussite, +0,77 R).
    2. **Cassure** du dernier swing, confirmée par le volume et le VWAP (69 %, +1,15 R — le meilleur).
    3. **Divergence** RSI/MACD + bougie de reprise (37,5 %, +0,19 R) — désactivée par défaut,
       cf. `allow_divergence`.
    Refus si le RSI 15 min est déjà en zone d'épuisement dans notre sens (entrée trop tardive).
    """
    out: dict = {"fired": False, "type": None, "reason": "", "notes": []}
    if len(candles) < 40 or direction == 0:
        out["reason"] = "données 15 min insuffisantes"
        return out

    closes = [c.close for c in candles]
    last, prev = candles[-1], candles[-2]
    price = last.close
    atr = ind.atr(candles, 14) or (price * 0.001)
    m = layer.metrics
    ma20, ma50 = m.get("ma20"), m.get("ma50")
    rsi_s = ind.rsi_series(closes, 14)
    rsi = m.get("rsi")
    rsi_prev = next((v for v in reversed(rsi_s[:-1]) if v is not None), rsi) if rsi_s else rsi
    vwap_val = m.get("vwap")
    rel_vol = m.get("relative_volume")
    bull = direction > 0

    # Garde-fou : ne jamais entrer en zone d'épuisement (RSI extrême dans notre sens).
    if rsi is not None and ((bull and rsi > 78) or (not bull and rsi < 22)):
        out["reason"] = f"RSI 15 min en zone d'épuisement ({rsi:.0f}) — entrée trop tardive"
        return out

    reprise = (last.close > last.open) if bull else (last.close < last.open)
    rsi_turn = (rsi is not None and rsi_prev is not None and
                ((bull and rsi > rsi_prev) or (not bull and rsi < rsi_prev)))

    # 1) Repli sur MA20/MA50 ou zone d'or Fibonacci.
    zone_touch = None
    for name, level in (("MA20", ma20), ("MA50", ma50)):
        if level is None:
            continue
        recent_extreme = min(c.low for c in candles[-3:]) if bull else max(c.high for c in candles[-3:])
        if abs(recent_extreme - level) <= 0.6 * atr:
            zone_touch = name
            break
    fib_ctx = m.get("fibonacci") or {}
    if zone_touch is None and fib_ctx.get("golden_zone") and not fib_ctx.get("invalidated"):
        swing_ok = (fib_ctx.get("swing") == "haussier") == bull
        if swing_ok:
            zone_touch = "zone d'or Fibonacci"
    if zone_touch and reprise and rsi_turn:
        out.update(fired=True, type="repli", reason=f"repli sur {zone_touch} + bougie de reprise + RSI qui se retourne")

    # 2) Cassure confirmée (volume + VWAP).
    if not out["fired"]:
        sw = ind.swing_points(candles[-40:], 2, 2)
        pts = sw["highs"] if bull else sw["lows"]
        if pts:
            level = pts[-1][1]
            broken = (price > level) if bull else (price < level)
            vol_ok = rel_vol is None or rel_vol >= 1.2
            vwap_ok = vwap_val is None or ((price > vwap_val) if bull else (price < vwap_val))
            if broken and reprise and vol_ok and vwap_ok and prev.close != price:
                out.update(fired=True, type="cassure",
                           reason=f"cassure du dernier swing {'haut' if bull else 'bas'} "
                                  f"({level:.6g}) confirmée par le volume et le VWAP")

    # 3) Divergence en notre faveur — DÉSACTIVÉE par défaut comme déclencheur d'entrée.
    # Mesure du backtest : 37,5 % de réussite et +0,19 R sur 16 trades, contre 69 % / +1,15 R pour
    # la cassure. Elle ne détruit pas de capital mais dilue l'espérance ; elle reste calculée et
    # affichée, car une divergence CONTRAIRE garde toute sa valeur d'avertissement.
    if not out["fired"] and allow_divergence:
        want = "haussière" if bull else "baissière"
        if (m.get("rsi_divergence") == want or m.get("macd_divergence") == want) and reprise:
            out.update(fired=True, type="divergence",
                       reason=f"divergence {want} (RSI/MACD) + bougie de reprise")

    if not out["fired"]:
        out["reason"] = "aucun déclencheur 15 min (ni repli qualifié, ni cassure confirmée, ni divergence)"
        return out

    # --- Stop technique : sous/au-dessus du dernier creux/sommet 15 min, avec marge ATR ---
    if bull:
        swing_stop = min(c.low for c in candles[-10:])
        stop = min(swing_stop, price - 1.0 * atr) - 0.2 * atr
        if price - stop < 0.6 * atr:
            stop = price - 0.6 * atr
    else:
        swing_stop = max(c.high for c in candles[-10:])
        stop = max(swing_stop, price + 1.0 * atr) + 0.2 * atr
        if stop - price < 0.6 * atr:
            stop = price + 0.6 * atr

    out["entry"] = price
    out["stop"] = stop
    out["atr"] = atr
    return out


def _arguments(layer: Layer) -> list[str]:
    """Liste lisible des arguments d'une unité de temps, chacun avec son score de fiabilité."""
    lines: list[str] = []
    for f in layer.factors:
        if f["weight"] > 0:
            lines.append(
                f"   • {f['label']} ({f['value']}) : {f['reading']}. "
                f"Score {fmt_score(f['score'])} — {f['reliability']}."
            )
        else:
            lines.append(f"   • {f['label']} ({f['value']}) : {f['reading']}.")
    return lines


def narrate(
    setup: PlaybookSetup,
    layers: dict[str, Layer],
    *,
    trend_sense: str,
    day_ok: bool,
    h4_ok: bool,
    trigger: dict,
    min_rr: float,
    max_rr: float,
    min_target_pips: float,
) -> str:
    """Rédige l'explication COMPLÈTE de la décision, en français, sans aucun score technique brut.

    Sept sections qui suivent la stratégie pas à pas, chaque argument étant noté de -5 à +5, puis
    une conclusion qui donne le score de fiabilité total du trade.
    """
    l_month, l_day, l_h4, l_m15 = layers["monthly"], layers["daily"], layers["h4"], layers["m15"]
    lv = setup.levels or {}
    sess = setup.session or {}
    decision = {"BUY": "ACHAT", "SELL": "VENTE"}.get(setup.direction, "PAS DE TRADE (attente)")
    out: list[str] = [f"DÉCISION : {decision} sur {setup.symbol}", ""]

    # 1 — Tendance de fond
    out.append(f"1) LA TENDANCE DE FOND EST {trend_sense}")
    out.append(
        "   On commence toujours par le haut : sans tendance de fond claire sur le mensuel et le "
        "journalier, on ne prend aucune position. Voici ce que disent ces deux unités de temps."
    )
    out.append(f"   Mensuel — lecture {'haussière' if l_month.bias > 0 else 'baissière' if l_month.bias < 0 else 'neutre'} "
               f"(fiabilité {fmt_score(l_month.score_5)}) :")
    out += _arguments(l_month)
    out.append(f"   Journalier — lecture {'haussière' if l_day.bias > 0 else 'baissière' if l_day.bias < 0 else 'neutre'} "
               f"(fiabilité {fmt_score(l_day.score_5)}) :")
    out += _arguments(l_day)
    out.append("")

    # 2 — Niveaux majeurs
    out.append("2) LES NIVEAUX MAJEURS QUI ENCADRENT LE TRADE")
    if lv.get("major_support") or lv.get("major_resistance"):
        out.append(
            f"   Le support majeur se situe à {lv.get('major_support')} ({lv.get('support_source')}) et "
            f"la résistance majeure à {lv.get('major_resistance')} ({lv.get('resistance_source')}). "
            "Ces niveaux viennent des sommets et creux mensuels et des extrêmes journaliers de "
            "référence : ce sont ceux que tout le marché surveille. Ils bornent l'objectif, car "
            "viser au-delà d'un niveau majeur suppose que le prix traverse une zone défendue."
        )
    else:
        out.append("   Aucun niveau majeur exploitable n'a pu être identifié : on ne sait pas où le "
                   "mouvement peut s'arrêter, donc on s'abstient.")
    out.append("")

    # 3 — Le journalier confirme ?
    out.append("3) EST-CE QUE LE JOURNALIER CONFIRME ?")
    if day_ok:
        out.append("   Oui. Le journalier va dans le même sens que la tendance de fond, avec les "
                   "arguments listés au point 1. La direction est donc cohérente d'une unité de "
                   "temps à l'autre.")
    else:
        out.append("   Non. Le journalier ne va pas dans le sens de la tendance de fond. Prendre "
                   "position ici, ce serait acheter (ou vendre) alors que l'unité de temps qui "
                   "commande le mouvement du jour dit l'inverse. On attend qu'elle se réaligne.")
    out.append("")

    # 4 — Le 4 h confirme ?
    out.append("4) EST-CE QUE LE 4 HEURES CONFIRME ?")
    out.append(f"   Lecture du 4 h : {'haussière' if l_h4.bias > 0 else 'baissière' if l_h4.bias < 0 else 'neutre'} "
               f"(fiabilité {fmt_score(l_h4.score_5)}).")
    out += _arguments(l_h4)
    out.append("   Oui, le 4 h confirme : c'est la deuxième validation exigée avant de chercher une "
               "entrée." if h4_ok else
               "   Non, le 4 h ne confirme pas. Le 4 h est l'unité de temps qui décide de la journée : "
               "tant qu'il n'est pas retourné dans notre sens, l'entrée est prématurée.")
    out.append("")

    # 5 — Le déclencheur 15 min
    out.append("5) LE DÉCLENCHEUR D'ENTRÉE EN 15 MINUTES")
    out.append(
        "   L'entrée ne se prend QUE en 15 minutes. Les unités supérieures donnent la direction, le "
        "15 min donne le moment. Trois déclencheurs sont acceptés : un repli sur la MA20/MA50 ou "
        "dans la zone d'or Fibonacci avec une bougie de reprise, une cassure confirmée par le "
        "volume et le VWAP, ou une divergence en notre faveur."
    )
    if trigger.get("fired"):
        out.append(f"   Déclencheur trouvé : {trigger['reason']}.")
    else:
        out.append(f"   Aucun déclencheur pour l'instant : {trigger.get('reason', '—')}. "
                   "C'est la raison principale de l'attente : le contexte peut être parfait, sans "
                   "signal d'entrée on ne rentre pas.")
    out.append(f"   État du 15 min : {'haussier' if l_m15.bias > 0 else 'baissier' if l_m15.bias < 0 else 'neutre'} "
               f"(fiabilité {fmt_score(l_m15.score_5)}).")
    out += _arguments(l_m15)
    out.append("")

    # 6 — Risque et objectif
    out.append("6) LE RISQUE ET L'OBJECTIF — STOP 15 MIN, OBJECTIF BORNÉ PAR LE 1 H")
    if setup.ready:
        out.append(
            f"   Entrée à {setup.entry:.6g}, stop à {setup.stop_loss:.6g} "
            f"({setup.risk_pips:.1f} {setup.pips_label}), premier objectif à {setup.take_profit_1:.6g} "
            f"({setup.reward_pips:.1f} {setup.pips_label}). Le rapport risque/rendement est de "
            f"1 pour {setup.risk_reward:.2f}, dans la bande imposée de {min_rr:g} à {max_rr:g}."
        )
        out.append(
            f"   Le stop vient de la {setup.stop_basis}. C'est un choix de méthode : l'entrée se "
            f"prend en 15 minutes, donc c'est le 15 minutes qui dit quand elle est fausse. Un stop "
            f"emprunté à une unité de temps supérieure ferait vivre le risque sur une échelle que "
            f"le déclencheur ne surveille pas."
        )
        if setup.target_level is not None:
            out.append(
                f"   L'objectif, lui, est {setup.target_basis}. Le prochain niveau horaire se situe "
                f"à {setup.target_level:.6g} : on sort DEVANT lui, pas derrière. Viser au-delà "
                f"reviendrait à parier que le prix traverse un niveau que le marché défend."
            )
        else:
            out.append(f"   L'objectif est calculé ainsi : {setup.target_basis}.")
        if setup.horizon_label:
            out.append(
                f"   Compte tenu de l'amplitude moyenne d'une bougie 15 minutes sur cet actif, "
                f"atteindre cet objectif demande environ {setup.horizon_label} de marché ouvert : "
                f"ce trade est un SWING, il se tient plusieurs séances."
            )
    else:
        floor_txt = (f" L'objectif ne peut pas descendre sous {min_target_pips:.0f} "
                     f"{setup.pips_label}." if min_target_pips else "")
        out.append(f"   Aucun niveau n'est proposé puisqu'il n'y a pas d'entrée. Pour information, "
                   f"la stratégie exige un rapport risque/rendement entre {min_rr:g} et {max_rr:g}, "
                   f"un stop pris sur la structure 15 minutes et un objectif borné par le prochain "
                   f"niveau 1 heure.{floor_txt}")
    out.append("")

    # 7 — Le moment de la journée
    out.append("7) LE MOMENT DE LA JOURNÉE")
    out.append(
        f"   Fenêtre actuelle : {sess.get('label', 'non évaluée')} ({sess.get('utc_time', '—')}). "
        + ("C'est une fenêtre à forte valeur : le volume institutionnel est présent, les mouvements "
           "sont directionnels." if sess.get("prime") else
           "Ce n'est pas une fenêtre à forte valeur : la liquidité est plus faible, les mouvements "
           "moins fiables. La conviction est donc réduite.")
    )
    nxt = sess.get("next_window")
    if nxt and not sess.get("prime"):
        out.append(f"   Prochaine fenêtre à surveiller : {nxt['label']}, dans "
                   f"{nxt['starts_in_minutes']} minutes ({nxt['window_utc']}).")
    out.append("")

    # Conclusion
    out.append("CONCLUSION")
    score = setup.reliability_score
    if setup.direction == "NO_TRADE":
        why = " ; ".join(setup.reasons) if setup.reasons else "conditions non réunies"
        out.append(
            f"   Pas de trade pour l'instant. Ce qui bloque : {why}. S'abstenir est une décision de "
            f"trading à part entière : on ne force pas une position quand la méthode n'est pas "
            f"entièrement satisfaite."
        )
        out.append("   SCORE DE FIABILITÉ DU TRADE : 0/5 — aucun signal exploitable.")
        if setup.context_ok:
            out.append(
                f"   En revanche le CONTEXTE, lui, est noté "
                f"{fmt_score(setup.context_reliability)} — "
                f"{trade_reliability_label(setup.context_reliability)} : les étapes 1 à 3 sont "
                f"validées et le setup est ARMÉ. Il sera pris AUTOMATIQUEMENT, sans aucune action "
                f"de ta part, à la seconde où le déclencheur 15 minutes se formera."
            )
    else:
        out.append(
            f"   Tous les points de la méthode sont réunis dans le sens {decision.lower()} : tendance "
            f"de fond, confirmation journalière, confirmation 4 h, déclencheur 15 min, et un risque "
            f"encadré. C'est pour cela que la décision est {decision}."
        )
        out.append(
            f"   SCORE DE FIABILITÉ DU TRADE : {fmt_score(score)} — {trade_reliability_label(score)}. "
            "Un score élevé signifie que les unités de temps s'accordent et que la fenêtre est "
            "favorable ; il ne garantit jamais le résultat."
        )
    return "\n".join(out)


def structural_stop(h4: list[Candle], bias: int, entry: float, min_distance: float) -> float:
    """Stop recalé sur la STRUCTURE 4 H, garanti à au moins `min_distance` de l'entrée.

    Nécessaire dès que l'objectif est ambitieux (200 pips) : un stop de 5 pips issu du bruit 15 min
    donnerait un R/R de 40:1 que le marché ne paie jamais — la position serait stoppée avant d'avoir
    commencé à travailler. L'objectif et le stop doivent vivre sur la même échelle de temps ; le
    15 min, lui, ne sert qu'à choisir le MOMENT de l'entrée.
    """
    atr4 = ind.atr(h4, 14) or min_distance
    window = h4[-10:] if len(h4) >= 10 else h4
    if not window:
        return entry - min_distance if bias > 0 else entry + min_distance
    if bias > 0:
        candidate = min(c.low for c in window) - 0.3 * atr4
        return min(candidate, entry - min_distance)
    candidate = max(c.high for c in window) + 0.3 * atr4
    return max(candidate, entry + min_distance)


def cluster_levels(levels: list[float], tolerance: float) -> list[float]:
    """Fusionne les niveaux quasi identiques : un même niveau vu deux fois n'est qu'UN niveau.

    Sans ce regroupement, un support touché trois fois compterait trois fois et fausserait le choix
    du stop (on croirait avoir trois barrières là où le marché n'en voit qu'une).
    """
    if not levels:
        return []
    ordered = sorted(levels)
    merged = [ordered[0]]
    for lvl in ordered[1:]:
        if abs(lvl - merged[-1]) <= tolerance:
            merged[-1] = (merged[-1] + lvl) / 2      # centre du groupe
        else:
            merged.append(lvl)
    return merged


def entry_structure(
    m15: list[Candle], h1: list[Candle] | None, entry: float, *,
    strength: int = 3, lookback: int = 200,
) -> dict:
    """Supports et résistances lus sur le 15 MIN et le 1 H — ceux qui encadrent réellement l'entrée.

    Ce sont les niveaux que le trader voit sur son écran au moment où il entre : les creux et
    sommets de swing confirmés des deux unités de temps les plus fines de la méthode. Ils servent à
    POSER le stop (juste derrière un support) et l'objectif (juste devant une résistance), au lieu
    de les placer à une distance calculée qui ne correspond à rien sur le graphique.

    Retourne ``{"supports": [... décroissant], "resistances": [... croissant], "tolerance"}``.
    """
    sources = [(m15, "15 min")]
    if h1:
        sources.append((h1, "1 h"))
    highs: list[float] = []
    lows: list[float] = []
    atrs: list[float] = []
    for candles, _label in sources:
        if not candles or len(candles) < 20:
            continue
        window = candles[-lookback:] if len(candles) > lookback else candles
        sw = ind.swing_points(window, strength, strength)
        highs += [p for _, p in sw["highs"]]
        lows += [p for _, p in sw["lows"]]
        atr = ind.atr(window, 14)
        if atr:
            atrs.append(atr)
    # Tolérance de regroupement : une fraction de l'ATR le plus fin disponible.
    tolerance = (min(atrs) * 0.5) if atrs else (entry * 0.0002)
    supports = [lvl for lvl in cluster_levels(lows, tolerance) if lvl < entry]
    resistances = [lvl for lvl in cluster_levels(highs, tolerance) if lvl > entry]
    return {
        "supports": sorted(supports, reverse=True),    # du plus proche au plus lointain
        "resistances": sorted(resistances),            # idem
        "tolerance": tolerance,
        "atr": min(atrs) if atrs else 0.0,
    }


def stop_behind_level(
    structure: dict, entry: float, bias: int, *,
    min_distance: float, max_distance: float, buffer_atr: float = 0.25,
) -> tuple[float, str] | None:
    """Place le stop JUSTE DERRIÈRE le support (ou la résistance) 15 min / 1 h le plus adapté.

    On retient le niveau le plus PROCHE qui respecte encore la distance minimale exigée par la
    stratégie : c'est le stop le plus serré qui ait un sens de marché, donc le meilleur R/R
    possible sans placer la sortie au milieu de nulle part. Retourne ``None`` si aucun niveau ne
    tombe dans la plage acceptable — on retombe alors sur la structure 4 h.
    """
    buffer = buffer_atr * (structure.get("atr") or 0.0)
    candidates = structure["supports"] if bias > 0 else structure["resistances"]
    for lvl in candidates:                       # déjà triés du plus proche au plus lointain
        price = lvl - buffer if bias > 0 else lvl + buffer
        distance = abs(entry - price)
        if min_distance <= distance <= max_distance:
            side = "support" if bias > 0 else "résistance"
            return price, f"{side} {lvl:.6g} du 15 min / 1 h (stop placé juste derrière)"
    return None


def target_before_level(
    structure: dict, entry: float, bias: int, *,
    min_distance: float, max_distance: float, buffer_atr: float = 0.25,
) -> tuple[float, str] | None:
    """Place l'objectif JUSTE DEVANT la première résistance (ou support) qui offre la distance visée.

    On vise le PREMIER niveau situé au-delà de la distance minimale : viser plus loin supposerait
    que le prix traverse ce niveau-là, ce qui est un pari supplémentaire qu'on ne prend pas.
    Retourne ``None`` si aucun niveau ne convient — l'objectif reste alors purement arithmétique.
    """
    buffer = buffer_atr * (structure.get("atr") or 0.0)
    candidates = structure["resistances"] if bias > 0 else structure["supports"]
    for lvl in candidates:
        price = lvl - buffer if bias > 0 else lvl + buffer
        distance = abs(price - entry)
        if distance < min_distance:
            continue                              # niveau trop proche : on le franchira
        if distance > max_distance:
            return None                           # le premier niveau utile est déjà hors bande
        side = "résistance" if bias > 0 else "support"
        return price, f"{side} {lvl:.6g} du 15 min / 1 h (objectif placé juste devant)"
    return None


def volatility_adjustment(
    daily: list[Candle], entry: float, bias: int, stop: float, *,
    max_atr_pct: float = MAX_ATR_PCT, mode: str = "adapt",
    max_widen: float = VOLATILITY_MAX_WIDEN, enabled: bool = True,
) -> dict:
    """Que faire quand la volatilité journalière dépasse le seuil mesuré comme dangereux ?

    Constat du backtest : les trades stoppés ont un ATR journalier supérieur de 21 % à celui des
    gagnants (1,39 % contre 1,15 %). Tous les autres facteurs — ADX, alignement des unités de temps,
    confiance, largeur relative du stop — sont identiques. Autrement dit, la stratégie ne se trompe
    pas de direction : elle se fait sortir par le bruit quand le marché s'agite.

    Deux réponses possibles, toutes deux défendables :
    - ``adapt``  : on ÉLARGIT le stop proportionnellement à l'excès de volatilité. On garde le trade
      et on paie le vrai prix du risque. L'objectif suivant automatiquement (R/R × risque), la
      contrainte des 200 pips reste satisfaite et la taille de position se réduit d'elle-même.
    - ``refuse`` : on n'entre pas au-dessus du seuil. Plus simple, mais on renonce à des mouvements
      qui sont précisément ceux qui paient le mieux quand ils partent dans le bon sens.

    Retourne ``{action, reason, atr_pct, ratio, stop}`` avec action ∈ {none, widen, refuse}.
    """
    atr_daily = ind.atr(daily, 14) or 0.0
    atr_pct = (atr_daily / entry * 100) if entry else 0.0
    base = {"atr_pct": round(atr_pct, 3), "threshold": max_atr_pct, "stop": stop, "ratio": 1.0}
    if not enabled or atr_pct <= 0 or atr_pct <= max_atr_pct:
        return {**base, "action": "none",
                "reason": f"volatilité journalière {atr_pct:.2f} % ≤ seuil {max_atr_pct:g} %"}

    ratio = min(atr_pct / max_atr_pct, max_widen)
    if mode == "refuse":
        return {**base, "action": "refuse", "ratio": round(ratio, 2),
                "reason": (f"volatilité journalière {atr_pct:.2f} % au-dessus du seuil "
                           f"{max_atr_pct:g} % — c'est le profil des trades qui se font stopper")}
    widened = entry - ratio * abs(entry - stop) if bias > 0 else entry + ratio * abs(entry - stop)
    return {
        **base, "action": "widen", "stop": widened, "ratio": round(ratio, 2),
        "reason": (f"stop élargi ×{ratio:.2f} (volatilité journalière {atr_pct:.2f} % contre un "
                   f"seuil de {max_atr_pct:g} %)"),
    }


def secured_stop(entry: float, stop_loss: float, direction: str, *, at_r: float = SECURE_AT_R) -> float:
    """Où placer le stop une fois `at_r` × le risque parcouru — la position ne peut plus perdre.

    C'est la règle demandée : un trade qui a atteint +2R voit son stop remonté SUR +2R, ce qui
    verrouille ce gain, puis on le laisse courir vers le R/R maximum. Le stop ne recule jamais.
    """
    risk = abs(entry - stop_loss)
    sign = 1 if str(direction).upper() in ("BUY", "LONG") else -1
    return entry + sign * at_r * risk


def target_barrier(
    candles: list[Candle], bias: int, entry: float,
    *, buffer_mult: float = TARGET_LEVEL_BUFFER, lookback: int = 120, strength: int = 3,
) -> dict | None:
    """Prochain niveau 1 H dans le sens du trade — c'est LUI qui borne l'objectif.

    « Niveau 1 h » = sommet/creux de swing horaire CONFIRMÉ (le pivot doit dominer `strength`
    bougies de chaque côté, soit 3 heures avant et 3 heures après), plus l'extrême de la fenêtre
    observée. La confirmation à 3 bougies est délibérée : un micro-pivot d'une seule heure n'est pas
    un niveau que le marché défend, et le retenir bloquerait tous les objectifs.

    On place l'objectif un peu AVANT le niveau (marge de `buffer_mult` × ATR 1 h) : sortir juste
    devant un niveau que tout le monde surveille est très différent de miser sur sa traversée.

    Retourne ``{"level", "target", "atr", "source"}`` ou ``None`` si aucun niveau n'est identifiable
    (dans ce cas l'objectif n'est borné que par les niveaux MAJEURS mensuels/journaliers).
    """
    if not candles or len(candles) < 20 or bias == 0:
        return None
    window = candles[-lookback:] if len(candles) > lookback else candles
    atr1 = ind.atr(window, 14) or 0.0
    buf = buffer_mult * atr1
    sw = ind.swing_points(window, strength, strength)
    # On retient TOUT niveau situé devant le prix, même collé à lui : un niveau à 2 pips au-dessus
    # de l'entrée n'est pas « aucun niveau », c'est un objectif impossible — et c'est au calcul de
    # marge de le refuser, pas à cette fonction de le masquer.
    if bias > 0:
        candidates = [p for _, p in sw["highs"] if p > entry]
        top = max(c.high for c in window)
        if top > entry:
            candidates.append(top)
        if not candidates:
            return None      # la route est libre : aucun sommet 1 h devant le prix
        level = min(candidates)
        return {"level": round(level, 8), "target": level - buf, "atr": atr1, "source": "swing 1 h"}
    candidates = [p for _, p in sw["lows"] if p < entry]
    bottom = min(c.low for c in window)
    if bottom < entry:
        candidates.append(bottom)
    if not candidates:
        return None
    level = max(candidates)
    return {"level": round(level, 8), "target": level + buf, "atr": atr1, "source": "swing 1 h"}


# --------------------------------------------------------------------------------------
# Assemblage complet
# --------------------------------------------------------------------------------------
@dataclass
class PlaybookSetup:
    symbol: str
    direction: str = "NO_TRADE"       # BUY | SELL | NO_TRADE
    bias: int = 0
    score: float = 0.0                # biais pour le Master [-1, +1]
    confidence: float = 0.0           # [0, 1]
    entry: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    risk_reward: float = 0.0
    trigger: str | None = None
    stop_basis: str = "structure 15 min"   # d'où vient le stop (toujours le 15 min)
    target_basis: str = ""                 # d'où vient l'objectif (borné ou non par un niveau)
    target_level: float | None = None      # niveau journalier qui borne l'objectif
    # Niveau où le stop sera automatiquement remonté dès qu'il est atteint (+2R) : à partir de là
    # la position ne peut plus redevenir perdante.
    secure_stop: float | None = None
    # Décision du filtre de volatilité (none / widen / refuse) et les chiffres qui l'ont motivée.
    volatility: dict = field(default_factory=dict)
    # Supports / résistances 15 min et 1 h qui encadrent l'entrée — ceux qui posent le SL et le TP.
    entry_levels: dict = field(default_factory=dict)
    horizon_days: float | None = None      # durée estimée pour atteindre l'objectif (ATR journalier)
    horizon_hours: float | None = None     # idem en heures (l'échelle naturelle d'un trade 15 min)
    horizon_label: str = ""                # « ~3 h », « ~2 j » — prêt à afficher
    ready: bool = False               # déclencheur d'entrée actif MAINTENANT
    context_ok: bool = False          # étapes 1-3 validées (tendance + niveaux + journalier + 4h)
    insufficient: bool = False        # données trop pauvres -> ne pas opposer de veto
    checklist: list[dict] = field(default_factory=list)
    layers: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    session: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    pips_label: str = "pips"
    trend_explanation: str = ""       # comment la tendance de fond a été établie (étape 1)
    # Score de fiabilité AFFICHÉ : +1..+5 pour un achat, -1..-5 pour une vente, 0 = pas de trade.
    reliability_score: int = 0
    # Fiabilité du CONTEXTE (étapes 1-3) — renseignée même sans déclencheur 15 min. C'est elle qui
    # classe les setups ARMÉS entre eux : « 0/5 » sur un contexte parfaitement aligné serait faux.
    context_reliability: int = 0
    narrative: str = ""               # explication complète en français, sans score technique brut

    @property
    def veto(self) -> bool:
        """Vrai si la stratégie interdit le trade (et que les données étaient suffisantes)."""
        return self.direction == "NO_TRADE" and not self.insufficient

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction, "bias": self.bias,
            "score": round(self.score, 3), "confidence": round(self.confidence, 3),
            "strength": score_strength(self.score),
            "entry": self.entry, "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1, "take_profit_2": self.take_profit_2,
            "take_profit_3": self.take_profit_3,
            "risk_pips": round(self.risk_pips, 1), "reward_pips": round(self.reward_pips, 1),
            "risk_reward": round(self.risk_reward, 2), "pips_label": self.pips_label,
            "trigger": self.trigger, "stop_basis": self.stop_basis,
            "target_basis": self.target_basis, "target_level": self.target_level,
            "secure_stop": self.secure_stop, "secure_at_r": SECURE_AT_R,
            "volatility": self.volatility, "entry_levels": self.entry_levels,
            "horizon_days": self.horizon_days, "horizon_hours": self.horizon_hours,
            "horizon_label": self.horizon_label,
            "ready": self.ready, "veto": self.veto, "context_ok": self.context_ok,
            "insufficient": self.insufficient, "checklist": self.checklist,
            "levels": self.levels, "session": self.session, "reasons": self.reasons,
            "layers": self.layers, "trend_explanation": self.trend_explanation,
            "reliability_score": self.reliability_score,
            "reliability": trade_reliability_label(self.reliability_score),
            "context_reliability": self.context_reliability,
            "context_reliability_label": trade_reliability_label(self.context_reliability),
            "narrative": self.narrative,
        }

    def summary(self) -> str:
        """Résumé lisible façon note de desk."""
        if self.direction == "NO_TRADE":
            why = " ; ".join(self.reasons) or "conditions non réunies"
            return f"Playbook {self.symbol} — PAS DE TRADE : {why}."
        horizon = f" | horizon {self.horizon_label}" if self.horizon_label else ""
        return (
            f"Playbook {self.symbol} — {self.direction} @ {self.entry:.6g} | "
            f"SL {self.stop_loss:.6g} ({self.risk_pips:.0f} {self.pips_label}) | "
            f"TP1 {self.take_profit_1:.6g} ({self.reward_pips:.0f} {self.pips_label}) | "
            f"R/R 1:{self.risk_reward:.1f}{horizon} | déclencheur 15 min : {self.trigger}"
        )


def _check(step: int, label: str, ok: bool, value: str, explain: str = "") -> dict:
    return {"step": step, "label": label, "pass": bool(ok), "value": value, "explain": explain}


def build(
    symbol: str,
    monthly: list[Candle],
    daily: list[Candle],
    h4: list[Candle],
    m15: list[Candle],
    *,
    h1: list[Candle] | None = None,
    session: dict | None = None,
    min_rr: float = MIN_RR,
    min_target_pips: float = MIN_TARGET_PIPS,
    max_stop_pips: float = MAX_STOP_PIPS,
    target_level_buffer: float = TARGET_LEVEL_BUFFER,
    max_rr: float = MAX_RR,
    max_atr_multiple: float = MAX_ATR_MULTIPLE,
    can_trade: bool = True,
    allow_divergence: bool = False,
    volatility_filter: bool = True,
    volatility_mode: str = "adapt",
    max_atr_pct: float = MAX_ATR_PCT,
    volatility_max_widen: float = VOLATILITY_MAX_WIDEN,
    # Mode de POSE du stop : "structure" (défaut — derrière un niveau 15 min/1 h/4 h) ou "atr4h"
    # (stop à `stop_atr_mult` × ATR 4 h, borné par la bande de R/R). Le second n'existe que pour
    # l'A/B test volatilité du backtest : la production reste sur la structure tant que la mesure
    # n'a pas tranché.
    stop_mode: str = "structure",
    stop_atr_mult: float = 2.0,
) -> PlaybookSetup:
    """Exécute la stratégie de bout en bout et retourne le setup (ou un refus motivé).

    `h1` : bougies 1 h. C'est une véritable **étape de confirmation** (étape 4) : le 1 h doit aller
    dans le sens du biais avant qu'on aille chercher un déclencheur en 15 min. Sans bougies 1 h,
    l'étape est signalée comme non vérifiable dans la checklist et le contexte n'est pas validé.

    `can_trade` : faux quand les places de Londres ET de New York sont fermées. L'analyse est alors
    produite intégralement (c'est bien le but : analyser aussi marchés fermés), mais aucun setup
    n'est déclaré exécutable — on n'ouvre pas de position dans un marché illiquide.
    """
    setup = PlaybookSetup(symbol=symbol, session=session or {})
    checklist: list[dict] = []
    reasons: list[str] = []

    l_month = factor_layer(monthly, "mensuel", symbol)
    l_day = factor_layer(daily, "journalier", symbol)
    l_h4 = factor_layer(h4, "4h", symbol)
    l_h1 = factor_layer(h1 or [], "1h", symbol)
    l_m15 = factor_layer(m15, "15m", symbol)
    setup.layers = {k: v.as_dict() for k, v in (
        ("monthly", l_month), ("daily", l_day), ("h4", l_h4), ("h1", l_h1), ("m15", l_m15))}

    if not (l_month.ok and l_day.ok and l_h4.ok and l_h1.ok and l_m15.ok):
        missing = [layer.label for layer in (l_month, l_day, l_h4, l_h1, l_m15) if not layer.ok]
        setup.insufficient = True
        setup.reasons = [f"données insuffisantes sur : {', '.join(missing)}"]
        setup.checklist = [_check(0, "Données multi-unités de temps disponibles", False, ", ".join(missing))]
        return setup

    price = daily[-1].close
    pip = pips_mod.pip_size(symbol, price)
    setup.pips_label = pips_mod.label(symbol)

    # ---------------- Étape 1 : tendance de fond (mensuel + journalier) + niveaux majeurs ----------
    trend_score = 0.5 * l_month.score + 0.5 * l_day.score
    bias = _bias_of(trend_score)
    setup.bias = bias
    sens = "HAUSSIÈRE" if bias > 0 else "BAISSIÈRE" if bias < 0 else "INDÉTERMINÉE"
    setup.trend_explanation = (
        f"Tendance de fond {sens} : moyenne du mensuel ({l_month.score:+.2f}, "
        f"{score_strength(l_month.score)}) et du journalier ({l_day.score:+.2f}, "
        f"{score_strength(l_day.score)}) = {trend_score:+.2f}. "
        f"Il faut dépasser ±{MIN_LAYER_SCORE:.2f} pour engager un trade.\n"
        f"• Mensuel — {l_month.explanation}\n• Journalier — {l_day.explanation}"
    )
    checklist.append(_check(
        1, "Tendance de fond mensuelle + journalière établie", bias != 0,
        f"(mensuel {l_month.score:+.2f} + journalier {l_day.score:+.2f}) / 2 = {trend_score:+.2f} → {sens.lower()}",
        setup.trend_explanation,
    ))

    levels = major_levels(monthly, daily, price)
    setup.levels = levels
    has_levels = levels["major_support"] is not None or levels["major_resistance"] is not None
    checklist.append(_check(
        1, "Supports / résistances majeurs fixés", has_levels,
        f"support {levels['major_support']} ({levels['support_source']}) · "
        f"résistance {levels['major_resistance']} ({levels['resistance_source']})",
        "Niveaux issus des swings MENSUELS et des extrêmes journaliers 6 mois / 1 mois — ceux que "
        "tout le marché surveille. Ils bornent l'objectif : un trade dont la cible est derrière un "
        "niveau majeur est un piège.",
    ))

    if bias == 0:
        reasons.append("tendance de fond indéterminée (mensuel et journalier ne s'accordent pas)")
    if not has_levels:
        reasons.append("aucun niveau majeur exploitable")

    # ---------------- Étape 2 : le journalier doit CONFIRMER ----------------
    day_ok = l_day.bias == bias and bias != 0
    checklist.append(_check(
        2, "Journalier confirme (RSI14, MA20/MA50, volume, VWAP, divergences, Fibonacci)", day_ok,
        f"score {l_day.score:+.2f} ({score_strength(l_day.score)})",
        l_day.explanation,
    ))
    if bias != 0 and not day_ok:
        reasons.append("le journalier ne confirme pas la tendance de fond")

    # ---------------- Étape 3 : le 4h doit CONFIRMER ----------------
    h4_ok = l_h4.bias == bias and bias != 0
    checklist.append(_check(
        3, "4 h confirme (mêmes facteurs)", h4_ok,
        f"score {l_h4.score:+.2f} ({score_strength(l_h4.score)})",
        l_h4.explanation,
    ))
    if bias != 0 and not h4_ok:
        reasons.append("le 4 h ne confirme pas le biais")

    # ---------------- Étape 4 : le 1 h doit CONFIRMER (dernière confirmation) ----------------
    h1_ok = l_h1.bias == bias and bias != 0
    checklist.append(_check(
        4, "1 h confirme (dernière confirmation avant l'entrée)", h1_ok,
        f"score {l_h1.score:+.2f} ({score_strength(l_h1.score)})",
        "Le 1 h est la dernière unité de temps consultée avant de chercher un déclencheur. Elle "
        "filtre les cas où le 4 h est encore orienté dans notre sens alors que le mouvement s'est "
        "déjà retourné en dessous : on éviterait sinon d'entrer juste au moment où le marché "
        "bascule.\n" + l_h1.explanation,
    ))
    if bias != 0 and not h1_ok:
        reasons.append("le 1 h ne confirme pas le biais")

    context_ok = bias != 0 and has_levels and day_ok and h4_ok and h1_ok
    setup.context_ok = context_ok

    # ---------------- Étape 5 : déclencheur d'entrée 15 min ----------------
    trig = entry_trigger(m15, bias if context_ok else 0, l_m15, allow_divergence=allow_divergence)
    checklist.append(_check(
        5, "Déclencheur d'entrée en 15 min (seule UT d'entrée)", bool(trig["fired"]),
        trig["reason"] or "—",
        "L'entrée ne se prend QUE en 15 min, et seulement sur l'un des trois déclencheurs : repli "
        "sur MA20/MA50 ou zone d'or Fibonacci avec bougie de reprise, cassure confirmée par le "
        "volume et le VWAP, ou divergence en notre faveur. Le 15 min donne le TIMING ; la direction "
        "vient des unités supérieures.\n" + l_m15.explanation,
    ))
    if context_ok and not trig["fired"]:
        reasons.append(f"pas d'entrée 15 min : {trig['reason']}")

    # --- Heures de marché : on ANALYSE toujours, on n'OUVRE que si une grande place est ouverte ---
    # `can_trade` est la décision de l'appelant : il combine le réglage global et l'état réel des
    # sessions. On s'y tient — re-tester la session ici rendrait le réglage impossible à désactiver.
    sess_ctx = session or {}
    if not can_trade:
        checklist.append(_check(
            5, "Place de Londres ou de New York ouverte", False,
            sess_ctx.get("trade_window") or sess_ctx.get("label", "marchés fermés"),
            "L'analyse tourne en permanence, y compris marchés fermés — c'est ainsi qu'on arrive "
            "préparé à l'ouverture. Mais on n'OUVRE pas de position hors des heures de Londres et "
            "de New York : le carnet d'ordres est vide, les écarts s'élargissent et les mouvements "
            "ne sont pas représentatifs.",
        ))
        reasons.append("marchés de Londres et de New York fermés — analyse seule, pas d'ouverture")

    # --- Score transmis au Master (informatif même sans déclencheur) ---
    alignment = sum(
        1 for lay in (l_month, l_day, l_h4, l_h1, l_m15) if lay.bias == bias and bias != 0
    ) / 5
    if context_ok:
        setup.score = bias * min(1.0, 0.45 + 0.55 * alignment)
    elif bias != 0:
        setup.score = bias * 0.20 * alignment   # biais visible mais non exploitable
    else:
        setup.score = 0.0

    _layers = {"monthly": l_month, "daily": l_day, "h4": l_h4, "h1": l_h1, "m15": l_m15}

    def _finish(s: PlaybookSetup) -> PlaybookSetup:
        """Calcule les scores de fiabilité affichés puis rédige l'explication complète."""
        if s.direction == "NO_TRADE":
            s.reliability_score = 0
        else:
            grade = max(1, min(5, int(round(s.confidence * 5))))
            s.reliability_score = grade if s.direction == "BUY" else -grade
        # Fiabilité du CONTEXTE : renseignée dès que les étapes 1-3 sont validées, même sans
        # déclencheur. Un setup armé sur 4 unités de temps alignées n'est pas « 0/5 ».
        if s.context_ok and bias != 0:
            ctx_grade = max(1, min(5, int(round((0.5 * alignment + 0.5 * s.confidence) * 5))))
            s.context_reliability = ctx_grade if bias > 0 else -ctx_grade
        else:
            s.context_reliability = 0
        s.narrative = narrate(
            s, _layers, trend_sense=sens, day_ok=day_ok, h4_ok=h4_ok, trigger=trig,
            min_rr=min_rr, max_rr=max_rr, min_target_pips=min_target_pips,
        )
        return s

    if not context_ok or not trig["fired"]:
        setup.reasons = reasons or ["conditions de la stratégie non réunies"]
        setup.checklist = checklist
        setup.confidence = round(0.25 + 0.35 * alignment, 3)
        return _finish(setup)

    # ---------------- Étapes 6-8 : risque, objectif ≥ 200 pips, faisabilité ----------
    entry = trig["entry"]
    stop = trig["stop"]
    sign = 1 if bias > 0 else -1

    # --- Le stop doit permettre de loger 200 pips dans la bande de R/R ---
    # 200 pips avec un R/R plafonné à 1:3 => stop d'au moins 200/3 ≈ 67 pips. Le stop 15 min du
    # déclencheur (quelques pips) est donc systématiquement recalé sur la STRUCTURE 4 H : c'est la
    # seule échelle où un stop de cette taille a un sens de marché.
    target_floor = min_target_pips * pip
    min_risk = target_floor / max_rr if max_rr > 0 else target_floor
    max_risk = min(max_stop_pips * pip, target_floor / min_rr if min_rr > 0 else max_stop_pips * pip)

    # --- SUPPORTS / RÉSISTANCES 15 MIN et 1 H : ce sont eux qui POSENT le stop et l'objectif ---
    # Un stop placé à une distance calculée ne correspond à rien sur le graphique ; placé derrière
    # un support réel, il n'est franchi que si ce support cède — c'est-à-dire si le scénario est
    # réellement invalidé. Même logique pour l'objectif, posé devant la première résistance.
    structure = entry_structure(m15, h1, entry)
    setup.entry_levels = {
        "supports": [round(x, 8) for x in structure["supports"][:5]],
        "resistances": [round(x, 8) for x in structure["resistances"][:5]],
        "timeframes": "15 min + 1 h" if h1 else "15 min",
    }

    anchored = stop_behind_level(structure, entry, bias,
                                 min_distance=min_risk, max_distance=max_risk)
    if anchored is not None:
        stop, setup.stop_basis = anchored
    elif abs(entry - stop) < min_risk:
        stop = structural_stop(h4, bias, entry, min_risk)
        setup.stop_basis = (
            f"structure 4 h (aucun support/résistance 15 min ou 1 h à une distance exploitable "
            f"pour viser {min_target_pips:.0f} {setup.pips_label})"
        )
    else:
        setup.stop_basis = "structure 15 min (déjà assez large pour l'objectif)"

    # --- Variante A/B « stop_atr4h » : stop à k × ATR 4 h, borné par la bande de R/R ---
    # Réponse candidate au constat de volatilité (les stoppés ont un ATR 21 % plus haut) : un stop
    # proportionnel à la volatilité RÉELLE du moment plutôt que posé sur la structure. La borne
    # [min_risk ; max_risk] préserve la contrainte des 200 pips et la bande de R/R.
    if stop_mode == "atr4h":
        atr4 = ind.atr(h4, 14) or 0.0
        if atr4 > 0:
            dist = min(max(stop_atr_mult * atr4, min_risk), max_risk)
            stop = entry - sign * dist
            setup.stop_basis = (
                f"stop volatilité {stop_atr_mult:g} × ATR 4 h (borné par la bande de R/R)"
            )

    # --- FILTRE DE VOLATILITÉ (mesuré sur le backtest) ---
    # Les trades stoppés ont un ATR journalier supérieur de 21 % à celui des gagnants (1,39 % contre
    # 1,15 %) — c'est le seul facteur qui les sépare nettement. Un stop calibré sur une volatilité
    # normale saute sur du bruit quand l'amplitude quotidienne gonfle.
    vol = volatility_adjustment(
        daily, entry, bias, stop,
        max_atr_pct=max_atr_pct, mode=volatility_mode, max_widen=volatility_max_widen,
        enabled=volatility_filter,
    )
    setup.volatility = vol
    if vol["action"] == "refuse":
        reasons.append(vol["reason"])
    elif vol["action"] == "widen":
        stop = vol["stop"]
        setup.stop_basis += f" · {vol['reason']}"

    risk_dist = abs(entry - stop)
    risk_pips = risk_dist / pip if pip else 0.0

    stop_ok = 0 < risk_pips <= max_stop_pips
    checklist.append(_check(
        6, f"Stop structurel cohérent (≤ {max_stop_pips:.0f} {setup.pips_label})", stop_ok,
        f"{risk_pips:.1f} {setup.pips_label} — placé sur la {setup.stop_basis}",
        f"Le stop est placé derrière la structure, jamais à une distance arbitraire. Il doit valoir "
        f"au moins {min_risk / pip:.0f} {setup.pips_label} (objectif {min_target_pips:.0f} ÷ R/R "
        f"maximum 1:{max_rr:g}) : viser {min_target_pips:.0f} pips avec un stop minuscule donnerait "
        f"un R/R irréaliste et la position sauterait au premier soubresaut. Le 15 min donne le "
        f"moment de l'entrée ; il ne porte pas le risque.",
    ))

    # --- Objectif : au moins 200 pips ET au moins le R/R minimum ---
    # L'objectif satisfait SIMULTANÉMENT les deux contraintes ; le R/R qui en résulte doit ensuite
    # tomber dans la bande [min_rr ; max_rr], sinon le setup est refusé.
    target_dist = max(min_rr * risk_dist, target_floor)
    setup.target_basis = (
        f"max({min_rr:g} × risque, {min_target_pips:.0f} {setup.pips_label})"
    )
    # L'objectif est posé DEVANT la première résistance (ou support) 15 min / 1 h qui offre au moins
    # cette distance : viser au-delà supposerait de traverser ce niveau, pari qu'on ne prend pas.
    aimed = target_before_level(
        structure, entry, bias,
        min_distance=target_dist, max_distance=max_rr * risk_dist,
    )
    if aimed is not None:
        target_price, target_reason = aimed
        target_dist = abs(target_price - entry)
        setup.target_basis = target_reason

    # Faisabilité : l'objectif doit être atteignable AVANT le niveau MAJEUR opposé — celui que tout
    # le marché surveille (swings mensuels, extrêmes journaliers de référence). On ne borne PAS
    # l'objectif sur chaque micro-swing journalier : ils sont trop denses et bloqueraient tout.
    barrier = levels["major_resistance"] if bias > 0 else levels["major_support"]
    room_ok = True
    if barrier is None:
        room_txt = "aucun niveau majeur sur la route"
    else:
        setup.target_level = barrier
        room = abs(barrier - entry)
        # 60 % de l'objectif en marge libre : au-delà, on suppose que le prix traverse un niveau
        # défendu, ce qui n'est plus un pari raisonnable.
        room_ok = room >= 0.6 * target_dist
        setup.target_basis += f", sous le niveau majeur {barrier:.6g}"
        room_txt = (
            f"{room / pip:.0f} {setup.pips_label} de marge jusqu'au niveau majeur "
            f"{barrier:.6g} (objectif {target_dist / pip:.0f})"
        )

    tp1 = entry + sign * target_dist
    reward_pips = target_dist / pip if pip else 0.0
    rr = target_dist / risk_dist if risk_dist > 0 else 0.0

    # TP1 = R/R minimum (1:2) ; TP2 = R/R maximum (1:3). C'est TP1 qui sert de niveau de
    # SÉCURISATION : dès qu'il est touché, le stop y est remonté et la position ne peut plus perdre.
    major = levels["major_resistance"] if bias > 0 else levels["major_support"]
    def _cap(value: float) -> float:
        if major is None:
            return value
        return min(value, major) if bias > 0 else max(value, major)
    tp2 = _cap(entry + sign * max_rr * risk_dist)
    # La stratégie ne définit QUE deux objectifs : le R/R minimum (TP1, niveau de sécurisation) et
    # le R/R maximum (TP2, objectif final). Un troisième palier serait un chiffre inventé — on le
    # laisse vide plutôt que d'afficher un niveau auquel personne ne vise.
    tp3 = None
    if (tp2 - entry) * sign <= 0 or abs(tp2 - tp1) < 1e-9:
        tp2 = None      # le niveau majeur est déjà atteint par TP1 : pas de second objectif distinct
    setup.secure_stop = round(entry + sign * SECURE_AT_R * risk_dist, 8)

    rr_ok = min_rr - 0.01 <= rr <= max_rr + 0.01
    checklist.append(_check(
        7, f"Risque / rendement dans la bande 1:{min_rr:g} – 1:{max_rr:g}", rr_ok, f"1:{rr:.2f}",
        f"L'objectif vaut {reward_pips:.1f} {setup.pips_label} pour {risk_pips:.1f} risqués, soit "
        f"1:{rr:.2f}. Le plancher de 1:{min_rr:g} garantit qu'un gagnant paie au moins deux "
        f"perdants ; le plafond de 1:{max_rr:g} interdit un objectif si lointain qu'il ne serait "
        f"presque jamais atteint.",
    ))
    if not rr_ok:
        reasons.append(f"R/R {rr:.2f} hors de la bande {min_rr:g}–{max_rr:g}")

    floor_ok = reward_pips >= min_target_pips - 0.5
    checklist.append(_check(
        7, f"Objectif ≥ {min_target_pips:.0f} {setup.pips_label}", floor_ok,
        f"{reward_pips:.1f} {setup.pips_label} (TP1 {tp1:.6g}) — {setup.target_basis}",
        f"Plancher fixé par la stratégie. TP1 ({tp1:.6g}) correspond au R/R minimum et sert de "
        f"niveau de SÉCURISATION : dès qu'il est atteint, le stop y est remonté automatiquement et "
        f"la position ne peut plus redevenir perdante."
        + (f" On la laisse ensuite courir vers TP2 ({tp2:.6g}), au R/R maximum 1:{max_rr:g}."
           if tp2 is not None else
           " Le niveau majeur opposé ne laisse pas la place à un second objectif distinct."),
    ))
    if not floor_ok:
        reasons.append(
            f"objectif ({reward_pips:.1f} {setup.pips_label}) sous le plancher de "
            f"{min_target_pips:.0f} — le niveau journalier opposé est trop proche"
        )

    checklist.append(_check(
        8, "Objectif atteignable avant le niveau majeur opposé", room_ok, room_txt,
        "Un objectif situé derrière un support/résistance MAJEUR suppose que le prix traverse le "
        "niveau que tout le marché défend. On exige au moins 60 % de l'objectif en marge libre. "
        "Ce sont les niveaux majeurs (swings mensuels, extrêmes journaliers de référence) qui "
        "servent ici, pas chaque micro-swing : un niveau que personne ne surveille ne borne rien.",
    ))
    if not room_ok:
        reasons.append("objectif bloqué par un niveau majeur")

    # Volatilité : viser 200 pips n'a de sens que si le marché parcourt cette distance.
    daily_atr = ind.atr(daily, 14) or 0.0
    atr_multiple = (target_dist / daily_atr) if daily_atr > 0 else 0.0
    reach_ok = daily_atr <= 0 or atr_multiple <= max_atr_multiple
    horizon = math.ceil(atr_multiple) if atr_multiple else None
    setup.horizon_days = float(horizon) if horizon else None
    setup.horizon_hours = round(setup.horizon_days * 24, 1) if setup.horizon_days else None
    setup.horizon_label = f"~{horizon} j" if horizon else ""
    checklist.append(_check(
        8, f"Objectif compatible avec la volatilité (≤ {max_atr_multiple:g} × ATR journalier)", reach_ok,
        f"objectif {reward_pips:.0f} {setup.pips_label} = {atr_multiple:.1f} × l'ATR journalier "
        f"({daily_atr / pip:.0f} {setup.pips_label}) → horizon estimé {setup.horizon_label}"
        if pip and daily_atr else "—",
        f"L'ATR journalier est l'amplitude moyenne d'une journée. Un objectif de "
        f"{reward_pips:.0f} {setup.pips_label} demande environ {atr_multiple:.1f} journées moyennes "
        f"de mouvement : ce trade est un SWING, il se tient plusieurs séances. Au-delà de "
        f"{max_atr_multiple:g} × l'ATR, l'objectif n'est plus raisonnable sur cet horizon.",
    ))

    # ---------------- Étape 9 : timing de session ----------------
    sess = session or {}
    timing_ok = bool(sess.get("prime", True))
    checklist.append(_check(
        9, "Fenêtre de session favorable (ouverture Londres/New York ou chevauchement)", timing_ok,
        sess.get("label", "session non évaluée"),
        "Les mouvements directionnels naissent quand le volume institutionnel entre : premières "
        "heures de Londres, premières heures de New York, et surtout leur chevauchement "
        "(12:00–16:00 UTC), la fenêtre la plus liquide de la journée. Hors de ces créneaux, la "
        "conviction est réduite mais le setup reste valable.",
    ))

    if not stop_ok:
        reasons.append(
            f"stop trop large ({risk_pips:.0f} {setup.pips_label}, maximum {max_stop_pips:.0f}) "
            f"pour viser {min_target_pips:.0f} pips proprement"
        )
    if not reach_ok:
        reasons.append(
            f"objectif hors de portée : {atr_multiple:.1f} × l'ATR journalier "
            f"(maximum {max_atr_multiple:g})"
        )

    setup.checklist = checklist
    if reasons:
        setup.reasons = reasons
        setup.confidence = round(0.30 + 0.30 * alignment, 3)
        setup.score = bias * 0.20 * alignment
        return _finish(setup)

    # ---------------- Setup VALIDÉ ----------------
    setup.direction = "BUY" if bias > 0 else "SELL"
    setup.entry = round(entry, 8)
    setup.stop_loss = round(stop, 8)
    setup.take_profit_1 = round(tp1, 8)
    setup.take_profit_2 = round(tp2, 8) if tp2 is not None else None
    setup.take_profit_3 = round(tp3, 8) if tp3 is not None else None
    setup.risk_pips = risk_pips
    setup.reward_pips = reward_pips
    setup.risk_reward = rr
    setup.trigger = f"{trig['type']} — {trig['reason']}"
    setup.ready = True
    quality = sum(1 for c in checklist if c["pass"]) / len(checklist)
    setup.confidence = round(min(1.0, 0.35 + 0.45 * alignment + 0.20 * quality) *
                             (0.75 + 0.25 * float(sess.get("quality", 1.0))), 3)
    setup.reasons = []
    return _finish(setup)
