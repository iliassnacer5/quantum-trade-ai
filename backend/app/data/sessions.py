"""Sessions de trading mondiales (Asie / Londres / New York).

Chaque session a sa fenêtre horaire (UTC) et ses actifs les plus liquides — donc les plus fiables à
trader pendant cette session. La crypto est 24/7 (toujours incluse) ; le forex et les actions sont
filtrés selon la session active. Permet de scanner « les bonnes paires au bon moment ».
"""

from __future__ import annotations

from datetime import datetime, timezone

# Cryptos majeures — liquides en continu, incluses dans toutes les sessions.
_CRYPTO = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT"]

SESSIONS: dict[str, dict] = {
    "asian": {
        "label": "Asie (Tokyo/Sydney)",
        "start": 23, "end": 8,  # UTC (chevauche minuit)
        "forex": ["USD/JPY", "EUR/JPY", "GBP/JPY", "AUD/JPY", "AUD/USD", "NZD/USD"],
        "stocks": [],  # marchés US/EU fermés
    },
    "london": {
        "label": "Londres (Europe)",
        "start": 7, "end": 16,
        "forex": ["EUR/USD", "GBP/USD", "EUR/GBP", "EUR/CHF", "GBP/CHF", "USD/CHF", "EUR/JPY"],
        "stocks": [],  # actions EU non couvertes ; US en pré-marché
    },
    "newyork": {
        "label": "New York (Amérique)",
        "start": 12, "end": 21,
        "forex": ["EUR/USD", "GBP/USD", "USD/CAD", "USD/JPY", "USD/CHF"],
        "stocks": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD", "NFLX", "JPM"],
    },
}



# --- Fenêtres à FORTE valeur (« kill zones ») exigées par la stratégie ------------------------
# Heures UTC décimales. Ce sont les moments où le volume institutionnel entre réellement :
# les premières heures d'ouverture de Londres et de New York, et surtout leur CHEVAUCHEMENT
# (la fenêtre la plus liquide et la plus directionnelle de la journée forex).
# NB : les marchés actions US ouvrent à 13:30 UTC (heure d'été) ou 14:30 UTC (heure d'hiver) —
# la fenêtre New York couvre les deux cas.
KILL_ZONES: list[dict] = [
    {"id": "london_open", "label": "Ouverture de Londres (premières heures)",
     "start": 7.0, "end": 10.0, "quality": 0.85},
    {"id": "overlap", "label": "Chevauchement Londres / New York",
     "start": 12.0, "end": 16.0, "quality": 1.0},
    {"id": "newyork_open", "label": "Ouverture de New York (premières heures)",
     "start": 12.5, "end": 15.5, "quality": 0.9},
]

# Qualité de timing hors kill zone.
_QUALITY_IN_SESSION = 0.6      # session ouverte mais hors fenêtre à forte valeur
_QUALITY_OFF_SESSION = 0.3     # aucune session majeure (Asie seule / creux)


def _is_open(start: int, end: int, hour: int) -> bool:
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def _in_window(start: float, end: float, h: float) -> bool:
    return start <= h < end if start <= end else (h >= start or h < end)


def current_sessions(now: datetime | None = None) -> list[str]:
    """Sessions actuellement ouvertes (elles se chevauchent)."""
    h = (now or datetime.now(timezone.utc)).hour
    return [name for name, s in SESSIONS.items() if _is_open(s["start"], s["end"], h)]


def active_kill_zones(now: datetime | None = None) -> list[dict]:
    """Fenêtres à forte valeur actuellement actives (ouverture Londres/NY, chevauchement)."""
    now = now or datetime.now(timezone.utc)
    h = now.hour + now.minute / 60
    return [z for z in KILL_ZONES if _in_window(z["start"], z["end"], h)]


def is_overlap(now: datetime | None = None) -> bool:
    """Vrai pendant le chevauchement Londres / New York — la meilleure fenêtre de la journée."""
    return any(z["id"] == "overlap" for z in active_kill_zones(now))


def session_context(now: datetime | None = None) -> dict:
    """Contexte de timing utilisé par le playbook et par tous les agents.

    Retourne la ou les sessions ouvertes, les fenêtres à forte valeur actives, une note de
    qualité [0-1] et `prime` (vrai = moment privilégié pour entrer en position).
    """
    now = now or datetime.now(timezone.utc)
    active = current_sessions(now)
    zones = active_kill_zones(now)
    if zones:
        best = max(zones, key=lambda z: z["quality"])
        quality = best["quality"]
        label = best["label"]
        if len(zones) > 1:
            label = " + ".join(z["label"] for z in sorted(zones, key=lambda z: -z["quality"]))
    elif active and active != ["asian"]:
        quality, label = _QUALITY_IN_SESSION, "session ouverte, hors fenêtre à forte valeur"
    else:
        quality = _QUALITY_OFF_SESSION
        label = "hors sessions majeures (liquidité faible)" if not active else "session asiatique seule"
    return {
        "utc_time": now.strftime("%H:%M UTC"),
        "active": active,
        "active_labels": [SESSIONS[a]["label"] for a in active],
        "kill_zones": [z["id"] for z in zones],
        "overlap": any(z["id"] == "overlap" for z in zones),
        "quality": quality,
        "prime": quality >= 0.85,          # ouverture Londres, ouverture NY ou chevauchement
        "label": label,
        "next_window": _next_window(now),
    }


def _next_window(now: datetime) -> dict | None:
    """Prochaine fenêtre à forte valeur (pour « sois attentif dans X minutes »)."""
    h = now.hour + now.minute / 60
    upcoming = sorted(
        [(z["start"] - h if z["start"] >= h else z["start"] + 24 - h, z) for z in KILL_ZONES],
        key=lambda t: t[0],
    )
    if not upcoming:
        return None
    delta_h, zone = upcoming[0]
    return {"id": zone["id"], "label": zone["label"],
            "starts_in_minutes": int(round(delta_h * 60)),
            "window_utc": f"{int(zone['start']):02d}:{int(zone['start'] % 1 * 60):02d}–"
                          f"{int(zone['end']):02d}:{int(zone['end'] % 1 * 60):02d} UTC"}


def session_universe(session: str) -> list[dict]:
    """Actifs pertinents pour une session : crypto (toujours) + forex + actions de la session."""
    s = SESSIONS.get(session)
    if not s:
        return []
    out = [{"symbol": c, "asset_class": "crypto"} for c in _CRYPTO]
    out += [{"symbol": f, "asset_class": "forex"} for f in s["forex"]]
    out += [{"symbol": a, "asset_class": "stock"} for a in s["stocks"]]
    return out


def overlap_universe() -> list[dict]:
    """Univers du chevauchement Londres/New York : les paires que les deux desks traitent.

    C'est la fenêtre que la stratégie demande de surveiller en priorité — on y scanne
    l'intersection Londres ∩ New York (majeures USD/EUR/GBP) plus l'or et les cryptos majeures.
    """
    pairs = sorted(set(SESSIONS["london"]["forex"]) | set(SESSIONS["newyork"]["forex"]))
    out = [{"symbol": p, "asset_class": "forex"} for p in pairs]
    out += [{"symbol": s, "asset_class": "commodity"} for s in ("XAU/USD", "XAG/USD")]
    out += [{"symbol": s, "asset_class": "stock"} for s in SESSIONS["newyork"]["stocks"][:6]]
    out += [{"symbol": c, "asset_class": "crypto"} for c in _CRYPTO[:4]]
    return out


def overview(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    active = current_sessions(now)
    ctx = session_context(now)
    return {
        "utc_time": now.strftime("%H:%M UTC"),
        "active": active,
        "context": ctx,
        "kill_zones": [
            {**z, "active": z["id"] in ctx["kill_zones"],
             "window_utc": f"{int(z['start']):02d}:{int(z['start'] % 1 * 60):02d}–"
                           f"{int(z['end']):02d}:{int(z['end'] % 1 * 60):02d}"}
            for z in KILL_ZONES
        ],
        "sessions": [
            {
                "id": name,
                "label": s["label"],
                "window_utc": f"{s['start']:02d}:00–{s['end']:02d}:00",
                "open": name in active,
                "symbol_count": len(session_universe(name)),
            }
            for name, s in SESSIONS.items()
        ],
    }
