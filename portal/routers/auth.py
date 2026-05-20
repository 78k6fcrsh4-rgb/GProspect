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

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
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
from portal.models.user import User, UserRole

router = APIRouter(prefix="/auth", tags=["Authentication"])


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
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db:        Session                   = Depends(get_db),
):
    """
    Login endpoint — validates credentials and returns JWT token.

    Accepts email and password via OAuth2 password form.
    Returns a JWT token on success that the client stores
    and uses for all subsequent authenticated requests.

    Args:
        form_data: OAuth2 form with username (email) and password.
        db:        Database session.

    Returns:
        TokenResponse with JWT token and user info.

    Raises:
        HTTPException 401: Invalid email or password.
        HTTPException 403: Account is deactivated.
    """
    # Look up user by email
    user = db.query(User).filter(
        User.email == form_data.username.lower().strip()
    ).first()

    # Verify user exists and password is correct
    if not user or not verify_password(form_data.password, user.hashed_password):
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
    user.last_login = datetime.utcnow()
    db.commit()

    # Create JWT token
    token = create_access_token({
        "sub":      user.email,
        "role":     user.role.value,
        "org_name": user.org_name,
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
def change_password(
    request:      ChangePasswordRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Changes the current user's password.

    Requires the current password to be correct before
    allowing the change.

    Args:
        request:      Current and new password.
        current_user: Authenticated user (auto-injected).
        db:           Database session.

    Returns:
        Success message.

    Raises:
        HTTPException 400: Current password is incorrect.
    """
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "Current password is incorrect.",
        )

    if len(request.new_password) < 8:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "New password must be at least 8 characters.",
        )

    current_user.hashed_password = hash_password(request.new_password)
    current_user.updated_at      = datetime.utcnow()
    db.commit()

    return {"message": "Password changed successfully."}


@router.post("/logout")
def logout(current_user: User = Depends(get_current_user)):
    """
    Logout endpoint.

    JWT tokens are stateless — the server cannot invalidate them.
    The client is responsible for deleting the stored token.
    This endpoint confirms the logout and the client clears
    its stored token.

    Args:
        current_user: Authenticated user (auto-injected).

    Returns:
        Success message.
    """
    return {
        "message": f"Successfully logged out. Goodbye {current_user.full_name}."
    }