"""
portal/auth/dependencies.py
---------------------------
FastAPI dependency functions for authentication and
role-based access control.

Three dependencies used throughout the portal:

    get_current_user  — any authenticated user
    get_current_admin — authenticated admin only
    get_same_org_user — user can only access their org's data

Usage in routes:
    from portal.auth.dependencies import get_current_user, get_current_admin
    from fastapi import Depends

    @router.get("/results")
    def get_results(current_user = Depends(get_current_user)):
        # Any logged-in user can access this
        pass

    @router.post("/admin/settings")
    def update_settings(current_user = Depends(get_current_admin)):
        # Only admins can access this
        pass
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from database.db import get_db
from portal.auth.security import decode_access_token
from portal.models.user import User, UserRole

# ─────────────────────────────────────────────────────────────────────────────
# OAuth2 scheme
# Tells FastAPI where to find the token in the request.
# Clients send: Authorization: Bearer <token>
# ─────────────────────────────────────────────────────────────────────────────

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ─────────────────────────────────────────────────────────────────────────────
# Standard HTTP exceptions
# Defined once here so every route uses consistent error messages.
# ─────────────────────────────────────────────────────────────────────────────

CREDENTIALS_EXCEPTION = HTTPException(
    status_code = status.HTTP_401_UNAUTHORIZED,
    detail      = "Could not validate credentials. Please log in again.",
    headers     = {"WWW-Authenticate": "Bearer"},
)

INACTIVE_EXCEPTION = HTTPException(
    status_code = status.HTTP_403_FORBIDDEN,
    detail      = "This account has been deactivated. Contact your administrator.",
)

ADMIN_REQUIRED_EXCEPTION = HTTPException(
    status_code = status.HTTP_403_FORBIDDEN,
    detail      = "Administrator access required for this action.",
)

ORG_MISMATCH_EXCEPTION = HTTPException(
    status_code = status.HTTP_403_FORBIDDEN,
    detail      = "You do not have permission to access this organization's data.",
)


# ─────────────────────────────────────────────────────────────────────────────
# Dependency functions
# ─────────────────────────────────────────────────────────────────────────────

def get_current_user(
    token: str     = Depends(oauth2_scheme),
    db:    Session = Depends(get_db)
) -> User:
    """
    FastAPI dependency — returns the current authenticated user.

    Validates the JWT token, looks up the user in the database,
    and returns the User object. Raises 401 if token is invalid
    or user is not found. Raises 403 if account is deactivated.

    Use this for any endpoint that requires a logged-in user
    regardless of their role.

    Args:
        token: JWT token from Authorization header (auto-injected).
        db:    Database session (auto-injected).

    Returns:
        The authenticated User database object.

    Raises:
        HTTPException 401: Token invalid or user not found.
        HTTPException 403: Account deactivated.
    """
    # Decode and validate the token
    payload = decode_access_token(token)
    if payload is None:
        raise CREDENTIALS_EXCEPTION

    # Extract email from token
    email: str = payload.get("sub")
    if not email:
        raise CREDENTIALS_EXCEPTION

    # Look up user in database
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise CREDENTIALS_EXCEPTION

    # Check account is active
    if not user.is_active:
        raise INACTIVE_EXCEPTION

    # Verify the token's version matches the user's current token_version.
    # /auth/logout increments token_version, so every outstanding JWT for that
    # user becomes invalid immediately. Tokens issued before this field existed
    # carry no `tv` claim — they are treated as version 0, matching the column
    # default, so existing sessions survive deployment of this change.
    token_tv: int = int(payload.get("tv", 0) or 0)
    if token_tv != int(user.token_version or 0):
        raise CREDENTIALS_EXCEPTION

    return user


def get_current_admin(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    FastAPI dependency — returns the current user only if Admin.

    Builds on get_current_user — first authenticates the user,
    then checks their role. Raises 403 if not an admin.

    Use this for any endpoint that requires Admin access —
    profile management, agent settings, user management,
    learning loop submissions.

    Args:
        current_user: Authenticated user (auto-injected via
                     get_current_user dependency).

    Returns:
        The authenticated Admin User object.

    Raises:
        HTTPException 403: User is not an admin.
    """
    if current_user.role != UserRole.ADMIN:
        raise ADMIN_REQUIRED_EXCEPTION
    return current_user


def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    FastAPI dependency — returns current user if active.

    Alias for get_current_user with explicit active check.
    Use when you want to be explicit about requiring an
    active account.

    Args:
        current_user: Authenticated user (auto-injected).

    Returns:
        The active authenticated User object.
    """
    if not current_user.is_active:
        raise INACTIVE_EXCEPTION
    return current_user


def verify_org_access(
    org_name:     str,
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Verifies that the current user belongs to the requested org.

    Prevents users from one organization accessing another
    organization's grant results, learning log, or settings.

    Admins of an organization can only access their own org's data.
    There is no super-admin that can see all organizations.

    Args:
        org_name:     The organization name being requested.
        current_user: Authenticated user (auto-injected).

    Returns:
        The verified User object.

    Raises:
        HTTPException 403: User does not belong to this org.
    """
    if current_user.org_name.lower() != org_name.lower():
        raise ORG_MISMATCH_EXCEPTION
    return current_user


def get_optional_user(
    token: str     = Depends(oauth2_scheme),
    db:    Session = Depends(get_db)
) -> User | None:
    """
    FastAPI dependency — returns current user or None if not logged in.

    Use for endpoints that work for both authenticated and
    anonymous users but provide different responses for each.

    Args:
        token: JWT token from Authorization header.
        db:    Database session.

    Returns:
        User object if authenticated, None if not.
    """
    try:
        return get_current_user(token=token, db=db)
    except HTTPException:
        return None