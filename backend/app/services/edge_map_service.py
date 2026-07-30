"""Carte de l'edge — OÙ la stratégie du desk fonctionne, marché par marché.

Le desk n'applique qu'UNE stratégie (le playbook). La question n'est donc plus « quelle stratégie
choisir » mais « sur quels symboles et quelles unités de temps celle-ci a un edge démontré ». Le
sweep passe chaque symbole × chaque unité de temps au walk-forward avec frais, et classe :
  🟢 green  : alpha > 0 ET profit factor ≥ 1,2 (exploitable)
  🟡 yellow : alpha > 0 (à surveiller)
  🔴 red    : pas d'edge (à éviter)

Le résultat est stocké (record `edge_map`) avec un `green_streak` par combo : un edge qui clignote
n'est pas un edge — l'auto-trading papier ne prend que les combos verts stables.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# L'identifiant unique de la stratégie du desk : conservé dans les enregistrements pour que les
# cartes déjà stockées restent lisibles, et parce que l'auto-trading interroge la carte par clé.
STRATEGY_ID = "playbook"
STRATEGY_NAME = "Playbook — confluence multi-indicateurs"

#: Univers balayé : TOUT le catalogue, comme la stratégie elle-même. Une carte de l'edge qui ne
#: couvre qu'un sixième des symboles ne peut pas répondre à « où gagne-t-on », elle répond à « où
#: gagne-t-on parmi ceux que j'ai choisi de regarder » — ce qui est une autre question.
def universe() -> dict[str, list[str]]:
    from app.data import symbols as symbols_catalog

    out: dict[str, list[str]] = {}
    for item in symbols_catalog.all_symbols():
        out.setdefault(item["asset_class"], []).append(item["symbol"])
    return out


TIMEFRAMES = ["4h", "1d"]


# Un « vert » à 1-2 trades est du bruit (PF 10 / win 100% par chance) : échantillon minimal requis.
MIN_TRADES_GREEN = 8


def _classify(alpha: float, pf: float, trades: int = 0) -> str:
    if alpha > 0 and pf >= 1.2 and trades >= MIN_TRADES_GREEN:
        return "green"
    if alpha > 0:
        return "yellow"  # inclut les combos prometteurs mais à échantillon insuffisant
    return "red"


async def _preload(symbol: str, timeframe: str):
    """Charge une fois les bougies horodatées d'un symbole (repli synthétique tracé)."""
    from app.data.ohlcv import get_ohlcv
    from app.domain.indicators import Candle

    rows = await get_ohlcv(symbol, interval=timeframe, limit=1000)
    data_real = len(rows) >= 100
    candles = [
        Candle(r["open"], r["high"], r["low"], r["close"], r.get("volume", 0.0),
               timestamp=datetime.fromtimestamp(r["time"], UTC))
        for r in rows
    ] if data_real else []
    return candles, data_real


async def run_edge_sweep(store, timeframes: list[str] | None = None,
                         markets: list[str] | None = None) -> dict:
    """Exécute le sweep complet et persiste la carte. Retourne le payload stocké.

    Depuis que l'univers est le catalogue COMPLET, un passage couvre ~88 symboles × 2 unités de
    temps, soit environ 176 walk-forwards au lieu de 48 : compter une vingtaine de minutes plutôt
    que 1 à 3. C'est un travail de nuit, et le prix à payer pour que la carte réponde vraiment à
    « où gagne-t-on » plutôt qu'à « où gagne-t-on parmi les symboles que j'ai choisi de regarder ».
    `markets` et `timeframes` permettent de restreindre un passage manuel.
    """
    from app.backtest.walkforward import walk_forward

    tfs = timeframes or TIMEFRAMES
    rows: list[dict] = []
    # Métriques de LA STRATÉGIE, mesurées par le backtest : espérance en R, profit factor et
    # fréquence par symbole. Le walk-forward répond « cette combinaison bat-elle le buy & hold ? »,
    # le backtest répond « qu'est-ce que la stratégie y gagne par trade, et à quelle cadence ? ».
    # Les deux sont affichés côte à côte : ce ne sont pas les mêmes questions.
    playbook_stats = _playbook_stats(store)

    for market, symbols in universe().items():
        if markets and market not in markets:
            continue
        for symbol in symbols:
            for tf in tfs:
                try:
                    preloaded = await _preload(symbol, tf)
                except Exception as exc:  # noqa: BLE001 — un symbole HS ne bloque pas le sweep
                    logger.warning("Edge sweep : données %s %s indisponibles (%s)", symbol, tf, exc)
                    continue
                try:
                    r = await walk_forward(symbol, tf, folds=4, preloaded=preloaded)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Edge sweep : %s %s échoué (%s)", symbol, tf, exc)
                    continue
                alpha = r.get("avg_alpha_pct", 0.0) or 0.0
                pf = r.get("avg_profit_factor", 0.0) or 0.0
                n_trades = r.get("total_trades", 0) or 0
                rows.append({
                    "strategy": STRATEGY_ID, "strategy_name": STRATEGY_NAME, "symbol": symbol,
                    "market": market, "timeframe": tf,
                    "alpha": alpha, "pf": pf, "win": r.get("avg_win_rate", 0.0),
                    "trades": n_trades, "verdict": r.get("verdict"),
                    "data_real": bool(r.get("data_real", preloaded[1])),
                    "status": _classify(alpha, pf, n_trades),
                    # Ce que LA STRATÉGIE mesure sur ce symbole (backtest, toutes UT confondues).
                    "playbook": playbook_stats.get(symbol),
                })

    # Stabilité : un combo vert AUJOURD'HUI ET la fois précédente vaut plus qu'un vert isolé.
    prev = (store.records.get("edge_map", "latest") or {}).get("rows", [])
    prev_streak = {f"{p['strategy']}|{p['symbol']}|{p['timeframe']}": p.get("green_streak", 0) for p in prev}
    for row in rows:
        key = f"{row['strategy']}|{row['symbol']}|{row['timeframe']}"
        row["green_streak"] = (prev_streak.get(key, 0) + 1) if row["status"] == "green" else 0

    greens = [r for r in rows if r["status"] == "green"]
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "strategy": STRATEGY_NAME,
        "rows": sorted(rows, key=lambda r: (r["alpha"], r["pf"]), reverse=True),
        "greens": len(greens),
        "yellows": len([r for r in rows if r["status"] == "yellow"]),
        "reds": len([r for r in rows if r["status"] == "red"]),
        "symbols": sum(len(v) for v in universe().values()),
        "timeframes": tfs,
        # Résumé des métriques de la stratégie, pour que la carte affiche ce que LA MÉTHODE gagne et
        # pas seulement si elle bat le buy & hold.
        "playbook_summary": _playbook_summary(playbook_stats),
        "note": (f"✅ {len(greens)} combo(s) exploitables (alpha>0, PF≥1,2 out-of-sample)." if greens
                 else "⚠️ Aucun combo vert actuellement — s'abstenir est la bonne décision."),
    }
    store.records.put("edge_map", "latest", payload)
    store.records.put("edge_map", datetime.now(UTC).date().isoformat(), payload)
    logger.info("Edge sweep terminé : %d combos (%d verts)", len(rows), len(greens))
    return payload


def _playbook_stats(store) -> dict[str, dict]:  # noqa: ANN001
    """Espérance, profit factor et fréquence PAR SYMBOLE, tels que le backtest les a mesurés.

    On lit en priorité la passe LONGUE (5 ans) : c'est elle qui dit où la stratégie tient sur la
    durée. À défaut, la passe portée. Rien n'est recalculé ici — un sweep ne doit pas relancer un
    backtest de plusieurs heures pour afficher une colonne.
    """
    try:
        rec = store.records.get("playbook_backtest", "latest") or {}
    except Exception:  # noqa: BLE001 — une carte sans ces colonnes reste utilisable
        return {}
    source = rec.get("long") or rec.get("scope") or {}
    out: dict[str, dict] = {}
    for row in source.get("ranking") or []:
        if not row.get("trades"):
            continue
        out[row["symbol"]] = {
            "trades": row["trades"],
            "win_rate": row["win_rate"],
            "expectancy_r": row["expectancy_r"],
            "profit_factor": row["profit_factor"],
            "max_drawdown_r": row["max_drawdown_r"],
            "trades_per_day": row.get("trades_per_day"),
            "days_between_trades": row.get("days_between_trades"),
            "r_per_month": row.get("r_per_month"),
            "rank": row.get("rank"),
            "verdict": row.get("verdict"),
        }
    return out


def _playbook_summary(stats: dict[str, dict]) -> dict:
    """Ce que la stratégie produit sur l'ensemble des symboles mesurés (pas une moyenne de façade)."""
    rows = list(stats.values())
    if not rows:
        return {"measured": False,
                "note": ("Backtest de la stratégie pas encore passé : la carte n'affiche donc que "
                         "le walk-forward (bat-on le buy & hold ?), pas l'espérance par trade.")}
    total = sum(r["trades"] for r in rows)
    exp = sum(r["expectancy_r"] * r["trades"] for r in rows) / total
    rate = sum(r.get("trades_per_day") or 0.0 for r in rows)
    return {
        "measured": True,
        "symbols": len(rows),
        "trades": total,
        "expectancy_r": round(exp, 3),
        "trades_per_day": round(rate, 2),
        "trades_per_week": round(rate * 7, 1),
        "note": (
            f"Stratégie du desk mesurée sur {len(rows)} symboles : {total} trades, espérance "
            f"{exp:+.2f} R par trade, cadence {rate:.1f} trade(s) par jour et {rate * 7:.0f} par "
            "semaine sur cet univers."
        ),
    }


def get_edge_map(store) -> dict | None:
    return store.records.get("edge_map", "latest")


def is_combo_green(store, strategy_id: str, symbol: str, min_streak: int = 1) -> bool:
    """Vrai si (stratégie, symbole) est vert sur AU MOINS un timeframe balayé, avec la stabilité requise.

    Utilisé par l'auto-trading papier : on ne trade automatiquement que là où l'edge est prouvé."""
    latest = get_edge_map(store)
    if not latest:
        return False
    return any(
        r["strategy"] == strategy_id and r["symbol"] == symbol
        and r["status"] == "green" and r.get("green_streak", 0) >= min_streak
        for r in latest.get("rows", [])
    )
