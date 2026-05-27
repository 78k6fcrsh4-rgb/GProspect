#!/usr/bin/env python3
"""
scripts/set_user_password.py
----------------------------
Admin override: rewrite a user's password directly in the DB without
needing their current password.

Use this for:
  - Lost / forgotten passwords (until self-serve reset-by-email is wired)
  - First-time provisioning when you set a generic password via
    create_admin_user.py and want to rotate it before handing the
    account off
  - Emergency lock-out recovery

Self-service password change should go through the frontend's
👤 Account tab → POST /auth/change-password (current-pw required,
rate-limited). This script is the *operator* tool, not the user-facing
flow.

Examples:

    cd ~/WorkBench/AI4GSH/lsrmba777

    # Rotate the Found Village admin password
    .venv/bin/python scripts/set_user_password.py \\
        --email        admin@foundvillage.org \\
        --new-password 'a-real-secure-passphrase'

    # Lock out a compromised account by setting an unguessable random pw
    .venv/bin/python scripts/set_user_password.py \\
        --email   compromised@example.org \\
        --random 64

Increments the user's token_version too, which invalidates every
outstanding JWT for that user. They will need to sign in fresh with
the new password.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

# Project importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db                  import SessionLocal
from portal.auth.security         import hash_password
from portal.models.user           import User

# Force-import every model so SQLAlchemy's class registry is fully populated
# before the first query — same trap as create_admin_user.py.
from portal.models import (                                       # noqa: F401
    organization, org_profile, user, result, learning,
    opportunity, funder_candidate, grant, capacity, scheduled_run, source,
)


def main():
    p = argparse.ArgumentParser(
        description     = __doc__,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--email", required=True,
                   help="Email of the user whose password you're rewriting.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--new-password",
                       help="The new plaintext password to set.")
    group.add_argument("--random", type=int, metavar="LENGTH",
                       help="Generate a cryptographically-random URL-safe "
                            "password of the given length (use 32+ for "
                            "real security) and print it once. "
                            "Useful for locking accounts.")
    args = p.parse_args()

    if args.new_password is not None:
        if len(args.new_password) < 8:
            print("ERROR: --new-password must be at least 8 characters.",
                  file=sys.stderr)
            sys.exit(2)
        new_pw = args.new_password
        printed_to_stdout = False
    else:
        # secrets.token_urlsafe(n) returns ~1.33n characters of URL-safe text.
        # Multiply input by 1 so the user gets at least the requested length.
        raw_bytes = max(args.random, 16)
        new_pw = secrets.token_urlsafe(raw_bytes)[:max(args.random, 16)]
        printed_to_stdout = True

    db = SessionLocal()
    try:
        user_row = (
            db.query(User)
              .filter_by(email=args.email.lower().strip())
              .one_or_none()
        )
        if user_row is None:
            print(
                f"ERROR: no user with email={args.email!r} found.",
                file = sys.stderr,
            )
            sys.exit(3)

        user_row.hashed_password = hash_password(new_pw)
        # Bump token_version so every outstanding JWT for this user
        # immediately stops working. They'll need to sign in fresh.
        user_row.token_version = (user_row.token_version or 0) + 1
        db.commit()

        print(f"✅ Password updated for {user_row.email}.")
        print(f"   Org: {user_row.org_name}  Role: {user_row.role.value}")
        print(f"   Token version: {user_row.token_version}  "
              f"(all outstanding JWTs for this user are now invalid)")
        if printed_to_stdout:
            print()
            print("RANDOM PASSWORD (copy now — won't be shown again):")
            print(f"   {new_pw}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
