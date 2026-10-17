# -*- coding: utf-8 -*-
"""
OAuth 2.1 client automation for Sewa Setu evaluation harness.
Drives real OAuth code flows with PKCE against staging stack.
"""

import re
import time
import base64
import hashlib
import secrets
import requests


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def get_staging_sms_otp(base_url: str, mobile: str, max_wait: int = 10) -> str:
    """Read SMS OTP delivered to staging gateway."""
    for _ in range(max_wait):
        time.sleep(1)
        try:
            r = requests.get(f"{base_url}/__gateway/api/messages?to={mobile}", timeout=5).json()
            msgs = r.get("messages", [])
            if msgs:
                m = re.search(r"\b(\d{6})\b", msgs[-1].get("text", ""))
                if m:
                    return m.group(1)
        except Exception:
            pass
    raise TimeoutError(f"No OTP delivered to {mobile} on staging gateway within {max_wait}s")


def register_eval_client(base_url: str, client_name: str, scope: str) -> dict:
    """Register public OAuth client via DCR."""
    redirect_uri = "http://localhost:9999/callback"
    r = requests.post(
        f"{base_url}/oauth/register",
        json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "scope": scope,
        },
        timeout=5,
    )
    if r.status_code != 201:
        raise RuntimeError(f"DCR registration failed ({r.status_code}): {r.text}")
    data = r.json()
    data["redirect_uri"] = redirect_uri
    return data


def perform_citizen_oauth(base_url: str, mobile: str, resource: str | None = None) -> dict:
    """Full citizen OAuth 2.1 authorization code flow with PKCE."""
    client = register_eval_client(base_url, f"Eval Citizen {mobile}", scope="citizen")
    if resource is None:
        resource = f"{base_url}/mcp/citizen"

    verifier = secrets.token_urlsafe(32)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_hex(8)

    s = requests.Session()
    # 1. /oauth/authorize
    r_auth = s.get(
        f"{base_url}/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uri"],
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "citizen",
            "resource": resource,
            "state": state,
        },
        allow_redirects=False,
    )
    if r_auth.status_code not in (302, 303):
        raise RuntimeError(f"Authorize failed ({r_auth.status_code}): {r_auth.text}")
    m_rid = re.search(r"rid=([^&]+)", r_auth.headers.get("Location", ""))
    if not m_rid:
        raise RuntimeError(f"No rid found in authorize location: {r_auth.headers.get('Location')}")
    rid = m_rid.group(1)

    # 2. Enter mobile
    s.post(f"{base_url}/oauth/login?rid={rid}", data={"mobile": mobile}, allow_redirects=False)

    # 3. Read OTP & submit
    otp = get_staging_sms_otp(base_url, mobile)
    s.post(f"{base_url}/oauth/login?rid={rid}", data={"otp": otp}, allow_redirects=False)

    # 4. Consent
    r_consent = s.post(f"{base_url}/oauth/consent?rid={rid}", data={"decision": "allow"}, allow_redirects=False)
    if r_consent.status_code not in (302, 303):
        raise RuntimeError(f"Consent failed ({r_consent.status_code}): {r_consent.text}")
    code = re.search(r"code=([^&]+)", r_consent.headers.get("Location", "")).group(1)

    # 5. Token exchange
    r_tok = requests.post(
        f"{base_url}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "code": code,
            "redirect_uri": client["redirect_uri"],
            "code_verifier": verifier,
        },
        timeout=5,
    )
    if r_tok.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({r_tok.status_code}): {r_tok.text}")
    res = r_tok.json()
    res["client"] = client
    return res


def perform_officer_oauth(base_url: str, username: str = "admin", password: str = "staging-admin-pass-2026",
                          resource: str | None = None) -> dict:
    """Full officer OAuth 2.1 authorization code flow with PKCE."""
    client = register_eval_client(base_url, "Eval Officer", scope="officer")
    if resource is None:
        resource = f"{base_url}/mcp/officer"

    verifier = secrets.token_urlsafe(32)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_hex(8)

    s = requests.Session()
    # 1. /oauth/authorize
    r_auth = s.get(
        f"{base_url}/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uri"],
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "officer",
            "resource": resource,
            "state": state,
        },
        allow_redirects=False,
    )
    if r_auth.status_code not in (302, 303):
        raise RuntimeError(f"Officer authorize failed ({r_auth.status_code}): {r_auth.text}")
    rid = re.search(r"rid=([^&]+)", r_auth.headers.get("Location", "")).group(1)

    # 2. Officer credentials
    s.post(f"{base_url}/oauth/login?rid={rid}", data={"username": username, "password": password}, allow_redirects=False)

    # 3. Consent
    r_consent = s.post(f"{base_url}/oauth/consent?rid={rid}", data={"decision": "allow"}, allow_redirects=False)
    if r_consent.status_code not in (302, 303):
        raise RuntimeError(f"Officer consent failed ({r_consent.status_code}): {r_consent.text}")
    code = re.search(r"code=([^&]+)", r_consent.headers.get("Location", "")).group(1)

    # 4. Token exchange
    r_tok = requests.post(
        f"{base_url}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "code": code,
            "redirect_uri": client["redirect_uri"],
            "code_verifier": verifier,
        },
        timeout=5,
    )
    if r_tok.status_code != 200:
        raise RuntimeError(f"Officer token exchange failed ({r_tok.status_code}): {r_tok.text}")
    res = r_tok.json()
    res["client"] = client
    return res


def revoke_refresh_token(base_url: str, refresh_token: str, client_id: str, client_secret: str = "") -> bool:
    """Revoke a refresh token via /oauth/revoke."""
    r = requests.post(
        f"{base_url}/oauth/revoke",
        data={
            "token": refresh_token,
            "token_type_hint": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=5,
    )
    return r.status_code == 200
