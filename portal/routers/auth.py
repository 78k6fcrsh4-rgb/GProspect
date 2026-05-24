"""
portal/routers/auth.py
----------------------
Authentication endpoints for the grant prospecting portal.

Endpoints:
    POST /auth/login          — email + password → JWT token
    POST /auth/logout         — invalidate session (client-side)
    GET  /auth/me             — get current user info
    POST /auth/register       — create new user account (admin only)
    POST /auth/change-password — change own password
    POST /auth/reset-password — request password reset email

The login endpoint uses OAuth2 password flow which is
compatible with FastAPI's built-in security documentation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database.db import get_db
from portal.auth.security import (
    hash_password,
    verify_password,
    create_access_token,
)
from portal.auth.dependencies import (
    get_current_user,
    get_current_admin,
)
from portal.limiter import limiter
from portal.models.user import User, UserRole

router = APIRouter(prefix="/auth", tags=["Authentication"])


# Pre-computed bcrypt hash for a random string nobody knows. Used as a decoy
# target for verify_password() when login is called with an email that doesn't
# exist, so wrong-email and wrong-password take the same wall-clock time.
# Closes the timing oracle from the code review.
_DUMMY_BCRYPT_HASH = (
    "$2b$12$wOJxQyx1lG4rqWqcQwL.QeQX0Iu7XJrJ6NMpY6BqGJ.t8sxX6VqUq"
)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# These define what data the endpoints accept and return.
# Pydantic validates the data automatically.
# ─────────────────────────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    """Response returned on successful login."""
    access_token: str
    token_type:   str = "bearer"
    user_email:   str
    user_role:    str
    org_name:     str
    full_name:    str


class UserResponse(BaseModel):
    """Safe user info response — never includes password."""
    id:          int
    email:       str
    full_name:   str
    org_name:    str
    role:        str
    is_active:   bool
    created_at:  Optional[str]
    last_login:  Optional[str]


class CreateUserRequest(BaseModel):
    """Request body for creating a new user (admin only)."""
    email:     str
    full_name: str
    password:  str
    role:      str = "user"


class ChangePasswordRequest(BaseModel):
    """Request body for changing password."""
    current_password: str
    new_password:     str


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(
    request:   Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db:        Session                   = Depends(get_db),
):
    """
    Login endpoint — validates credentials and returns JWT token.

    Accepts email and password via OAuth2 password form. Returns a JWT
    token on success that the client stores and uses for all subsequent
    authenticated requests.

    Rate limit: 5 attempts per minute per source IP. Exceeding the limit
    returns HTTP 429 — defense against credential-stuffing and password
    spraying.

    Args:
        request:   FastAPI request (used by slowapi for IP extraction).
        form_data: OAuth2 form with username (email) and password.
        db:        Database session.

    Returns:
        TokenResponse with JWT token and user info.

    Raises:
        HTTPException 401: Invalid email or password.
        HTTPException 403: Account is deactivated.
        HTTPException 429: Rate limit exceeded.
    """
    # Look up user by email
    user = db.query(User).filter(
        User.email == form_data.username.lower().strip()
    ).first()

    # Verify user exists and password is correct.
    # NOTE: we run verify_password against a dummy hash even when the user
    # doesn't exist, so wrong-email and wrong-password responses take the
    # same time — closes the trivial timing oracle from the code review.
    if not user:
        verify_password(form_data.password, _DUMMY_BCRYPT_HASH)
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Incorrect email or password.",
            headers     = {"WWW-Authenticate": "Bearer"},
        )
    if not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Incorrect email or password.",
            headers     = {"WWW-Authenticate": "Bearer"},
        )

    # Check account is active
    if not user.is_active:
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "This account has been deactivated. Contact your administrator.",
        )

    # Update last login timestamp
    user.last_login = datetime.now(timezone.utc)
    db.commit()

    # Create JWT token. Includes the user's current token_version so /auth/logout
    # can invalidate all outstanding tokens by incrementing token_version.
    token = create_access_token({
        "sub":      user.email,
        "role":     user.role.value,
        "org_name": user.org_name,
        "tv":       user.token_version,
    })

    return TokenResponse(
        access_token = token,
        token_type   = "bearer",
        user_email   = user.email,
        user_role    = user.role.value,
        org_name     = user.org_name,
        full_name    = user.full_name,
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """
    Returns the current authenticated user's info.

    Used by the portal frontend to display the logged-in
    user's name, role, and organization.

    Args:
        current_user: Authenticated user (auto-injected).

    Returns:
        UserResponse with safe user info (no password).
    """
    return UserResponse(
        id         = current_user.id,
        email      = current_user.email,
        full_name  = current_user.full_name,
        org_name   = current_user.org_name,
        role       = current_user.role.value,
        is_active  = current_user.is_active,
        created_at = current_user.created_at.isoformat() if current_user.created_at else None,
        last_login = current_user.last_login.isoformat() if current_user.last_login else None,
    )


@router.post("/register", response_model=UserResponse)
def register_user(
    request:      CreateUserRequest,
    current_admin: User    = Depends(get_current_admin),
    db:            Session = Depends(get_db),
):
    """
    Creates a new user account (Admin only).

    Only admins can create new users. New users are created
    within the same organization as the admin creating them.

    Args:
        request:       New user details.
        current_admin: Must be an Admin (auto-injected).
        db:            Database session.

    Returns:
        UserResponse for the newly created user.

    Raises:
        HTTPException 400: Email already registered.
        HTTPException 403: Not an admin.
    """
    # Check email is not already registered
    existing = db.query(User).filter(
        User.email == request.email.lower().strip()
    ).first()

    if existing:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Email '{request.email}' is already registered.",
        )

    # Validate role
    try:
        role = UserRole(request.role.lower())
    except ValueError:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Invalid role '{request.role}'. Must be 'admin' or 'user'.",
        )

    # Create the new user
    new_user = User(
        email           = request.email.lower().strip(),
        full_name       = request.full_name,
        org_name        = current_admin.org_name,
        hashed_password = hash_password(request.password),
        role            = role,
        is_active       = True,
        is_verified     = True,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    print(f"[Auth] New user created: {new_user.email} ({role.value}) for {new_user.org_name}")

    return UserResponse(
        id         = new_user.id,
        email      = new_user.email,
        full_name  = new_user.full_name,
        org_name   = new_user.org_name,
        role       = new_user.role.value,
        is_active  = new_user.is_active,
        created_at = new_user.created_at.isoformat() if new_user.created_at else None,
        last_login = None,
    )


@router.post("/change-password")
@limiter.limit("10/hour")
def change_password(
    request:      Request,
    payload:      ChangePasswordRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Changes the current user's password.

    Requires the current password to be correct before allowing the change.
    Rate-limited to 10 attempts per hour per source IP — prevents using a
    valid session to brute-force the existing password.

    Args:
        request:      FastAPI request (slowapi key extraction).
        payload:      Current and new password.
        current_user: Authenticated user (auto-injected).
        db:           Database session.

    Returns:
        Success message.

    Raises:
        HTTPException 400: Current password is incorrect.
        HTTPException 429: Rate limit exceeded.
    """
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "Current password is incorrect.",
        )

    if len(payload.new_password) < 8:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "New password must be at least 8 characters.",
        )

    current_user.hashed_password = hash_password(payload.new_password)
    current_user.updated_at      = datetime.now(timezone.utc)
    db.commit()

    return {"message": "Password changed successfully."}


@router.post("/logout")
def logout(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Logout endpoint — actually invalidates outstanding tokens.

    Increments the user's `token_version` column, which is encoded as the
    `tv` claim in every issued JWT. Because get_current_user re-checks the
    claim against the column on every request, every outstanding token for
    this user becomes invalid as soon as this commit lands.

    Note: this invalidates ALL of the user's sessions, not just the current
    browser tab. A future "logout this device only" feature would require
    per-token jti tracking — not yet implemented.

    Args:
        current_user: Authenticated user (auto-injected).
        db:           Database session.

    Returns:
        Confirmation message.
    """
    current_user.token_version = (current_user.token_version or 0) + 1
    current_user.updated_at    = datetime.now(timezone.utc)
    db.commit()
    return {
        "message": (
            f"Successfully logged out. All sessions invalidated. "
            f"Goodbye {current_user.full_name}."
        ),
    }