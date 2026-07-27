"""Schémas Pydantic pour les requêtes/réponses de l'API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.signal import Timeframe

# Validation email simple sans dépendance email-validator (suffisant pour le MVP).
_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    email: str = Field(pattern=_EMAIL_RE)
    password: str = Field(min_length=8)
    full_name: str | None = None


class LoginRequest(BaseModel):
    email: str = Field(pattern=_EMAIL_RE)
    password: str
    mfa_code: str | None = None


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaEnableRequest(BaseModel):
    code: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str | None
    risk_profile: str
    capital: float
    watchlist: list[str]
    onboarded: bool
    plan: str


class OnboardingRequest(BaseModel):
    risk_profile: str = Field(pattern="^(conservative|moderate|aggressive)$")
    capital: float = Field(gt=0)
    watchlist: list[str] = Field(default_factory=lambda: ["BTC/USDT"], max_length=20)


class GenerateSignalRequest(BaseModel):
    asset: str = "BTC/USDT"
    timeframe: Timeframe = Timeframe.H1
    notify: bool = False


class SettingsRequest(BaseModel):
    """Mise à jour partielle des paramètres utilisateur (tous champs optionnels)."""

    watchlist: list[str] | None = Field(default=None, max_length=20)
    max_exposure_pct: float | None = Field(default=None, gt=0, le=100)
    max_daily_signals: int | None = Field(default=None, ge=1, le=1000)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=100)
    alert_email: bool | None = None
    alert_telegram: bool | None = None
    telegram_chat_id: str | None = None
    alert_webhook: bool | None = None
    webhook_url: str | None = None
    alert_sms: bool | None = None
    phone: str | None = None
    push_token: str | None = None
    locale: str | None = None
    daily_digest: bool | None = None


class SettingsResponse(BaseModel):
    watchlist: list[str]
    max_exposure_pct: float
    max_daily_signals: int
    daily_loss_limit_pct: float
    alert_email: bool
    alert_telegram: bool
    telegram_chat_id: str | None
    alert_webhook: bool
    webhook_url: str | None
    alert_sms: bool
    phone: str | None
    push_enabled: bool
    locale: str
    daily_digest: bool
    mfa_enabled: bool
