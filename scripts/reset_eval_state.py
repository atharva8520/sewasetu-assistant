#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reset evaluation state in the staging database.
Refuses to run against any non-staging database.

Usage:
  python scripts/reset_eval_state.py [--mobile 9000000001] [--docker-project sewasetu-staging]
"""

import sys
import os
import argparse
import subprocess


def is_staging_db(db_name: str) -> bool:
    name = (db_name or "").lower().strip()
    return "staging" in name or name == "sewasetu_staging"


def run_psql(cmd_args: list[str], sql: str) -> str:
    full_cmd = cmd_args + ["-t", "-A", "-F", "|", "-c", sql]
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"psql failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description="Reset eval state for prefix 90000 in staging.")
    parser.add_argument("--mobile", help="Specific mobile to clear (defaults to all prefix 90000)")
    parser.add_argument("--docker-project", default=os.environ.get("COMPOSE_PROJECT_NAME", "sewasetu-staging"),
                        help="Docker compose project name (default: sewasetu-staging)")
    parser.add_argument("--db-name", default=os.environ.get("POSTGRES_DB", "sewasetu_staging"),
                        help="Postgres database name")
    parser.add_argument("--db-user", default=os.environ.get("POSTGRES_USER", "sewasetu"))
    args = parser.parse_args()

    # Safety Guard: refuse if database name or docker project is not staging
    if not is_staging_db(args.db_name) or ("staging" not in args.docker_project.lower()):
        print(
            f"REFUSAL: reset_eval_state.py refuses to run against non-staging target!\n"
            f"  Target DB name: '{args.db_name}'\n"
            f"  Docker project: '{args.docker_project}'\n"
            f"This script is strictly safety-locked to staging environments only.",
            file=sys.stderr,
        )
        sys.exit(1)

    psql_base = [
        "docker", "compose", "-p", args.docker_project, "exec", "-T", "db",
        "psql", "-U", args.db_user, "-d", args.db_name
    ]

    # Verify live database identity directly from postgres engine
    try:
        active_db = run_psql(psql_base, "SELECT current_database();").splitlines()[0].strip()
    except Exception as e:
        print(f"REFUSAL: Unable to verify staging database connection: {e}", file=sys.stderr)
        sys.exit(1)

    if not is_staging_db(active_db):
        print(
            f"REFUSAL: Connected database '{active_db}' is not a staging database!\n"
            f"Terminating immediately to protect production data.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine mobile filter pattern
    mobile_cond = f"= '{args.mobile}'" if args.mobile else "LIKE '90000%'"
    sub_cond = f"= 'citizen:{args.mobile}'" if args.mobile else "LIKE 'citizen:90000%'"

    print(f"[*] Resetting eval state on staging database '{active_db}' (mobile {mobile_cond})...")

    # 1. Collect files to delete
    doc_paths_sql = (
        f"SELECT doc_path FROM applications WHERE mobile {mobile_cond} AND doc_path IS NOT NULL AND doc_path != '' "
        f"UNION "
        f"SELECT doc_path FROM agent_drafts WHERE mobile {mobile_cond} AND doc_path IS NOT NULL AND doc_path != '';"
    )
    doc_paths_raw = run_psql(psql_base, doc_paths_sql)
    doc_paths = [p.strip() for p in doc_paths_raw.splitlines() if p.strip()]

    # 2. Delete database rows
    cleanup_sql = f"""
    BEGIN;
    -- Audit logs for matching applications
    DELETE FROM audit_log WHERE application_id IN (
        SELECT id FROM applications WHERE mobile {mobile_cond}
    );
    -- Case views for matching applications
    DELETE FROM agent_case_views WHERE application_id IN (
        SELECT id FROM applications WHERE mobile {mobile_cond}
    );
    -- Applications
    DELETE FROM applications WHERE mobile {mobile_cond};
    -- Portal users
    DELETE FROM portal_users WHERE mobile {mobile_cond};
    -- OTPs
    DELETE FROM otps WHERE mobile {mobile_cond};
    -- Agent tables
    DELETE FROM agent_drafts WHERE mobile {mobile_cond};
    DELETE FROM agent_upload_tokens WHERE mobile {mobile_cond};
    DELETE FROM agent_reviews WHERE mobile {mobile_cond};
    -- OAuth session tables
    DELETE FROM oauth_refresh_tokens WHERE subject {sub_cond};
    DELETE FROM oauth_codes WHERE subject {sub_cond};
    DELETE FROM oauth_requests WHERE pending_mobile {mobile_cond} OR subject {sub_cond};
    COMMIT;
    """
    run_psql(psql_base, cleanup_sql)

    # 3. Remove upload files from staging upload volume via container
    for path in doc_paths:
        try:
            subprocess.run(
                ["docker", "compose", "-p", args.docker_project, "exec", "-T", "app", "rm", "-f", path],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    print(f"[+] Successfully cleared eval state for mobile {mobile_cond} (removed {len(doc_paths)} uploaded file(s)).")


if __name__ == "__main__":
    main()
