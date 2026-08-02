"""Automatisation quotidienne — pré-calcul des trades fiables + envoi du digest.

Boucle asyncio (sans dépendance native) lancée au démarrage : chaque jour à l'heure configurée
(`daily_digest_hour` UTC), elle calcule la sélection (`daily_picks`), la met en cache, et notifie
les utilisateurs ayant activé le digest, via leurs canaux d'alerte (email / Telegram / push).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.alerts import notifier
from app.core.config import get_settings
from app.services import signal_service

logger = logging.getLogger(__name__)


async def _format_portfolio_and_journal(store, tenant_id: str) -> str:  # noqa: ANN001
    """État du portefeuille (solde, équité, positions ouvertes) + du journal (KPI), pour un tenant.

    Personnel à chaque utilisateur — contrairement aux trades du jour, qui sont les mêmes pour
    tout le monde — donc calculé par tenant plutôt que diffusé une seule fois.
    """
    from app.services import journal_service, wallet_service

    lines = ["\n— — —\n💼 Portefeuille"]
    try:
        w = await wallet_service.compute_wallet(store, tenant_id)
        st = w["stats"]
        lines.append(
            f"Solde : {w['balance']:.2f} | Équité : {w['equity']:.2f} "
            f"({w['return_pct']:+.2f} % depuis le départ)"
        )
        lines.append(
            f"Trades clôturés : {st['trades']} ({st['wins']}G / {st['losses']}P, "
            f"{st['win_rate']} % de réussite) | Profit factor {st['profit_factor']}"
        )
        if w["positions"]:
            lines.append(f"{len(w['positions'])} position(s) ouverte(s) :")
            for p in w["positions"][:10]:
                lines.append(
                    f"  • {p['symbol']} {p['side']} @ {p['entry']:g} — "
                    f"P&L latent {p['unrealized_pnl']:+.2f}"
                )
        else:
            lines.append("Aucune position ouverte.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("État portefeuille indisponible pour digest (%s)", exc)
        lines.append("Indisponible pour le moment.")

    lines.append("\n📓 Journal")
    try:
        entries = journal_service.all_entries(store, tenant_id)
        js = journal_service.stats(entries)
        lines.append(
            f"{js['closed']} trade(s) clôturé(s), {js['open']} en cours — "
            f"{js['wins']}G / {js['losses']}P ({js['win_rate']} %) | P&L cumulé {js['total_pnl']:+.2f}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("État journal indisponible pour digest (%s)", exc)
        lines.append("Indisponible pour le moment.")
    return "\n".join(lines)


def _format_digest(picks: list[dict]) -> str:
    if not picks:
        return "Aucun trade fiable à forte conviction aujourd'hui. Mieux vaut s'abstenir."
    lines = ["📈 Trades du jour :"]
    for p in picks:
        bt = p.get("backtest") or {}
        tag = "✅ confirmé" if p.get("tier") == "confirmed" else "👀 à surveiller (non confirmé)"
        bt_txt = f" — backtest {bt.get('win_rate')}% / PF {bt.get('profit_factor')}" if bt else ""
        lines.append(f"• {p['symbol']} {p['direction']} [{tag}] — ADX {p['adx']}{bt_txt}")
    lines.append("\nAide à la décision, pas un conseil. Vérifie R/R et taille de position avant d'agir.")
    return "\n".join(lines)


#: Une notification n'est pas un tableau de bord. La sélection n'est plus plafonnée (tous les
#: setups conformes sont rendus par l'API et affichés dans l'interface), mais un message Telegram
#: de soixante trades n'est plus lu : on n'y met que la tête du classement, en disant combien il en
#: reste et où les voir.
_NOTIFY_MAX = 12


def format_top_trades(payload: dict) -> str:
    """Message des trades du jour issus de la STRATÉGIE (playbook) — format note de desk."""
    all_picks = payload.get("picks") or []
    picks = all_picks[:_NOTIFY_MAX]
    sess = payload.get("session") or {}
    head = f"🎯 Trades du jour — {payload.get('strategy', '')}\n🕒 {sess.get('label')} ({sess.get('utc_time')})"
    if not picks:
        return head + "\n\n" + payload.get("note", "Aucun setup conforme à la stratégie pour l'instant.")
    lines = [head, ""]
    for i, p in enumerate(picks, 1):
        if p["tier"] == "ready":
            lines.append(
                f"{i}. ✅ {p['symbol']} {p['direction']} @ {p['entry']:g}\n"
                f"   SL {p['stop_loss']:g} ({p['risk_pips']:g} {p['pips_label']}) · "
                f"TP1 {p['take_profit_1']:g} ({p['reward_pips']:g}) · R/R 1:{p['risk_reward']:g}\n"
                f"   Déclencheur 15 min : {p['trigger']}"
            )
        else:
            lines.append(
                f"{i}. 🟡 {p['symbol']} {p['direction']} — contexte validé (mensuel/journalier/4 h), "
                + ("ouverture AUTOMATIQUE en démo dès le déclencheur 15 min"
                   if get_settings().playbook_auto_entry_enabled
                   else "en attente du déclencheur 15 min")
            )
    lines.append("")
    if len(all_picks) > len(picks):
        lines.append(
            f"… et {len(all_picks) - len(picks)} autre(s) setup(s) conforme(s) — la liste complète "
            "est sur la page « Trades du jour »."
        )
    lines.append(payload.get("note", ""))
    lines.append("Aide à la décision, pas un conseil en investissement. Aucun trade n'est garanti gagnant.")
    return "\n".join(lines)


async def run_daily_top_trades(store, *, notify: bool = True) -> dict:  # noqa: ANN001
    """Calcule les trades du jour selon la stratégie, les met en cache et notifie les abonnés."""
    today = datetime.now(UTC).date().isoformat()
    payload = await signal_service.daily_top_trades()
    payload["date"] = today
    store.records.put("top_trades", today, payload)
    if notify:
        await _broadcast(store, format_top_trades(payload), "Trades du jour 🎯")
    logger.info(
        "Trades du jour (playbook) : %d retenu(s) dont %d exécutable(s)",
        len(payload.get("picks") or []), payload.get("ready", 0),
    )
    return payload


async def _auto_execute_paper(store, payload: dict) -> str:  # noqa: ANN001
    """Ouvre automatiquement les setups prêts en COMPTE DÉMO à l'entrée d'une fenêtre de session.

    Délègue à `auto_entry_service` — la MÊME veille que celle qui tourne en continu, avec les mêmes
    garde-fous (papier uniquement, recalcul de la stratégie, pas de doublon). Dupliquer cette
    logique ici ferait vivre deux chemins d'exécution qui finiraient par diverger.
    """
    from app.services import auto_entry_service

    candidates = [p for p in (payload.get("picks") or []) if p.get("tier") in ("ready", "armed")]
    if not candidates:
        return ""
    try:
        report = await auto_entry_service.run_auto_entry(store, candidates=candidates)
    except Exception as exc:  # noqa: BLE001 — l'exécution ne doit pas casser la veille de session
        logger.warning("Auto-exécution démo échouée à l'ouverture de session (%s)", exc)
        return ""
    if not report.get("opened"):
        return ""
    lines = [
        f"📥 {o['symbol']} {o['side'].upper()} {o['qty']} @ {o['entry']} — "
        f"SL {o['stop_loss']} · TP {o['take_profit']} (R/R 1:{o['risk_reward']})"
        for o in report["opened"]
    ]
    return "🧪 Ouvert automatiquement en COMPTE DÉMO :\n" + "\n".join(lines)


async def _broadcast(store, text: str, push_title: str) -> int:  # noqa: ANN001
    """Envoie `text` à tous les utilisateurs abonnés au digest, via leurs canaux d'alerte."""
    sent = 0
    try:
        users = store.users.list_all()
    except Exception:  # noqa: BLE001
        users = []
    for user in users:
        if not getattr(user, "daily_digest", False):
            continue
        try:
            if user.alert_email and user.email:
                await notifier.send_email(user.email, "Quantum Trade AI — Trades du jour", text)
            if user.alert_telegram and user.telegram_chat_id:
                await notifier.send_telegram(user.telegram_chat_id, text)
            if user.push_token:
                await notifier.send_push(user.push_token, push_title)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — un échec ne doit pas bloquer les autres
            logger.warning("Notification non envoyée à %s (%s)", user.email, exc)
    return sent


async def run_daily_digest(store) -> dict:  # noqa: ANN001
    """Calcule la sélection du jour, la met en cache et envoie le digest aux abonnés."""
    today = datetime.now(UTC).date().isoformat()
    picks = await signal_service.daily_picks()
    rec = store.records.put(
        "daily_picks", today, {"date": today, "picks": picks, "generated_at": datetime.now(UTC).isoformat()},
    )
    text = _format_digest(picks)
    # Les trades du jour issus de la STRATÉGIE partent dans le même digest matinal.
    try:
        top = await run_daily_top_trades(store, notify=False)
        text = format_top_trades(top) + "\n\n— — —\n" + text
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trades du jour (playbook) indisponibles (%s)", exc)
    sent = 0
    try:
        users = store.users.list_all()
    except Exception:  # noqa: BLE001
        users = []
    for user in users:
        if not getattr(user, "daily_digest", False):
            continue
        try:
            if user.alert_email and user.email:
                state = await _format_portfolio_and_journal(store, user.tenant_id)
                await notifier.send_email(
                    user.email, "Quantum Trade AI — Trades du jour", text + state,
                )
            if user.alert_telegram and user.telegram_chat_id:
                await notifier.send_telegram(user.telegram_chat_id, text)
            if user.push_token:
                await notifier.send_push(user.push_token, "Trades du jour disponibles 📈")
            sent += 1
        except Exception as exc:  # noqa: BLE001 — un échec ne doit pas bloquer les autres
            logger.warning("Digest non envoyé à %s (%s)", user.email, exc)
    logger.info("Digest quotidien : %d trades, %d utilisateurs notifiés", len(picks), sent)
    return rec


async def daily_loop() -> None:
    """Boucle : attend l'heure du jour configurée puis lance le digest, en continu."""
    from app.repositories.store import get_store

    while True:
        settings = get_settings()
        now = datetime.now(UTC)
        target = now.replace(hour=settings.daily_digest_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        try:
            await run_daily_digest(get_store())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec du digest quotidien (%s)", exc)


async def session_watch_loop() -> None:
    """VEILLE DES SESSIONS — le cœur du rythme quotidien demandé par la stratégie.

    Surveille les fenêtres à forte valeur et recalcule les trades du jour dès qu'on y entre :
    - ouverture de Londres (07:00–10:00 UTC),
    - ouverture de New York (12:30–15:30 UTC),
    - **chevauchement Londres / New York (12:00–16:00 UTC)** — la fenêtre la plus liquide, celle
      que la stratégie demande de surveiller en priorité.

    Une fenêtre ne déclenche qu'UNE alerte par jour (pas de spam), et le recalcul se poursuit à
    l'intervalle configuré tant que la fenêtre est ouverte (le cache multi-UT absorbe le coût).
    """
    from app.data import sessions as sessions_mod
    from app.repositories.store import get_store

    fired: dict[str, str] = {}  # kill_zone -> date déjà notifiée
    while True:
        s = get_settings()
        await asyncio.sleep(max(120, s.session_watch_interval))
        if not s.session_watch_enabled:
            continue
        try:
            ctx = sessions_mod.session_context()
            today = datetime.now(UTC).date().isoformat()
            fresh = [z for z in ctx["kill_zones"] if fired.get(z) != today]
            if not fresh:
                continue
            store = get_store()
            # On passe par l'INSTANTANÉ plutôt que de recalculer dans notre coin : sinon la veille
            # de session et les pages afficheraient deux sélections différentes au même moment.
            from app.services import live_snapshot

            payload = await live_snapshot.refresh(store)
            payload["trigger_window"] = fresh
            zone_labels = {
                "london_open": "🇬🇧 Ouverture de Londres",
                "newyork_open": "🇺🇸 Ouverture de New York",
                "overlap": "🔥 Chevauchement Londres / New York",
            }
            title = " + ".join(zone_labels.get(z, z) for z in fresh)
            body = format_top_trades(payload)
            # Exécution automatique en compte DÉMO des setups prêts (avec leur SL/TP).
            exec_report = await _auto_execute_paper(store, payload)
            if exec_report:
                body += "\n\n" + exec_report
            await _broadcast(store, f"{title}\n\n" + body, f"{title} — trades du jour")
            for z in fresh:
                fired[z] = today
            logger.info("Veille session : %s -> %d setup(s)", title, len(payload.get("picks") or []))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de la veille des sessions (%s)", exc)


async def learning_loop() -> None:
    """Apprentissage continu : résout les signaux ouverts (win/loss) pour TOUS les tenants.

    Plus il y a de trades résolus, plus les multiplicateurs de fiabilité par agent s'affinent et
    plus les signaux émis deviennent fiables (le Master pondère selon ce qui a marché)."""
    from app.repositories.store import get_store
    from app.services import journal_service

    while True:
        await asyncio.sleep(max(60, get_settings().learning_interval))
        store = get_store()
        try:
            tenants = {u.tenant_id for u in store.users.list_all()}
        except Exception:  # noqa: BLE001
            tenants = set()
        total = 0
        for tid in tenants:
            try:
                total += await journal_service.auto_resolve(store, tid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Apprentissage tenant %s échoué (%s)", tid, exc)
        if total:
            logger.info("Apprentissage : %d signal(aux) résolu(s) -> pondérations affinées", total)


async def strategy_alerts_loop() -> None:
    """Surveille les stratégies actives et envoie une alerte à chaque nouveau signal directionnel."""
    from app.repositories.store import get_store
    from app.services import strategy_alert_service

    while True:
        await asyncio.sleep(max(120, get_settings().strategy_alerts_interval))
        try:
            sent = await strategy_alert_service.check_strategy_alerts(get_store())
            if sent:
                logger.info("Alertes stratégie : %d notification(s) envoyée(s)", sent)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec des alertes stratégie (%s)", exc)


async def edge_sweep_loop() -> None:
    """Sweep NOCTURNE de la carte de l'edge : walk-forward de toutes les stratégies × symboles × TF.

    Phase B du plan maître : savoir en continu OÙ il y a un edge exploitable (et où il n'y en a
    pas). Premier passage ~10 min après le boot, puis toutes les `edge_sweep_interval_hours` h."""
    from app.repositories.store import get_store
    from app.services import edge_map_service

    await asyncio.sleep(600)  # laisser le démarrage se stabiliser
    while True:
        s = get_settings()
        if s.edge_sweep_enabled:
            try:
                payload = await edge_map_service.run_edge_sweep(get_store())
                logger.info("Carte de l'edge mise à jour : %s", payload.get("note"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Échec du sweep de la carte de l'edge (%s)", exc)
        await asyncio.sleep(max(3600, s.edge_sweep_interval_hours * 3600))


async def snapshot_loop() -> None:
    """INSTANTANÉ TEMPS RÉEL — recalcule la stratégie en fond pour que les pages soient instantanées.

    C'est cette boucle qui absorbe le coût (40 symboles × 5 unités de temps). Les pages ne font plus
    que lire le résultat : elles peuvent donc se rafraîchir toutes les 10 secondes sans jamais
    attendre. Sans elle, chaque ouverture de page relancerait le calcul complet.
    """
    from app.repositories.store import get_store
    from app.services import live_snapshot

    await asyncio.sleep(5)  # laisser l'API finir de démarrer
    while True:
        s = get_settings()
        if not s.playbook_snapshot_enabled:
            await asyncio.sleep(60)
            continue
        started = datetime.now(UTC)
        try:
            payload = await live_snapshot.refresh(get_store())
            logger.info(
                "Instantané stratégie : %d setup(s) dont %d exécutable(s) en %.1f s",
                len(payload.get("picks") or []), payload.get("ready", 0),
                (datetime.now(UTC) - started).total_seconds(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec du rafraîchissement de l'instantané (%s)", exc)
        # Repos GARANTI entre deux cycles. Si un cycle dure plus longtemps que l'intervalle (réseau
        # lent, fournisseur qui limite le débit), repartir aussitôt saturerait la machine en continu
        # — c'est exactement ce qui rendait l'API inutilisable. On laisse toujours respirer.
        elapsed = (datetime.now(UTC) - started).total_seconds()
        rest = max(30.0, s.playbook_snapshot_interval - elapsed)
        if elapsed > s.playbook_snapshot_interval:
            logger.warning(
                "Cycle d'instantané plus long que son intervalle (%.0f s > %d s) — "
                "réduis l'univers ou allonge `playbook_snapshot_interval`",
                elapsed, s.playbook_snapshot_interval,
            )
        await asyncio.sleep(rest)


async def daily_picks_loop() -> None:
    """SCANNER COMPLÉMENTAIRE — précalcule la sélection par marché, pour les 6 unités de temps.

    Même principe que `snapshot_loop`, appliqué au bas de la page « Trades du jour » : c'est cette
    boucle qui absorbe le coût (4 classes d'actifs × jusqu'à 8 backtests complets, par unité de
    temps) pour que la route se contente de LIRE. Avant, chaque première visite d'une unité de temps
    déclenchait ce calcul dans la requête HTTP et faisait attendre l'utilisateur.

    Les unités de temps sont traitées L'UNE APRÈS L'AUTRE, volontairement : elles se disputeraient
    sinon les mêmes fournisseurs de données, et la saturation coûte plus cher que la file d'attente
    (mesuré, cf. `data.markets._cascade`).
    """
    from app.repositories.store import get_store
    from app.services import daily_picks_cache

    await asyncio.sleep(45)  # après le premier instantané de la stratégie, qui est prioritaire
    while True:
        s = get_settings()
        if not s.daily_picks_precompute_enabled:
            await asyncio.sleep(120)
            continue
        started = datetime.now(UTC)
        store = get_store()
        for tf in daily_picks_cache.timeframes():
            try:
                payload = await daily_picks_cache.refresh(store, tf)
                logger.info("Sélection du jour (%s) : %d trade(s) retenu(s)",
                            tf, len(payload.get("picks") or []))
            except Exception as exc:  # noqa: BLE001 — une unité de temps KO n'arrête pas les autres
                logger.warning("Sélection du jour (%s) échouée (%s)", tf, exc)
        # Repos GARANTI entre deux cycles, comme pour l'instantané : si un cycle dure plus longtemps
        # que son intervalle, repartir aussitôt maintiendrait la machine à 100 % en continu.
        elapsed = (datetime.now(UTC) - started).total_seconds()
        await asyncio.sleep(max(60.0, s.daily_picks_precompute_interval - elapsed))


async def auto_entry_loop() -> None:
    """AUTO-ENTRÉE — surveille les setups ARMÉS et ouvre en démo dès le déclencheur 15 min.

    C'est ce qui remplace le clic : le contexte est déjà validé, il ne manque que le timing, et le
    timing arrive sans prévenir. Compte DÉMO uniquement (cf. `auto_entry_service`).
    """
    from app.repositories.store import get_store
    from app.services import auto_entry_service

    await asyncio.sleep(20)  # laisser un premier instantané se former
    while True:
        s = get_settings()
        await asyncio.sleep(max(15, s.playbook_auto_entry_interval))
        if not auto_entry_service.enabled():
            continue
        try:
            report = await auto_entry_service.run_auto_entry(get_store())
            if report.get("opened"):
                logger.info("Auto-entrée : %s", report["note"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de la veille d'auto-entrée (%s)", exc)


async def training_loop() -> None:
    """ENTRAÎNEMENT QUOTIDIEN des agents sur la stratégie (walk-forward + fiches d'expertise).

    Premier passage peu après le démarrage (pour que le classement dispose tout de suite de
    statistiques mesurées), puis chaque jour à `playbook_training_hour` UTC.
    """
    from app.repositories.store import get_store
    from app.services import training_service

    await asyncio.sleep(120)  # ne pas concurrencer le démarrage
    store = get_store()
    training_service.load_from_store(store)
    if not training_service.is_trained():
        try:
            await training_service.run_training(store)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de l'entraînement initial (%s)", exc)

    while True:
        s = get_settings()
        now = datetime.now(UTC)
        target = now.replace(hour=s.playbook_training_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        if not get_settings().playbook_training_enabled:
            continue
        try:
            await training_service.run_training(get_store())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de l'entraînement quotidien (%s)", exc)


async def market_opinion_loop() -> None:
    """ANALYSE QUOTIDIENNE des marchés (forex + or), HORS stratégie du desk.

    Deux déclenchements, et le premier compte autant que le second :

    1. **Rattrapage au démarrage** — si l'analyse du jour manque, elle est produite peu après le
       lancement. C'est ce qui tient la promesse « chaque jour je lance le projet, je la trouve
       prête » : sans ce rattrapage, un projet démarré à 9 h attendrait le lendemain 6 h UTC.
    2. **Passage quotidien** à `market_opinion_hour` UTC (06 h par défaut, soit 07 h au Maroc :
       l'analyse est en place avant l'ouverture de Londres).

    Elle ne concurrence pas la stratégie — elle répond à une autre question (cf.
    `services.market_opinion_service`).
    """
    from app.repositories.store import get_store
    from app.services import market_opinion_service

    # Décalé de l'entraînement et du premier instantané : ces trois passages tapent les mêmes
    # fournisseurs gratuits, et les lancer ensemble les fait tous ralentir.
    await asyncio.sleep(180)
    s = get_settings()
    if s.market_opinion_enabled and s.market_opinion_on_startup:
        store = get_store()
        try:
            if not market_opinion_service.is_fresh(market_opinion_service.latest(store)):
                await market_opinion_service.run_daily_opinion(store)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de l'analyse quotidienne au démarrage (%s)", exc)

    while True:
        s = get_settings()
        now = datetime.now(UTC)
        target = now.replace(hour=s.market_opinion_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        if not get_settings().market_opinion_enabled:
            continue
        try:
            await market_opinion_service.run_daily_opinion(get_store())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de l'analyse quotidienne (%s)", exc)


async def backtest_loop() -> None:
    """BACKTEST HEBDOMADAIRE de la stratégie sur toutes les paires du desk.

    Beaucoup plus lourd que l'entraînement quotidien (toute la profondeur d'historique × toutes les
    paires) : il tourne une fois par semaine, le week-end, quand les marchés sont fermés et que la
    boucle d'instantané n'a rien d'urgent à calculer. C'est lui qui produit le classement des paires
    par fiabilité, réutilisé ensuite par le classement des trades du jour.
    """
    from app.repositories.store import get_store
    from app.services import training_service

    await asyncio.sleep(300)  # laisser le démarrage se stabiliser
    while True:
        s = get_settings()
        now = datetime.now(UTC)
        target = now.replace(hour=s.playbook_backtest_hour, minute=0, second=0, microsecond=0)
        days_ahead = (s.playbook_backtest_weekday - now.weekday()) % 7
        target += timedelta(days=days_ahead)
        if target <= now:
            target += timedelta(days=7)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        if not get_settings().playbook_backtest_enabled:
            continue
        try:
            payload = await training_service.run_backtest_training(get_store())
            logger.info("Backtest hebdomadaire : %s", payload["conclusion"]["headline"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec du backtest hebdomadaire (%s)", exc)


async def deep_backtest_loop() -> None:
    """PASSE LONGUE (5 ans) rejouée en milieu de semaine, indépendamment du backtest complet.

    Le backtest hebdomadaire du dimanche enchaîne trois passes et dure plusieurs heures. La passe
    longue, elle, est légère (journalier au lieu de 1 h : environ vingt fois moins de bougies à
    évaluer) et c'est elle qui porte la réponse à « où cette stratégie est-elle fiable sur la
    durée ». On la rejoue donc seule au milieu de la semaine : le classement sur cinq ans est
    rafraîchi deux fois par semaine sans payer deux fois le prix du passage complet.

    Elle ne touche PAS aux verdicts par paire (🟢/🟡/🔴) : ceux-ci sont établis par la passe portée,
    qui mesure le réglage de production. Mélanger deux échelles de temps dans un même verdict
    donnerait une note que rien ne trade.
    """
    from app.backtest import playbook_backtest as pbt
    from app.repositories.store import get_store

    await asyncio.sleep(900)  # après l'entraînement initial et le premier sweep
    while True:
        s = get_settings()
        now = datetime.now(UTC)
        target = now.replace(hour=s.playbook_backtest_hour, minute=30, second=0, microsecond=0)
        target += timedelta(days=(s.playbook_backtest_deep_weekday - now.weekday()) % 7)
        if target <= now:
            target += timedelta(days=7)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        s = get_settings()
        if not (s.playbook_backtest_enabled and s.playbook_backtest_deep_enabled):
            continue
        if pbt.run_state()["running"]:
            logger.info("Passe longue reportée : un backtest est déjà en cours")
            continue
        try:
            store = get_store()
            payload = await pbt.run_pass(
                pbt.full_universe(), entry_tf="1d", step=s.playbook_backtest_deep_step,
                parallel=s.playbook_max_parallel, ladder="swing",
                max_years=s.playbook_backtest_deep_years,
            )
            # Stocké à part : ce n'est pas le réglage de production, et confondre les deux dans le
            # même enregistrement ferait lire des chiffres de swing long comme des chiffres d'intraday.
            record = {"date": datetime.now(UTC).date().isoformat(),
                      **{k: v for k, v in payload.items() if k != "trade_log"}}
            store.records.put("playbook_backtest_long", record["date"], record)
            store.records.put("playbook_backtest_long", pbt.LATEST, record)
            o = payload["overall"]
            logger.info(
                "Passe longue (%s ans) : %d trades, %s %% de réussite, espérance %+.2f R, PF %s",
                payload["years_covered"], o["trades"], o["win_rate"], o["expectancy_r"],
                o["profit_factor"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de la passe longue (%s)", exc)


async def positions_loop() -> None:
    """Surveillance continue des positions papier : clôture auto dès qu'un SL/TP est atteint."""
    from app.repositories.store import get_store
    from app.services import execution_service

    # La quarantaine est le SEUL passage qui doive regarder les positions déjà CLÔTURÉES (elle
    # cherche des résultats que le marché n'a pas produits) : elle ne peut donc pas bénéficier du
    # filtre « positions ouvertes » qui allège les trois autres. Or son coût croît avec tout
    # l'historique de la plateforme. Comme c'est un filet de réparation pour un bug DÉJÀ corrigé, et
    # non un besoin temps réel, elle tourne toutes les 10 minutes au lieu de toutes les minutes —
    # une position corrompue reste détectée, simplement pas dans la même seconde.
    quarantine_every = 10
    pass_count = 0
    while True:
        interval = max(15, get_settings().position_monitor_interval)
        await asyncio.sleep(interval)
        pass_count += 1
        try:
            store = get_store()
            # 0) Quarantaine : neutralise les clôtures impossibles (résultat que le marché n'a
            #    jamais produit). Sans ça, un P&L inventé contaminerait le portefeuille et
            #    l'apprentissage des agents.
            if pass_count % quarantine_every == 1:
                report = execution_service.quarantine_impossible_closures(store)
                if report["count"]:
                    logger.warning("Quarantaine : %d position(s) invalidée(s)", report["count"])
            # 1) Sécuriser AVANT de vérifier les clôtures : un trade qui a atteint +2R doit voir son
            #    stop remonté au même passage, sinon un repli dans la même minute le rendrait perdant.
            secured = await execution_service.secure_open_profits(store)
            if secured:
                logger.info("Profit sécurisé : %d position(s) — stop remonté sur +2R", secured)
            # 1 bis) TP1 atteint : poursuivre vers TP2 en verrouillant 80 % du chemin, ou prendre
            #    le gain si le momentum ne confirme plus. Même raison d'être AVANT les clôtures.
            progressed = await execution_service.manage_tp_progression(store)
            if progressed:
                logger.info("TP1 atteint : %d position(s) arbitrée(s) entre TP2 et prise de gain",
                            progressed)
            closed = await execution_service.monitor_positions(store)
            if closed:
                logger.info("Moniteur positions : %d position(s) clôturée(s) automatiquement", closed)
            # 2) Gel des entrées (plan, Phase 3.3) : si les clôtures du passage viennent de faire
            #    franchir −3 % (jour) ou −6 % (semaine), on l'annonce UNE fois — le gel lui-même
            #    est appliqué au moment d'ouvrir (execute_playbook_trades).
            from app.services import risk_service

            frozen = await risk_service.notify_freezes(store)
            if frozen:
                logger.warning("Gel des entrées annoncé à %d tenant(s)", frozen)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec du moniteur de positions (%s)", exc)
