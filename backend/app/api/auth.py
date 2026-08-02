"""Routes d'authentification : inscription, connexion, profil, MFA (TOTP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core import totp
from app.core.config import get_settings
from app.core.deps import current_user, store_dep
from app.core.security import create_access_token, hash_password, verify_password
from app.models.entities import User
from app.models.schemas import (
    LoginRequest,
    MfaEnableRequest,
    MfaSetupResponse,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.repositories.store import AppStore
from app.services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _to_response(user: User, store: AppStore) -> UserResponse:
    tenant = store.tenants.get(user.tenant_id)
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        risk_profile=user.risk_profile,
        capital=user.capital,
        watchlist=user.watchlist,
        onboarded=user.onboarded,
        plan=tenant.plan if tenant else "free",
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, request: Request, store: AppStore = Depends(store_dep)
) -> TokenResponse:
    if not get_settings().allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Inscription fermée")
    if store.users.get_by_email(body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email déjà enregistré")
    tenant = store.tenants.create(name=body.email)
    user = store.users.create(
        tenant_id=tenant.id,
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
    )
    audit.record("user.register", actor=user.email, tenant_id=user.tenant_id, ip=_client_ip(request))
    token = create_access_token(user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, request: Request, store: AppStore = Depends(store_dep)
) -> TokenResponse:
    ip = _client_ip(request)
    user = store.users.get_by_email(body.email)
    if user is None or not verify_password(body.password, user.password_hash):
        audit.record("auth.login_failed", actor=body.email, ip=ip, detail="bad credentials")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiants invalides")

    # MFA exigée si activée.
    if user.mfa_enabled and user.mfa_secret:
        if not body.mfa_code:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code MFA requis")
        if not totp.verify(user.mfa_secret, body.mfa_code):
            audit.record("auth.mfa_failed", actor=user.email, tenant_id=user.tenant_id, ip=ip)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code MFA invalide")

    audit.record("auth.login", actor=user.email, tenant_id=user.tenant_id, ip=ip)
    token = create_access_token(user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user), store: AppStore = Depends(store_dep)) -> UserResponse:
    return _to_response(user, store)


# ---------------- MFA (TOTP) ----------------
@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    user: User = Depends(current_user), store: AppStore = Depends(store_dep)
) -> MfaSetupResponse:
    """Génère un secret TOTP (non encore activé) et l'URI otpauth pour le QR code."""
    secret = totp.generate_secret()
    user.mfa_secret = secret
    store.users.update(user)
    return MfaSetupResponse(secret=secret, otpauth_uri=totp.provisioning_uri(secret, user.email))


@router.post("/mfa/enable", response_model=UserResponse)
async def mfa_enable(
    body: MfaEnableRequest,
    request: Request,
    user: User = Depends(current_user),
    store: AppStore = Depends(store_dep),
) -> UserResponse:
    """Active la MFA après vérification d'un premier code."""
    if not user.mfa_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Appelez /mfa/setup d'abord")
    if not totp.verify(user.mfa_secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code invalide")
    user.mfa_enabled = True
    store.users.update(user)
    audit.record("auth.mfa_enabled", actor=user.email, tenant_id=user.tenant_id, ip=_client_ip(request))
    return _to_response(user, store)


@router.post("/mfa/disable", response_model=UserResponse)
async def mfa_disable(
    user: User = Depends(current_user), store: AppStore = Depends(store_dep)
) -> UserResponse:
    user.mfa_enabled = False
    user.mfa_secret = None
    store.users.update(user)
    return _to_response(user, store)
