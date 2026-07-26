"""PLAYBOOK — la stratégie de trading imposée à TOUS les agents du projet.

Méthode, dans cet ordre STRICT (aucune étape ne peut être sautée) :

1. **Mensuel + Journalier** — établir la tendance de fond et FIXER les supports / résistances
   MAJEURS. Sans tendance de fond claire : pas de trade.
2. **Journalier (détail)** — RSI 14, MA 20, MA 50, volume, tendance VWAP, divergences RSI et MACD,
   et retracements de Fibonacci **en cas de correction**. Le journalier doit CONFIRMER le biais.
3. **4 heures** — exactement les mêmes facteurs, pour confirmer une seconde fois.
4. **15 minutes** — la SEULE unité de temps où l'on entre. Déclencheur d'entrée : repli sur MA/zone
   d'or Fibonacci avec bougie de reprise, cassure confirmée par le volume, ou divergence.

Contraintes non négociables appliquées ensuite :
- Risque / rendement **minimum 1 : 2**.
- Objectif **minimum 200 pips** (cf. `domain.pips` pour l'équivalence hors forex).
- Le stop est **recalé sur la structure 4 h** si le stop 15 min est trop serré pour l'objectif :
  viser 200 pips avec un stop de 5 pips donnerait un R/R de 40:1 que le marché ne paie jamais.
- L'objectif doit être atteignable AVANT le niveau majeur opposé, et compatible avec la volatilité
  journalière (l'horizon estimé du trade est calculé et affiché).
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
MIN_RR = 1.2                 # risque/rendement minimum
MAX_RR = 1.3                 # risque/rendement maximum
MIN_TARGET_PIPS = 200.0      # objectif minimum de 200 pips
# Conséquence arithmétique : viser 200 pips avec un R/R plafonné à 1,3 impose un stop d'au moins
# 200 / 1,3 ≈ 154 pips. Le plafond de stop doit donc laisser la place.
MAX_STOP_PIPS = 200.0
MAX_ATR_MULTIPLE = 4.0       # objectif <= N × ATR journalier (sinon hors de portée)
MIN_LAYER_SCORE = 0.08       # en-deçà, une unité de temps est « neutre » (pas de confirmation)

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
MIN_CANDLES = {"mensuel": 12, "journalier": 60, "4h": 60, "15m": 60}

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
def entry_trigger(candles: list[Candle], direction: int, layer: Layer) -> dict:
    """Cherche un déclencheur d'entrée 15 min dans le sens du biais confirmé.

    Trois déclencheurs valides (par ordre de qualité) :
    1. **Repli** sur MA20/MA50 ou dans la zone d'or Fibonacci + bougie de reprise + RSI qui se retourne.
    2. **Cassure** du dernier swing, confirmée par le volume et le VWAP.
    3. **Divergence** RSI/MACD en notre faveur + bougie de reprise.
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

    # 3) Divergence en notre faveur.
    if not out["fired"]:
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
    out.append("6) LE RISQUE ET L'OBJECTIF")
    if setup.ready:
        out.append(
            f"   Entrée à {setup.entry:.6g}, stop à {setup.stop_loss:.6g} "
            f"({setup.risk_pips:.0f} {setup.pips_label}), premier objectif à {setup.take_profit_1:.6g} "
            f"({setup.reward_pips:.0f} {setup.pips_label}). Le rapport risque/rendement est de "
            f"1 pour {setup.risk_reward:.2f}, dans la bande imposée de {min_rr:g} à {max_rr:g}."
        )
        out.append(
            f"   Le stop est placé sur la {setup.stop_basis} : il doit rester assez large pour "
            f"encaisser le bruit du marché, sinon la position sauterait avant d'avoir eu le temps "
            f"de travailler. L'objectif minimum est de {min_target_pips:.0f} {setup.pips_label}."
        )
        if setup.horizon_days:
            out.append(
                f"   Compte tenu de l'amplitude moyenne d'une journée sur cet actif, atteindre cet "
                f"objectif demande environ {setup.horizon_days:.0f} jour(s) : ce trade est un swing, "
                f"il se tient plusieurs séances, il ne se joue pas dans l'heure."
            )
    else:
        out.append(f"   Aucun niveau n'est proposé puisqu'il n'y a pas d'entrée. Pour information, la "
                   f"stratégie exige un rapport risque/rendement entre {min_rr:g} et {max_rr:g} et un "
                   f"objectif d'au moins {min_target_pips:.0f} pips.")
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
    donnerait un R/R de 40:1 que le marché ne paie jamais — le trade serait stoppé avant d'avoir
    commencé. L'objectif et le stop doivent vivre sur la même échelle de temps.
    """
    atr4 = ind.atr(h4, 14) or min_distance
    window = h4[-10:] if len(h4) >= 10 else h4
    if bias > 0:
        candidate = min(c.low for c in window) - 0.3 * atr4
        return min(candidate, entry - min_distance)
    candidate = max(c.high for c in window) + 0.3 * atr4
    return max(candidate, entry + min_distance)


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
    stop_basis: str = "structure 15 min"   # d'où vient le stop (15 min ou structure 4 h élargie)
    horizon_days: float | None = None      # durée estimée pour atteindre l'objectif (ATR journalier)
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
            "horizon_days": self.horizon_days,
            "ready": self.ready, "veto": self.veto, "context_ok": self.context_ok,
            "insufficient": self.insufficient, "checklist": self.checklist,
            "levels": self.levels, "session": self.session, "reasons": self.reasons,
            "layers": self.layers, "trend_explanation": self.trend_explanation,
            "reliability_score": self.reliability_score,
            "reliability": trade_reliability_label(self.reliability_score),
            "narrative": self.narrative,
        }

    def summary(self) -> str:
        """Résumé lisible façon note de desk."""
        if self.direction == "NO_TRADE":
            why = " ; ".join(self.reasons) or "conditions non réunies"
            return f"Playbook {self.symbol} — PAS DE TRADE : {why}."
        horizon = f" | horizon ~{self.horizon_days:.0f} j" if self.horizon_days else ""
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
    session: dict | None = None,
    min_rr: float = MIN_RR,
    min_target_pips: float = MIN_TARGET_PIPS,
    max_stop_pips: float = MAX_STOP_PIPS,
    max_rr: float = MAX_RR,
    max_atr_multiple: float = MAX_ATR_MULTIPLE,
) -> PlaybookSetup:
    """Exécute la stratégie de bout en bout et retourne le setup (ou un refus motivé)."""
    setup = PlaybookSetup(symbol=symbol, session=session or {})
    checklist: list[dict] = []
    reasons: list[str] = []

    l_month = factor_layer(monthly, "mensuel", symbol)
    l_day = factor_layer(daily, "journalier", symbol)
    l_h4 = factor_layer(h4, "4h", symbol)
    l_m15 = factor_layer(m15, "15m", symbol)
    setup.layers = {k: v.as_dict() for k, v in
                    (("monthly", l_month), ("daily", l_day), ("h4", l_h4), ("m15", l_m15))}

    if not (l_month.ok and l_day.ok and l_h4.ok and l_m15.ok):
        missing = [layer.label for layer in (l_month, l_day, l_h4, l_m15) if not layer.ok]
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

    context_ok = bias != 0 and has_levels and day_ok and h4_ok
    setup.context_ok = context_ok

    # ---------------- Étape 4 : déclencheur d'entrée 15 min ----------------
    trig = entry_trigger(m15, bias if context_ok else 0, l_m15)
    checklist.append(_check(
        4, "Déclencheur d'entrée en 15 min (seule UT d'entrée)", bool(trig["fired"]),
        trig["reason"] or "—",
        "L'entrée ne se prend QUE en 15 min, et seulement sur l'un des trois déclencheurs : repli "
        "sur MA20/MA50 ou zone d'or Fibonacci avec bougie de reprise, cassure confirmée par le "
        "volume et le VWAP, ou divergence en notre faveur. Le 15 min donne le TIMING ; la direction "
        "vient des unités supérieures.\n" + l_m15.explanation,
    ))
    if context_ok and not trig["fired"]:
        reasons.append(f"pas d'entrée 15 min : {trig['reason']}")

    # --- Score transmis au Master (informatif même sans déclencheur) ---
    alignment = sum(1 for lay in (l_month, l_day, l_h4, l_m15) if lay.bias == bias and bias != 0) / 4
    if context_ok:
        setup.score = bias * min(1.0, 0.45 + 0.55 * alignment)
    elif bias != 0:
        setup.score = bias * 0.20 * alignment   # biais visible mais non exploitable
    else:
        setup.score = 0.0

    _layers = {"monthly": l_month, "daily": l_day, "h4": l_h4, "m15": l_m15}

    def _finish(s: PlaybookSetup) -> PlaybookSetup:
        """Calcule le score de fiabilité affiché puis rédige l'explication complète."""
        if s.direction == "NO_TRADE":
            s.reliability_score = 0
        else:
            grade = max(1, min(5, int(round(s.confidence * 5))))
            s.reliability_score = grade if s.direction == "BUY" else -grade
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

    # ---------------- Étapes 5-7 : risque, objectif, faisabilité ----------------
    entry = trig["entry"]
    stop = trig["stop"]
    target_floor = min_target_pips * pip
    # Un objectif de 200 pips impose un stop d'au moins 200/max_rr pips : sinon le R/R affiché est
    # flatteur mais le trade est stoppé par le premier soubresaut.
    min_risk = target_floor / max_rr
    if abs(entry - stop) < min_risk:
        stop = structural_stop(h4, bias, entry, min_risk)
        setup.stop_basis = "structure 4 h (stop 15 min trop serré pour l'objectif)"
    risk_dist = abs(entry - stop)
    risk_pips = risk_dist / pip if pip else 0.0

    checklist.append(_check(
        5, f"Stop technique cohérent (≤ {max_stop_pips:.0f} {setup.pips_label})",
        0 < risk_pips <= max_stop_pips,
        f"{risk_pips:.0f} {setup.pips_label} — placé sur la {setup.stop_basis}",
        f"Le stop est placé derrière la structure, pas à une distance arbitraire. Il doit valoir au "
        f"moins {min_risk / pip:.0f} {setup.pips_label} (objectif {min_target_pips:.0f} ÷ R/R max "
        f"{max_rr:g}) : viser {min_target_pips:.0f} pips avec un stop minuscule donnerait un R/R "
        f"irréaliste et le trade serait stoppé avant d'avoir commencé.",
    ))
    stop_ok = 0 < risk_pips <= max_stop_pips

    # L'objectif satisfait SIMULTANÉMENT le R/R minimum et le plancher de pips.
    target_dist = max(min_rr * risk_dist, target_floor)
    sign = 1 if bias > 0 else -1
    tp1 = entry + sign * target_dist
    tp2 = entry + sign * target_dist * 1.5
    tp3 = entry + sign * target_dist * 2.0
    reward_pips = target_dist / pip if pip else 0.0
    rr = target_dist / risk_dist if risk_dist > 0 else 0.0

    rr_ok = min_rr - 0.01 <= rr <= max_rr + 0.01
    checklist.append(_check(
        6, f"Risque / rendement dans la bande 1:{min_rr:g} – 1:{max_rr:g}", rr_ok, f"1:{rr:.2f}",
        f"L'objectif vaut max({min_rr:g} × risque, {min_target_pips:.0f} {setup.pips_label}) = "
        f"{reward_pips:.0f} pour {risk_pips:.0f} risqués, soit 1:{rr:.2f}. La bande est volontairement "
        f"resserrée : un objectif proche du risque est atteint bien plus souvent qu'un objectif "
        f"lointain, et le plafond de {max_rr:g} interdit un stop trop serré (qui gonflerait le R/R "
        f"affiché tout en faisant sauter la position au premier soubresaut).",
    ))
    if not rr_ok:
        reasons.append(f"R/R {rr:.2f} hors de la bande {min_rr:g}–{max_rr:g}")
    checklist.append(_check(
        7, f"Objectif ≥ {min_target_pips:.0f} {setup.pips_label}", reward_pips >= min_target_pips,
        f"{reward_pips:.0f} {setup.pips_label} (TP1 {tp1:.6g})",
        f"Plancher fixé par la stratégie. TP2 ({tp2:.6g}) et TP3 ({tp3:.6g}) permettent de sortir "
        f"par paliers en laissant courir une partie de la position.",
    ))

    # Faisabilité : l'objectif doit être atteignable avant le niveau MAJEUR opposé.
    barrier = levels["major_resistance"] if bias > 0 else levels["major_support"]
    room_ok = True
    room_txt = "aucun niveau majeur sur la route"
    if barrier is not None:
        room = abs(barrier - entry)
        room_ok = room >= 0.6 * target_dist
        room_txt = (f"{room / pip:.0f} {setup.pips_label} de marge jusqu'au niveau majeur "
                    f"{barrier:.6g} (objectif {reward_pips:.0f})")
    checklist.append(_check(
        7, "Objectif atteignable avant le niveau majeur opposé", room_ok, room_txt,
        "Un objectif situé derrière un support/résistance majeur suppose que le prix traverse le "
        "niveau que tout le marché défend. On exige au moins 60 % de l'objectif en marge libre.",
    ))

    # Volatilité : viser 200 pips n'a de sens que si le marché parcourt cette distance.
    daily_atr = ind.atr(daily, 14) or 0.0
    atr_multiple = (target_dist / daily_atr) if daily_atr > 0 else 0.0
    reach_ok = daily_atr <= 0 or atr_multiple <= max_atr_multiple
    horizon = math.ceil(atr_multiple) if atr_multiple else None
    setup.horizon_days = float(horizon) if horizon else None
    checklist.append(_check(
        7, f"Objectif compatible avec la volatilité (≤ {max_atr_multiple:g} × ATR journalier)", reach_ok,
        f"objectif {reward_pips:.0f} = {atr_multiple:.1f} × l'ATR journalier "
        f"({daily_atr / pip:.0f} {setup.pips_label}) → horizon estimé ~{horizon} jour(s)"
        if pip and daily_atr else "—",
        f"L'ATR journalier est l'amplitude moyenne d'une journée. Un objectif de "
        f"{reward_pips:.0f} {setup.pips_label} demande donc environ {atr_multiple:.1f} journées "
        f"moyennes de mouvement : ce trade est un SWING, il se tient plusieurs jours. Au-delà de "
        f"{max_atr_multiple:g} × l'ATR, l'objectif n'est plus raisonnable sur cet horizon.",
    ))

    # ---------------- Étape 8 : timing de session ----------------
    sess = session or {}
    timing_ok = bool(sess.get("prime", True))
    checklist.append(_check(
        8, "Fenêtre de session favorable (ouverture Londres/New York ou chevauchement)", timing_ok,
        sess.get("label", "session non évaluée"),
        "Les mouvements directionnels naissent quand le volume institutionnel entre : premières "
        "heures de Londres, premières heures de New York, et surtout leur chevauchement "
        "(12:00–16:00 UTC), la fenêtre la plus liquide de la journée. Hors de ces créneaux, la "
        "conviction est réduite mais le setup reste valable.",
    ))

    if not stop_ok:
        reasons.append(
            f"stop trop large ({risk_pips:.0f} {setup.pips_label}) pour viser "
            f"{min_target_pips:.0f} pips proprement"
        )
    if not room_ok:
        reasons.append("objectif bloqué par un niveau majeur")
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
    setup.take_profit_2 = round(tp2, 8)
    setup.take_profit_3 = round(tp3, 8)
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
