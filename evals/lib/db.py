# -*- coding: utf-8 -*-
"""
Database utilities for Sewa Setu evaluation harness.
Strictly safety-locked to the staging database.
"""

import os
import subprocess


def run_staging_psql(sql: str, docker_project: str = "sewasetu-staging", db_name: str = "sewasetu_staging") -> str:
    """Run SQL command against staging Postgres container."""
    cmd = [
        "docker", "compose", "-p", docker_project, "exec", "-T", "db",
        "psql", "-U", "sewasetu", "-d", db_name, "-t", "-A", "-F", "|", "-c", sql,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Staging psql execution failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout.strip()


def query_staging_db(sql: str) -> list[list[str]]:
    """Execute SQL query and return rows split by pipe."""
    out = run_staging_psql(sql)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("INSERT ") and not line.startswith("DELETE ") and not line.startswith("UPDATE "):
            rows.append(line.split("|"))
    return rows
