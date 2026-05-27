#!/usr/bin/env python3
"""
scripts/create_admin_user.py
----------------------------
Provision a new user (admin or staff) on an existing organization.

Fills the gap left by /auth/register, which is hard-coded to the calling
admin's org. Use this to seed the FIRST admin for a newly-created org
(Found Village, future pilots, etc.).

Examples:

    # Create the first Found Village admin
    cd ~/WorkBench/AI4GSH/lsrmba777
    .venv/bin/python scripts/create_admin_user.py \\
        --org-slug   found-village \\
        --email      admin@foundvillage.org \\
        --password   ChangeMe123! \\
        --name       Cincinnati Admin \\
        --role       admin

    # Create a read-only staff user later
    .venv/bin/python scripts/create_admin_user.py \\
        --org-slug found-village \\
        --email    staff@foundvillage.org \\
        --password initial-pw-they-rotate \\
        --name     "Staff Member" \\
        --role     user

Reads DATABASE_URL from .env the same way the portal does, so SQLite +
Postgres both work without flags. Idempotent: prints a clear message
and exits non-zero if the email already exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project importable when running this as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db                  import SessionLocal
from portal.auth.security         import hash_password
from portal.models.organization   import Organization
from portal.models.user           import User, UserRole

# Force-import every v2 model so SQLAlchemy's class registry is fully
# populated before we issue any queries. Organization.profile_versions
# (and others) reference related classes by string name; if those classes
# aren't yet imported, mapper configuration fails with "name 'X' is not
# defined". Same pattern used in database.db.create_tables().
from portal.models import (                                       # noqa: F401
    organization, org_profile, user, result, learning,
    opportunity, funder_candidate, grant, capacity, scheduled_run, source,
)


def main():
    p = argparse.ArgumentParser(
        description     = __doc__,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--org-slug", required=True,
                   help="Slug of the existing org (e.g. 'found-village', "
                        "'deborahs-place').")
    p.add_argument("--email",    required=True)
    p.add_argument("--password", required=True,
                   help="Plaintext password. The user MUST rotate it on "
                        "first login.")
    p.add_argument("--name",     required=True, help="Full display name.")
    p.add_argument("--role",     default="admin", choices=("admin", "user"),
                   help="Default: admin. Use 'user' for read+export only.")
    args = p.parse_args()

    db = SessionLocal()
    try:
        org = db.query(Organization).filter_by(slug=args.org_slug).one_or_none()
        if org is None:
            print(
                f"ERROR: no organization with slug={args.org_slug!r} found.\n"
                f"List existing orgs with:\n"
                f"  python scripts/create_admin_user.py --list-orgs",
                file = sys.stderr,
            )
            sys.exit(2)

        existing = db.query(User).filter_by(email=args.email.lower().strip()).one_or_none()
        if existing is not None:
            print(
                f"ERROR: a user with email={args.email!r} already exists "
                f"(org_id={existing.org_id}, role={existing.role.value}).\n"
                f"Pick a different email, or delete the existing row first.",
                file = sys.stderr,
            )
            sys.exit(3)

        try:
            role = UserRole(args.role)
        except ValueError:
            print(f"ERROR: invalid role {args.role!r}", file=sys.stderr)
            sys.exit(2)

        user = User(
            email           = args.email.lower().strip(),
            full_name       = args.name,
            org_id          = org.id,
            org_name        = org.display_name,
            hashed_password = hash_password(args.password),
            role            = role,
            is_active       = True,
            is_verified     = True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        print(f"✅ Created user id={user.id} ({user.email}) "
              f"as {role.value} of {org.display_name!r}.")
        print(f"   They can sign in at the Streamlit frontend.")
        print(f"   ⚠️  Remind them to change the password immediately.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
