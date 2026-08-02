"""Notificateurs multicanal : email (Resend), Telegram, webhook, SMS (Twilio), push.

Si les clés ne sont pas configurées, les envois sont journalisés (no-op gracieux) afin que le
pipeline complet fonctionne en dev/test sans dépendances externes.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.models.signal import SignalCard

logger = logging.getLogger(__name__)


def format_signal(card: SignalCard) -> str:
    tps = " / ".join(
        str(t) for t in [card.take_profit_1, card.take_profit_2, card.take_profit_3] if t is not None
    )
    # « — » plutôt que « None » : une décision « pas de trade » n'a pas de niveaux (cf. SignalCard).
    # Une alerte qui annonce « Entrée: None » est au mieux illisible, au pire prise pour un bug.
    def _lvl(value: float | None) -> str:
        return "—" if value is None else str(value)

    return (
        f"[{card.direction.value}] {card.asset}\n"
        f"Entrée: {_lvl(card.entry)} | SL: {_lvl(card.stop_loss)} | TP: {tps or '—'}\n"
        f"R/R: {_lvl(card.risk_reward)} | Confiance: {card.confidence}% | {card.timeframe.value}\n"
        f"{card.rationale}"
    )


async def send_telegram(chat_id: str, text: str) -> bool:
    s = get_settings()
    if not s.telegram_bot_token:
        logger.info("[telegram:noop] %s", text.replace("\n", " | "))
        return False
    try:
        import httpx

        url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Envoi Telegram échoué (%s)", exc)
        return False


def _send_email_gmail_sync(to: str, subject: str, text: str) -> bool:
    """SMTP Gmail (bloquant — appelé via `asyncio.to_thread`). Pas de domaine à posséder ni à
    vérifier : l'email part directement du compte Gmail configuré, authentifié par un mot de
    passe d'application (jamais le mot de passe réel du compte)."""
    s = get_settings()
    try:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = s.gmail_address
        msg["To"] = to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(s.gmail_address, s.gmail_app_password)
            server.sendmail(s.gmail_address, [to], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Envoi email Gmail échoué (%s)", exc)
        return False


async def send_email(to: str, subject: str, text: str) -> bool:
    s = get_settings()
    # Gmail (SMTP direct) est prioritaire : aucun domaine à posséder, contrairement à Resend.
    if s.gmail_address and s.gmail_app_password:
        return await asyncio.to_thread(_send_email_gmail_sync, to, subject, text)
    if not s.resend_api_key:
        logger.info("[email:noop] to=%s subject=%s", to, subject)
        return False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json={"from": s.email_from, "to": [to], "subject": subject, "text": text},
            )
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Envoi email échoué (%s)", exc)
        return False


async def send_webhook(url: str, card: SignalCard) -> bool:
    """POST le signal (JSON) vers un webhook utilisateur."""
    if not url:
        return False
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=card.model_dump(mode="json"))
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Webhook échoué (%s)", exc)
        return False


async def send_sms(to: str, text: str) -> bool:
    """SMS via Twilio (no-op si non configuré)."""
    s = get_settings()
    sid = getattr(s, "twilio_account_sid", "") or ""
    token = getattr(s, "twilio_auth_token", "") or ""
    from_ = getattr(s, "twilio_from", "") or ""
    if not (sid and token and from_):
        logger.info("[sms:noop] to=%s", to)
        return False
    try:
        import httpx

        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, auth=(sid, token), data={"To": to, "From": from_, "Body": text[:1500]}
            )
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Envoi SMS échoué (%s)", exc)
        return False


async def send_push(token: str, text: str) -> bool:
    """Notification push (stub : à brancher sur FCM/APNs en Phase 3 mobile)."""
    if not token:
        return False
    logger.info("[push:noop] token=%s %s", token[:8], text.replace("\n", " | "))
    return False


async def notify_signal(
    card: SignalCard,
    *,
    email: str | None = None,
    telegram_chat_id: str | None = None,
    webhook_url: str | None = None,
    sms_to: str | None = None,
    push_token: str | None = None,
) -> None:
    """Diffuse un signal sur tous les canaux activés (chacun dégrade gracieusement)."""
    text = format_signal(card)
    subject = f"Nouveau signal {card.asset} — {card.direction.value}"
    if email:
        await send_email(email, subject, text)
    if telegram_chat_id:
        await send_telegram(telegram_chat_id, text)
    if webhook_url:
        await send_webhook(webhook_url, card)
    if sms_to:
        await send_sms(sms_to, text)
    if push_token:
        await send_push(push_token, text)
