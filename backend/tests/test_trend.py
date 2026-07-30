"""Tests de l'ÉTAPE 1 : détection de tendance multi-indicateurs.

Ce que le moteur doit garantir :
- la direction vient de plusieurs indicateurs, jamais d'un seul ;
- l'ADX ne décide RIEN (retiré de la stratégie après mesure) — il reste publié pour information ;
- le 4 h et le 1 h doivent s'accorder — et eux seuls. Le 15 min ne bloque pas (il est en correction
  au moment précis où l'on cherche une entrée) ; le journalier non plus depuis le 28/07/2026, mais
  il pèse toujours 40 % du score, donc son désaccord se paie en confiance ;
- ce qui n'est pas mesurable ne vote pas (le volume absent est retiré, pas compté comme neutre).
"""

from __future__ import annotations

from app.domain import trend as trend_mod
from app.domain.indicators import Candle


def _c(o, h, low, c, v=1000.0):
    return Candle(o, h, low, c, v)


def _trend(n=260, start=1.1000, step=0.0015, up=True, vol=1000.0) -> list[Candle]:
    """Tendance nette en escalier : 5 bougies dans le sens, 2 de repli -> HH + HL, ADX élevé."""
    out, p = [], start
    for i in range(n):
        move = step * (1.0 if i % 7 < 5 else -0.6)
        p += move if up else -move
        out.append(_c(p - step * 0.3, p + step * 0.5, p - step * 0.5, p, vol))
    return out


def _range(n=260, price=1.1000, amp=0.0008, vol=1000.0) -> list[Candle]:
    """Marché sans direction : oscillation autour d'un prix -> ADX bas."""
    out = []
    for i in range(n):
        d = amp * (1 if i % 4 < 2 else -1)
        p = price + d
        out.append(_c(price, p + amp * 0.4, p - amp * 0.4, p, vol))
    return out


def _all_up() -> dict:
    return {"monthly": _trend(80, 1.0000, 0.0100), "daily": _trend(), "h4": _trend(),
            "h1": _trend(), "m15": _trend()}


def _confidence(**over) -> dict:
    data = _all_up()
    data.update(over)
    return trend_mod.trend_confidence(
        data["monthly"], data["daily"], data["h4"], data["h1"], data["m15"]
    )


# --- Vote d'une unité de temps -----------------------------------------------------------------

def test_a_layer_combines_several_indicators_not_just_one():
    layer = trend_mod.trend_vote_layer(_trend(), "journalier")
    assert layer["ok"] is True
    keys = {f["key"] for f in layer["factors"]}
    assert {"ema", "structure", "supertrend", "macd", "rsi"} <= keys
    assert layer["bias"] == 1 and layer["score"] > 0


def test_a_downtrend_votes_the_other_way():
    layer = trend_mod.trend_vote_layer(_trend(up=False, start=1.4000), "journalier")
    assert layer["bias"] == -1 and layer["score"] < 0


def test_a_layer_without_history_says_so_instead_of_guessing():
    layer = trend_mod.trend_vote_layer([_c(1.1, 1.1005, 1.0995, 1.1002)] * 20, "journalier")
    assert layer["ok"] is False and layer["bias"] == 0


def test_volume_that_cannot_be_measured_does_not_vote():
    """Sur le forex spot il n'y a pas de volume : le facteur doit DISPARAÎTRE, pas valoir zéro."""
    no_volume = trend_mod.trend_vote_layer(_trend(vol=0.0), "journalier")
    keys = {f["key"] for f in no_volume["factors"]}
    assert "volume" not in keys
    # Les autres indicateurs continuent de trancher normalement.
    assert no_volume["ok"] is True and no_volume["bias"] == 1


def test_directional_indicators_follow_the_move():
    up = trend_mod.trend_vote_layer(_trend(), "journalier")
    assert up["plus_di"] > up["minus_di"]
    down = trend_mod.trend_vote_layer(_trend(up=False, start=1.4000), "journalier")
    assert down["minus_di"] > down["plus_di"]


# --- Agrégation multi-unités de temps ----------------------------------------------------------

def test_four_aligned_timeframes_validate_the_trend():
    res = _confidence()
    assert res["status"] == "valid"
    assert res["direction"] == 1
    assert 0 < res["confidence"] <= 1
    assert res["score_100"] == round(res["confidence"] * 100)


def test_the_aggregate_score_follows_the_documented_weights():
    """S = 0,40 × journalier + 0,30 × 4 h + 0,20 × 1 h + 0,10 × 15 min."""
    res = _confidence()
    per_tf = res["per_tf"]
    expected = sum(per_tf[name]["score"] * w for name, w in trend_mod.TF_WEIGHTS.items())
    assert abs(res["score"] - expected) < 1e-6


def test_a_range_is_refused_by_the_indicators_themselves():
    """Un marché sans direction est écarté par le désaccord des indicateurs, pas par l'ADX."""
    res = _confidence(daily=_range(), h4=_range(), h1=_range(), m15=_range())
    assert res["status"] != "valid"
    assert res["direction"] == 0
    assert res["reasons"]                    # le refus est toujours motivé


def test_the_adx_is_published_but_never_decides():
    """Mesuré : en faire un verrou coupait surtout de bons trades. Il informe, il ne filtre pas."""
    res = _confidence()
    assert res["status"] == "valid"
    assert res["adx"]["informatif"] is True
    assert res["adx"]["journalier"] > 0            # toujours calculé
    assert not any("ADX" in r for r in res["reasons"])
    # Le statut « tendance trop faible (ADX) » n'existe plus.
    assert "weak_adx" not in trend_mod._STATUS_REASONS


def test_only_the_netness_threshold_can_refuse_an_aligned_trend():
    res = trend_mod.trend_confidence(
        _trend(80, 1.0000, 0.0100), _trend(), _trend(), _trend(), _trend(),
        min_score=0.99,      # seuil de netteté inatteignable
    )
    assert res["status"] == "no_direction"
    assert res["direction"] == 0


def test_a_contradicting_timeframe_breaks_the_alignment():
    res = _confidence(h4=_trend(up=False, start=1.4000))
    assert res["status"] == "misaligned"
    assert res["direction"] == 0


def test_a_correcting_15m_does_not_invalidate_the_trend():
    """Le 15 min en correction est NORMAL : c'est même ce qu'on attend pour acheter un repli.

    Il pèse dans le score (10 %) mais ne peut pas invalider une direction portée par le journalier,
    le 4 h et le 1 h — sinon la stratégie d'entrée sur repli serait impossible à exécuter.
    """
    res = _confidence(m15=_trend(up=False, start=1.4000))
    assert res["status"] == "valid" and res["direction"] == 1
    # Sa contribution négative reste visible dans le score agrégé.
    assert res["per_tf"]["15m"]["bias"] == -1
    assert res["score"] < _confidence()["score"]


# --- Règle d'agrégation, testée sur des couches contrôlées ------------------------------------
# Un vrai marché « neutre » (biais exactement nul) est un état trop étroit pour être fabriqué de
# façon fiable par un jeu de bougies. On teste donc la RÈGLE elle-même en fournissant directement
# les votes de chaque unité de temps : c'est la décision d'agrégation qui est vérifiée ici, pas la
# détection (couverte par les tests précédents).

def _stub(monkeypatch, scores: dict, *, adx=40.0, plus_di=30.0, minus_di=10.0, conflicts=None):
    conflicts = conflicts or {}

    def fake_layer(candles, label, *, weights=None):
        return {"label": label, "score": scores.get(label, 0.0),
                "bias": 1 if scores.get(label, 0.0) > 0.1
                else -1 if scores.get(label, 0.0) < -0.1 else 0,
                "ok": True, "adx": adx, "plus_di": plus_di, "minus_di": minus_di,
                "factors": [], "conflict": conflicts.get(label)}

    monkeypatch.setattr(trend_mod, "trend_vote_layer", fake_layer)
    return trend_mod.trend_confidence([], [], [], [], [])


def test_only_the_required_timeframes_must_align(monkeypatch):
    """Le 4 h et le 1 h portent la direction ; le 15 min ne décide que du moment."""
    for m15_score in (0.0, -0.5):
        res = _stub(monkeypatch,
                    {"mensuel": 0.5, "journalier": 0.6, "4h": 0.5, "1h": 0.4, "15m": m15_score})
        assert res["status"] == "valid" and res["direction"] == 1

    contrary_h1 = _stub(monkeypatch,
                        {"mensuel": 0.5, "journalier": 0.6, "4h": 0.5, "1h": -0.4, "15m": 0.3})
    assert contrary_h1["status"] == "misaligned"
    assert "1 h" in contrary_h1["reasons"][0]


def test_the_daily_is_no_longer_required_to_validate_the_trend(monkeypatch):
    """Le D1 n'est plus obligatoire : l'accord du 4 h et du 1 h suffit à nommer la direction.

    Décision de l'utilisateur du 28/07/2026. Le journalier reste dans le SCORE (40 %) — c'est ce
    qui empêche cette souplesse de devenir de la complaisance : un journalier franchement contraire
    tire le score sous le seuil de netteté et la tendance n'est pas nommée non plus.
    """
    # Journalier neutre (biais 0), 4 h et 1 h d'accord -> tendance validée.
    res = _stub(monkeypatch,
                {"mensuel": 0.4, "journalier": 0.0, "4h": 0.6, "1h": 0.6, "15m": 0.3})
    assert res["status"] == "valid" and res["direction"] == 1
    assert res["required_tfs"] == ["4 h", "1 h"]

    # ...mais un journalier franchement contraire pèse encore : il porte 40 % du score.
    contrary = _stub(monkeypatch,
                     {"mensuel": 0.0, "journalier": -0.6, "4h": 0.3, "1h": 0.3, "15m": 0.0})
    assert contrary["status"] != "valid"


def test_the_required_timeframes_are_configurable(monkeypatch):
    """`required_tfs` permet de rétablir l'exigence du journalier sans toucher au code."""
    scores = {"mensuel": 0.4, "journalier": 0.0, "4h": 0.6, "1h": 0.6, "15m": 0.3}

    def fake_layer(candles, label, *, weights=None):
        return {"label": label, "score": scores.get(label, 0.0),
                "bias": 1 if scores.get(label, 0.0) > 0.1
                else -1 if scores.get(label, 0.0) < -0.1 else 0,
                "ok": True, "adx": 40.0, "plus_di": 30.0, "minus_di": 10.0,
                "factors": [], "conflict": None}

    monkeypatch.setattr(trend_mod, "trend_vote_layer", fake_layer)
    strict = trend_mod.trend_confidence(
        [], [], [], [], [], required_tfs=("journalier", "4h", "1h"))
    assert strict["status"] == "misaligned"
    assert "journalier" in strict["reasons"][0]


def test_parse_required_tfs_ignores_nonsense():
    """Une faute de frappe dans le `.env` ne doit pas supprimer toute condition d'alignement."""
    assert trend_mod.parse_required_tfs("4h,1h") == ("4h", "1h")
    assert trend_mod.parse_required_tfs("journalier,4h") == ("journalier", "4h")
    assert trend_mod.parse_required_tfs("") == trend_mod.REQUIRED_TFS
    assert trend_mod.parse_required_tfs("n_importe_quoi") == trend_mod.REQUIRED_TFS


def test_directional_indicators_no_longer_gate_the_trend(monkeypatch):
    """Même des directionnels ADX à contresens ne bloquent plus rien : ils informent seulement."""
    res = _stub(monkeypatch, {"mensuel": 0.5, "journalier": 0.6, "4h": 0.5, "1h": 0.4, "15m": 0.3},
                plus_di=10.0, minus_di=30.0)
    assert res["status"] == "valid"
    assert res["adx"]["plus_di"] == 10.0 and res["adx"]["minus_di"] == 30.0


def test_a_structural_conflict_blocks_the_trend(monkeypatch):
    """Un conflit d'indicateurs sur une unité de temps EXIGÉE ferme la tendance."""
    res = _stub(monkeypatch, {"mensuel": 0.5, "journalier": 0.6, "4h": 0.5, "1h": 0.4, "15m": 0.3},
                conflicts={"4h": "supertrend"})
    assert res["status"] == "conflict"
    assert "supertrend" in res["reasons"][0]


def test_a_conflict_outside_the_required_timeframes_does_not_block(monkeypatch):
    """Le conflit se juge là où l'alignement est exigé : ailleurs, il informe sans fermer.

    Le journalier a rejoint cette catégorie le 28/07/2026, en même temps qu'il a cessé d'être une
    condition d'alignement — les deux règles doivent porter sur les mêmes unités de temps, sinon
    le journalier bloquerait par la fenêtre après être sorti par la porte.
    """
    res = _stub(monkeypatch, {"mensuel": 0.5, "journalier": 0.6, "4h": 0.5, "1h": 0.4, "15m": 0.3},
                conflicts={"journalier": "ema"})
    assert res["status"] == "valid"


def test_a_strongly_contrary_monthly_blocks_the_trend():
    res = _confidence(monthly=_trend(80, 2.0000, 0.0100, up=False))
    assert res["status"] == "monthly_against"
    assert res["direction"] == 0


def test_missing_history_returns_insufficient_and_no_direction():
    res = _confidence(h1=[])
    assert res["status"] == "insufficient"
    assert res["direction"] == 0
    assert "1 h" in res["reasons"][0]


def test_confidence_is_the_netness_of_the_agreement(monkeypatch):
    """La confiance vaut |score agrégé| : plus les unités de temps s'accordent, plus elle monte."""
    strong = _stub(monkeypatch, {"mensuel": 0.8, "journalier": 0.9, "4h": 0.9, "1h": 0.9, "15m": 0.9})
    weak = _stub(monkeypatch, {"mensuel": 0.2, "journalier": 0.3, "4h": 0.2, "1h": 0.2, "15m": 0.2})
    assert strong["confidence"] > weak["confidence"]
    assert abs(strong["confidence"] - abs(strong["score"])) < 1e-9


def test_the_explanation_states_the_arithmetic_and_the_decision():
    res = _confidence()
    text = res["explanation"]
    assert "40 %" in text
    assert "FIGÉE" in text        # la tendance ne sera plus rediscutée par les étapes suivantes
    # L'ADX est mentionné comme une lecture, pas comme une condition.
    assert "pas une condition" in text


def test_a_refusal_always_says_why():
    res = _confidence(h4=_trend(up=False, start=1.4000))
    assert res["reasons"]
    assert "Aucune tendance exploitable" in res["explanation"]


# --- Poids configurables -----------------------------------------------------------------------

def test_weights_can_be_overridden_from_configuration():
    parsed = trend_mod.parse_weights("ema:0.5,rsi:0.01")
    assert parsed["ema"] == 0.5 and parsed["rsi"] == 0.01
    assert parsed["macd"] == trend_mod.TREND_WEIGHTS["macd"]     # non cité = inchangé


def test_a_typo_in_the_weights_is_ignored_not_fatal():
    parsed = trend_mod.parse_weights("ema:abc,inconnu:0.9,,structure:0.4")
    assert parsed["ema"] == trend_mod.TREND_WEIGHTS["ema"]       # valeur illisible : on garde
    assert "inconnu" not in parsed
    assert parsed["structure"] == 0.4


def test_empty_weights_string_gives_the_defaults():
    assert trend_mod.parse_weights("") == trend_mod.TREND_WEIGHTS
