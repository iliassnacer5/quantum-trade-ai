"""AI Copilot (M5) — copilot de trading complet, routé par intention — Phase 3.

Le Copilot n'est plus figé sur un seul actif : il *route* la question de l'utilisateur vers les bons
outils existants (scan multi-paires, trades du jour backtestés, vérification de fiabilité d'un
symbole, état macro/sessions) puis assemble un contexte riche qu'il fait rédiger par le LLM en
streaming. Pour CHAQUE intention, une réponse déterministe de repli est fournie : le Copilot reste
pleinement utile sans clé LLM (propriété clé du codebase).

Intentions reconnues :
- ``todays_trades``    : « quels trades aujourd'hui ? » -> daily_picks (haute-conviction backtestés)
- ``market_overview``  : « comment sont les marchés ? » -> sessions + macro (VIX) + opportunités
- ``should_i_trade``   : « dois-je trader BTC ? »       -> signal + verdict checklist + niveaux
- ``scan``             : « meilleures paires crypto ? » -> scan classé par conviction
- ``analyze``          : symbole cité                    -> analyse multi-agents détaillée
- ``general``          : sinon                           -> portefeuille/watchlist + actif par défaut
"""

from __future__ import annotations

import logging
import re

from app.agents import llm
from app.data import macro as macro_data_mod, sessions as sessions_mod, symbols as symbols_catalog
from app.domain import indicators as ind
from app.models.entities import User
from app.models.signal import Timeframe
from app.repositories.store import AppStore
from app.services import signal_service

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Tu es l'AI Copilot de Quantum Trade AI, un copilot de trading EXPERT qui guide l'utilisateur au "
    "quotidien : état des marchés, opportunités du jour, et décision « trader ou s'abstenir » sur un "
    "actif donné. Tu expliques l'analyse multi-agents (technique, volume, sentiment, figures, macro, "
    "risque) de façon claire, pédagogique et actionnable, en français. Tu ne donnes JAMAIS de conseil "
    "financier personnalisé ni de garantie de gain ; tu rappelles systématiquement les risques et que "
    "« ne pas trader » est une décision valable. Tu t'appuies UNIQUEMENT sur le CONTEXTE fourni "
    "(données marché, scan, backtests et sorties d'agents) — tu n'inventes aucun chiffre."
)

# --- Vocabulaire d'intention (mots-clés FR/EN, sans accents pour matcher largement) ---
# Marqueurs temporels « aujourd'hui » génériques (ambigus seuls : « marchés aujourd'hui » = aperçu).
_KW_TODAY = ("aujourd'hui", "aujourdhui", "ce matin", "du jour", "today")
# Phrases explicites « quels trades faire » -> sélection du jour, sans ambiguïté.
_KW_TODAY_EXPLICIT = (
    "trades du jour", "trade du jour", "quoi trader", "que trader", "quels trades",
    "quel trade", "quels sont les trades", "picks", "que faire aujourd'hui",
    "que dois-je faire", "opportunites du jour", "what should i trade",
)
_KW_OVERVIEW = (
    "marche", "marches", "market", "etat", "regime", "climat", "ambiance", "sessions",
    "session", "vix", "macro", "risk-on", "risk off", "risk-off", "sentiment general",
    "comment sont", "tendance generale", "overview",
)
_KW_TRADE_DECISION = (
    "dois-je", "dois je", "faut-il", "faut il", "je trade", "je dois trader", "trader",
    "acheter", "vendre", "entrer", "long", "short", "bon trade", "bon plan", "j'achete",
    "should i", "buy", "sell", "go long", "go short",
)
_KW_SCAN = (
    "scanne", "scan", "meilleures paires", "meilleures pairs", "meilleurs", "opportunites",
    "opportunite", "top ", "classement", "screener", "screen", "best pairs",
)
_CLASS_KEYWORDS = {
    "crypto": ("crypto", "cryptos", "bitcoin", "altcoin"),
    "forex": ("forex", "fx", "devise", "devises", "paires de devises"),
    "stock": ("action", "actions", "stock", "stocks", "bourse", "equity", "equities"),
}


def _normalize(text: str) -> str:
    """Minuscule + suppression des accents pour un matching de mots-clés robuste."""
    repl = str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")
    return text.lower().translate(repl)


def _extract_symbol(message: str, default: str | None = None) -> str | None:
    """Extrait le premier symbole connu cité dans le message (« btc » -> BTC/USDT), sinon `default`."""
    # Tokens alphanumériques + paires « XXX/YYY » ou « XXX-YYY ».
    for raw in re.findall(r"[A-Za-z]{2,}(?:[/-][A-Za-z]{2,})?", message):
        cand = symbols_catalog.normalize(raw)
        if symbols_catalog.is_known(cand):
            return cand
        # Base crypto nue (ex. « btc » -> BTC/USDT) si pas déjà une paire.
        if "/" not in raw and "-" not in raw and symbols_catalog.is_known(f"{raw.upper()}/USDT"):
            return f"{raw.upper()}/USDT"
    return symbols_catalog.normalize(default) if default else None


def _detect_class(message: str) -> str | None:
    norm = _normalize(message)
    for cls, kws in _CLASS_KEYWORDS.items():
        if any(k in norm for k in kws):
            return cls
    return None


def detect_intent(message: str, symbol: str | None) -> str:
    """Route la question vers une intention selon mots-clés et présence d'un symbole."""
    norm = _normalize(message)
    has_trade_word = any(w in norm for w in ("trade", "trader", "picks"))

    # Décision explicite sur un symbole donné l'emporte (« dois-je trader BTC ? »).
    if symbol and any(k in norm for k in _KW_TRADE_DECISION):
        return "should_i_trade"
    # Sélection du jour : phrase explicite, ou « aujourd'hui » + un mot lié au trade.
    if any(k in norm for k in _KW_TODAY_EXPLICIT) or (
        any(k in norm for k in _KW_TODAY) and has_trade_word
    ):
        return "todays_trades"
    if any(k in norm for k in _KW_SCAN):
        return "scan"
    if any(k in norm for k in _KW_OVERVIEW):
        return "market_overview"
    if symbol:
        return "analyze"
    return "general"


# --------------------------------------------------------------------------------------
# Constructeurs de contexte : chacun renvoie (titre, texte_contexte, reponse_deterministe)
# --------------------------------------------------------------------------------------

_DISCLAIMER = (
    "Rappel : analyse automatisée, pas un conseil financier. Ne pas trader est une décision valable."
)


async def build_context(user: User, store: AppStore, asset: str) -> dict:
    """Génère un instantané d'analyse pour `asset` (réutilise le pipeline de signaux, sans notif)."""
    card = await signal_service.generate_for_user(
        user, store, asset=asset, timeframe=Timeframe.H1, notify=False
    )
    candles = await signal_service._load_candles(asset, Timeframe.H1)
    last = candles[-1].close if candles else 0.0
    rsi = ind.rsi([c.close for c in candles], 14) if len(candles) > 14 else None
    return {
        "asset": asset,
        "last_price": round(last, 8),
        "rsi": round(rsi, 1) if rsi is not None else None,
        "card": card,
        "signal": {
            "direction": card.direction.value,
            "confidence": card.confidence,
            "entry": card.entry,
            "stop_loss": card.stop_loss,
            "take_profit_1": card.take_profit_1,
            "rationale": card.rationale,
        },
        "agents": [
            {"name": a["name"], "score": a["score"], "confidence": a["confidence"], "rationale": a["rationale"]}
            for a in (card.agents or [])
        ],
    }


def _context_text(ctx: dict) -> str:
    lines = [
        f"Actif : {ctx['asset']} | Dernier prix : {ctx['last_price']} | RSI(14) : {ctx['rsi']}",
        f"Signal consolidé : {ctx['signal']['direction']} (confiance {ctx['signal']['confidence']}%) — "
        f"entrée {ctx['signal']['entry']}, stop {ctx['signal']['stop_loss']}, TP1 {ctx['signal']['take_profit_1']}",
        f"Justification : {ctx['signal']['rationale']}",
        "Détail des agents :",
    ]
    for a in ctx["agents"]:
        lines.append(f"  - {a['name']} : score {a['score']:+.2f}, conf {a['confidence']:.2f} — {a['rationale']}")
    return "\n".join(lines)


def _analyze_deterministic(ctx: dict) -> str:
    s = ctx["signal"]
    top = sorted(ctx["agents"], key=lambda a: abs(a["score"]), reverse=True)[:3]
    drivers = ", ".join(f"{a['name']} ({a['score']:+.2f})" for a in top) or "aucun signal marqué"
    return (
        f"Analyse de {ctx['asset']} (prix {ctx['last_price']}, RSI {ctx['rsi']}).\n"
        f"Biais consolidé : {s['direction']} avec une confiance de {s['confidence']}%.\n"
        f"Principaux moteurs : {drivers}.\n"
        f"Niveaux : entrée {s['entry']}, stop {s['stop_loss']}, objectif {s['take_profit_1']}.\n"
        f"{_DISCLAIMER}"
    )


async def _ctx_analyze(user: User, store: AppStore, symbol: str) -> tuple[str, str, str]:
    ctx = await build_context(user, store, symbol)
    return f"ANALYSE DÉTAILLÉE — {symbol}", _context_text(ctx), _analyze_deterministic(ctx)


async def _ctx_should_i_trade(user: User, store: AppStore, symbol: str) -> tuple[str, str, str]:
    """Décision « trader ou s'abstenir » : signal + checklist de fiabilité (backtest inclus)."""
    ctx = await build_context(user, store, symbol)
    card = ctx["card"]
    verify = await signal_service.verify_signal(
        symbol,
        card.timeframe.value if hasattr(card.timeframe, "value") else str(card.timeframe),
        confidence=card.confidence,
        consensus_pct=card.consensus_pct or 0,
        risk_reward=card.risk_reward or 0.0,
        mtf_aligned=(card.mtf or {}).get("aligned", 0),
        mtf_total=(card.mtf or {}).get("total", 0),
        adx=card.metrics.get("adx"),
        direction=card.direction.value,
    )
    verdict = verify["verdict"]
    verdict_label = {
        "strong": "✅ OUI — setup solide",
        "moderate": "⚠️ POSSIBLE mais prudence",
        "weak": "🔴 NON — signal faible",
        "skip": "⏸️ NON — pas de trade (biais neutre / non confirmé)",
    }.get(verdict, verdict)

    s = ctx["signal"]
    top = sorted(ctx["agents"], key=lambda a: abs(a["score"]), reverse=True)[:3]
    drivers = ", ".join(f"{a['name']} ({a['score']:+.2f})" for a in top) or "aucun moteur marqué"
    checks_txt = "\n".join(
        f"  - {'✅' if c['pass'] else '❌'} {c['label']} : {c['value']}" for c in verify["checks"]
    )
    bt = verify.get("backtest")
    bt_txt = (
        f"Backtest : {bt['win_rate']}% de réussite, profit factor {bt['profit_factor']}, "
        f"{bt['trades']} trades."
        if bt else "Backtest indisponible (données insuffisantes)."
    )

    context_text = (
        f"Décision demandée sur {symbol}.\n"
        f"Signal : {s['direction']} (confiance {s['confidence']}%) — entrée {s['entry']}, "
        f"stop {s['stop_loss']}, TP1 {s['take_profit_1']}, R/R {card.risk_reward}.\n"
        f"Verdict checklist : {verdict_label} ({verify['passed']}/{verify['total']} critères).\n"
        f"{bt_txt}\nCritères :\n{checks_txt}\n"
        f"Moteurs principaux : {drivers}."
    )
    tf = card.timeframe.value if hasattr(card.timeframe, "value") else str(card.timeframe)
    deterministic = (
        f"Dois-je trader {symbol} ? (unité de temps : {tf}) → {verdict_label}\n"
        f"Direction du signal : {s['direction']} (confiance {s['confidence']}%).\n"
        f"Critères validés : {verify['passed']}/{verify['total']}.\n{bt_txt}\n"
        f"Si tu prends le trade : entrée {s['entry']}, stop {s['stop_loss']}, objectif {s['take_profit_1']} "
        f"(R/R {card.risk_reward}).\nMoteurs : {drivers}.\n{_DISCLAIMER}"
    )
    return f"DÉCISION — {symbol} ({tf})", context_text, deterministic


async def _ctx_todays_trades(user: User, store: AppStore, symbol: str) -> tuple[str, str, str]:
    """Trades du jour : setups haute-conviction CONFIRMÉS par backtest, par marché."""
    picks = await signal_service.daily_picks()
    if not picks:
        msg = (
            "Aucun trade fiable à forte conviction aujourd'hui. C'est un signal en soi : mieux vaut "
            "s'abstenir que forcer un mauvais trade. Reviens plus tard ou élargis ta surveillance.\n"
            f"{_DISCLAIMER}"
        )
        return "TRADES DU JOUR", "Aucun setup retenu aujourd'hui (aucun n'a passé le filtre backtest).", msg

    by_class: dict[str, list[dict]] = {}
    for p in picks:
        by_class.setdefault(p["asset_class"], []).append(p)
    labels = {"crypto": "Crypto", "forex": "Forex", "stock": "Actions",
              "commodity": "Or & Métaux", "index": "Indices"}
    lines: list[str] = []
    for cls, items in by_class.items():
        lines.append(f"{labels.get(cls, cls)} :")
        for p in items:
            bt = p.get("backtest") or {}
            # On annonce ce que la STRATÉGIE mesure : sa lecture de la tendance et son R/R.
            trend = f"tendance {p['trend_score']}/100" if p.get("trend_score") is not None else p["trend"]
            rr = f" | R/R 1:{p['risk_reward']:.2f}" if p.get("risk_reward") else ""
            lines.append(
                f"  - {p['symbol']} {p['direction']} @ {p['price']} | {trend}{rr} | "
                f"réussite {bt.get('win_rate')}% | PF {bt.get('profit_factor')}"
            )
    context_text = "\n".join(lines)
    deterministic = (
        f"🎯 Trades du jour ({len(picks)} retenu(s), confirmés par backtest) :\n{context_text}\n"
        f"Ce sont les setups les plus fiables du moment, pas un ordre d'achat. {_DISCLAIMER}"
    )
    return "TRADES DU JOUR", context_text, deterministic


async def _ctx_scan(user: User, store: AppStore, symbol: str, asset_class: str | None) -> tuple[str, str, str]:
    results = await signal_service.scan_market(asset_class=asset_class, timeframe="1h", limit=12)
    cls_label = {"crypto": "crypto", "forex": "forex", "stock": "actions"}.get(asset_class, "tous marchés")
    if not results:
        msg = f"Aucun symbole exploitable sur {cls_label} pour le moment.\n{_DISCLAIMER}"
        return "SCAN", "Scan vide.", msg
    top = results[:8]
    lines = [
        f"  - {r['symbol']} {r['direction']} @ {r['price']} | conviction {r['conviction']} | "
        f"ADX {r['adx']} | RSI {r['rsi']}" + (" ★" if r["high_conviction"] else "")
        for r in top
    ]
    context_text = f"Scan {cls_label} (classé par conviction) :\n" + "\n".join(lines)
    hc = sum(1 for r in results if r["high_conviction"])
    deterministic = (
        f"🔍 Meilleures opportunités {cls_label} (top {len(top)}, {hc} ★ haute-conviction) :\n"
        + "\n".join(lines) + f"\n★ = ADX>25 + tendance franche. {_DISCLAIMER}"
    )
    return f"SCAN — {cls_label}", context_text, deterministic


async def _ctx_market_overview(user: User, store: AppStore, symbol: str) -> tuple[str, str, str]:
    """État des marchés : sessions ouvertes + macro (VIX/taux) + opportunités haute-conviction."""
    overview = sessions_mod.overview()
    macro = await macro_data_mod.fetch_macro_data()
    vix = macro.get("vix")
    if vix is None:
        regime = "régime indéterminé (VIX indisponible)"
    elif vix < 15:
        regime = f"risk-ON (calme, VIX {vix})"
    elif vix < 25:
        regime = f"neutre (VIX {vix})"
    else:
        regime = f"risk-OFF (stress, VIX {vix})"

    open_sessions = [s["label"] for s in overview["sessions"] if s["open"]] or ["aucune (hors sessions)"]

    # Aperçu rapide des opportunités haute-conviction par classe.
    opp_lines: list[str] = []
    for cls, label in (("crypto", "Crypto"), ("forex", "Forex"), ("stock", "Actions")):
        try:
            res = await signal_service.scan_market(asset_class=cls, timeframe="1h", limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scan overview %s échoué (%s)", cls, exc)
            continue
        hc = [r for r in res if r["high_conviction"]]
        best = hc[0] if hc else (res[0] if res else None)
        if best:
            tag = f"{best['symbol']} {best['direction']} (conv. {best['conviction']})"
            opp_lines.append(f"  - {label} : {len(hc)} setup(s) ★ — meilleur : {tag}")
        else:
            opp_lines.append(f"  - {label} : rien de marqué")

    context_text = (
        f"Heure : {overview['utc_time']}.\n"
        f"Sessions ouvertes : {', '.join(open_sessions)}.\n"
        f"Régime de risque : {regime}. Taux : {macro.get('rate_trend')}, inflation : {macro.get('inflation')}.\n"
        f"Opportunités haute-conviction :\n" + "\n".join(opp_lines)
    )
    deterministic = (
        f"📊 État des marchés ({overview['utc_time']}) :\n"
        f"• Sessions ouvertes : {', '.join(open_sessions)}\n"
        f"• Régime : {regime}\n"
        f"• Opportunités :\n" + "\n".join(opp_lines) + f"\n{_DISCLAIMER}"
    )
    return "ÉTAT DES MARCHÉS", context_text, deterministic


async def _ctx_general(user: User, store: AppStore, symbol: str) -> tuple[str, str, str]:
    """Repli : si un actif par défaut existe, on analyse ; sinon on oriente l'utilisateur."""
    if symbol:
        return await _ctx_analyze(user, store, symbol)
    watch = ", ".join(user.watchlist) if getattr(user, "watchlist", None) else "non définie"
    context_text = f"Pas d'actif ni d'intention claire. Watchlist de l'utilisateur : {watch}."
    deterministic = (
        "Je suis ton copilot de trading. Tu peux me demander :\n"
        "• « Comment sont les marchés aujourd'hui ? »\n"
        "• « Quels trades je dois faire aujourd'hui ? »\n"
        "• « Dois-je trader BTC ? » (ou un autre symbole)\n"
        "• « Les meilleures paires forex ? »\n"
        f"Ta watchlist : {watch}.\n{_DISCLAIMER}"
    )
    return "AIDE", context_text, deterministic


async def _route(user: User, store: AppStore, asset: str | None, message: str) -> tuple[str, str, str]:
    """Détecte l'intention et construit (titre, contexte, repli déterministe)."""
    symbol = _extract_symbol(message, asset)
    intent = detect_intent(message, symbol)
    logger.info("Copilot intent=%s symbol=%s", intent, symbol)

    if intent == "todays_trades":
        return await _ctx_todays_trades(user, store, symbol)
    if intent == "should_i_trade":
        return await _ctx_should_i_trade(user, store, symbol)
    if intent == "scan":
        return await _ctx_scan(user, store, symbol, _detect_class(message))
    if intent == "market_overview":
        return await _ctx_market_overview(user, store, symbol)
    if intent == "analyze":
        return await _ctx_analyze(user, store, symbol)
    return await _ctx_general(user, store, symbol)


async def answer_stream(user: User, store: AppStore, asset: str | None, question: str):
    """Async generator de fragments de texte (pour SSE). Inclut un repli déterministe par intention."""
    title, context_text, deterministic = await _route(user, store, asset, question)
    prompt = (
        f"CONTEXTE ({title}) :\n{context_text}\n\n"
        f"QUESTION DE L'UTILISATEUR : {question}\n\n"
        f"Réponds de façon concise, structurée et actionnable en t'appuyant UNIQUEMENT sur le contexte. "
        f"Termine par un rappel de risque court."
    )
    if not llm.available():
        yield deterministic
        return
    try:
        async for piece in llm.stream(prompt, role="reasoning", system=SYSTEM_PROMPT, max_tokens=800):
            yield piece
    except llm.LLMUnavailable:
        yield deterministic
