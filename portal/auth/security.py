"""
portal/auth/security.py
-----------------------
Password hashing and JWT token management for the portal.

Two responsibilities:
    1. Password security — hash passwords before storing,
       verify passwords on login. Uses bcrypt via passlib.

    2. JWT tokens — create tokens on login, decode tokens
       on every authenticated request. Uses python-jose.

Security standards:
    - Passwords hashed with bcrypt (cost factor 12)
    - JWT tokens signed with HS256 algorithm
    - Tokens expire after configured duration
    - Secret key loaded from .env — never hardcoded
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from jose import JWTError, jwt
from passlib.context import CryptContext

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# All security settings loaded from environment variables.
# ─────────────────────────────────────────────────────────────────────────────

# Secret key for signing JWT tokens.
# MUST be set in .env (or any secrets manager) for any non-local deployment.
# If unset, or left at the well-known placeholder string, we generate an
# ephemeral random key at process start so local development still works —
# but every restart invalidates all existing JWTs and we print a loud
# warning so this never silently ships to production.
_PLACEHOLDER_SECRET = "change-this-in-production-use-a-long-random-string"
SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY or SECRET_KEY == _PLACEHOLDER_SECRET:
    SECRET_KEY = secrets.token_urlsafe(64)
    print(
        "\n[security] WARNING: SECRET_KEY is not set (or is the known placeholder).\n"
        "[security]          Generated a one-shot ephemeral key for this process.\n"
        "[security]          All issued JWTs will be invalidated on restart.\n"
        "[security]          Set SECRET_KEY in your .env before any deployment.\n"
        "[security]          Generate with:  python -c \"import secrets; print(secrets.token_urlsafe(64))\"\n",
        file=sys.stderr,
    )

# Algorithm used to sign JWT tokens
ALGORITHM = "HS256"

# How long access tokens are valid (in minutes)
ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480")  # 8 hours default
)

# ─────────────────────────────────────────────────────────────────────────────
# Password hashing
# CryptContext handles the bcrypt hashing and verification.
# deprecated="auto" automatically upgrades old hash schemes.
# ─────────────────────────────────────────────────────────────────────────────

pwd_context = CryptContext(
    schemes    = ["bcrypt"],
    deprecated = "auto"
)


# ─────────────────────────────────────────────────────────────────────────────
# Password functions
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(plain_password: str) -> str:
    """
    Hashes a plain-text password using bcrypt.

    Never store plain-text passwords. Always hash before saving
    to the database. The hash is a one-way transformation —
    the original password cannot be recovered from the hash.

    Args:
        plain_password: The plain-text password from the user.

    Returns:
        Bcrypt hash string safe to store in the database.

    Example:
        hashed = hash_password("mypassword123")
        # Returns something like:
        # $2b$12$... (60 character hash)
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies a plain-text password against a stored bcrypt hash.

    Hashes the plain password and compares it to the stored hash.
    Returns True if they match, False otherwise.

    Args:
        plain_password:  Password typed by the user at login.
        hashed_password: Hash stored in the database.

    Returns:
        True if password is correct, False if not.

    Example:
        is_valid = verify_password("mypassword123", stored_hash)
    """
    return pwd_context.verify(plain_password, hashed_password)


# ─────────────────────────────────────────────────────────────────────────────
# JWT token functions
# ─────────────────────────────────────────────────────────────────────────────

def create_access_token(
    data:       dict,
    expires_in: Optional[timedelta] = None
) -> str:
    """
    Creates a signed JWT access token.

    The token encodes the user's email, role, and org_name
    so the server can identify who is making each request
    without a database lookup.

    Args:
        data:       Dictionary of claims to encode in the token.
                   Must include 'sub' (subject = user email).
        expires_in: How long the token is valid. Defaults to
                   ACCESS_TOKEN_EXPIRE_MINUTES from settings.

    Returns:
        Signed JWT token string.

    Example:
        token = create_access_token({
            "sub":      "mary@deborahsplace.org",
            "role":     "admin",
            "org_name": "Deborah's Place"
        })
    """
    to_encode = data.copy()

    if expires_in:
        expire = datetime.now(timezone.utc) + expires_in
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decodes and validates a JWT access token.

    Verifies the token signature and expiration. Returns the
    decoded payload if valid, None if invalid or expired.

    Args:
        token: JWT token string from the Authorization header.

    Returns:
        Decoded token payload dictionary if valid, None if not.

    Example:
        payload = decode_access_token(token)
        if payload:
            email = payload.get("sub")
            role  = payload.get("role")
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_token_email(token: str) -> Optional[str]:
    """
    Extracts the user email from a JWT token.

    Convenience function used by auth dependencies to quickly
    identify which user is making a request.

    Args:
        token: JWT token string.

    Returns:
        User email string or None if token is invalid.
    """
    payload = decode_access_token(token)
    if payload:
        return payload.get("sub")
    return None


def get_token_role(token: str) -> Optional[str]:
    """
    Extracts the user role from a JWT token.

    Used by role-based access control to check whether
    a user has permission to access a specific endpoint.

    Args:
        token: JWT token string.

    Returns:
        Role string ('admin' or 'user') or None if invalid.
    """
    payload = decode_access_token(token)
    if payload:
        return payload.get("role")
    return None