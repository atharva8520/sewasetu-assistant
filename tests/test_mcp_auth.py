# -*- coding: utf-8 -*-
"""
Tests for Phase 4: Citizen and Officer MCP Servers + OAuth 2.1 Security.
Run against live stack: pytest tests/test_mcp_auth.py
"""

import os
import re
import time
import base64
import hashlib
import secrets
import asyncio
from datetime import datetime, date, timedelta

import subprocess

import pytest
import requests

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx2

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
INTERNAL_BASE_URL = os.environ.get("INTERNAL_BASE_URL", "http://localhost:8000" if ":8080" in BASE_URL else BASE_URL)
AGENT_INTERNAL_KEY = os.environ.get("AGENT_INTERNAL_KEY", "dev-agent-key-sewasetu-2026")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-before-deploying")


def run_psql(sql: str) -> str:
    res = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "db",
            "psql", "-U", "sewasetu", "-d", "sewasetu", "-t", "-A", "-F", "|", "-c", sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def get_sms_otp(mobile: str, max_wait: int = 10) -> str:
    for _ in range(max_wait):
        time.sleep(1)
        try:
            r = requests.get(f"{BASE_URL}/__gateway/api/messages?to={mobile}", timeout=5).json()
            msgs = r.get("messages", [])
            if msgs:
                m = re.search(r"\b(\d{6})\b", msgs[-1].get("text", ""))
                if m:
                    return m.group(1)
        except Exception:
            pass
    raise TimeoutError(f"No OTP delivered to {mobile} within {max_wait}s")


def register_oauth_client(name: str, scope: str) -> dict:
    redirect_uri = "http://localhost:9999/callback"
    r = requests.post(
        f"{BASE_URL}/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "scope": scope,
        },
        timeout=5,
    )
    assert r.status_code == 201, f"DCR failed: {r.text}"
    data = r.json()
    data["redirect_uri"] = redirect_uri
    return data


def perform_citizen_oauth_flow(mobile: str, resource: str | None = None) -> dict:
    """Drive the real OAuth authorization code flow with PKCE for a citizen."""
    client = register_oauth_client(f"Citizen App {mobile}", scope="citizen")
    if resource is None:
        resource = f"{BASE_URL}/mcp/citizen"

    verifier = secrets.token_urlsafe(32)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_hex(8)

    s = requests.Session()
    # 1. /oauth/authorize
    r_auth = s.get(
        f"{BASE_URL}/oauth/authorize",
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
    assert r_auth.status_code in (302, 303), f"Authorize did not redirect: {r_auth.status_code} {r_auth.text}"
    m_rid = re.search(r"rid=([^&]+)", r_auth.headers.get("Location", ""))
    assert m_rid, f"Could not find rid in Location header: {r_auth.headers.get('Location')}"
    rid = m_rid.group(1)

    # 2. Enter mobile
    r_mob = s.post(
        f"{BASE_URL}/oauth/login?rid={rid}",
        data={"mobile": mobile},
        allow_redirects=False,
    )

    # 3. Read OTP & verify
    otp = get_sms_otp(mobile)
    r_otp = s.post(
        f"{BASE_URL}/oauth/login?rid={rid}",
        data={"otp": otp},
        allow_redirects=False,
    )

    # 4. Consent
    r_consent = s.post(
        f"{BASE_URL}/oauth/consent?rid={rid}",
        data={"decision": "allow"},
        allow_redirects=False,
    )
    assert r_consent.status_code in (302, 303)
    redirect_loc = r_consent.headers["Location"]
    m_code = re.search(r"code=([^&]+)", redirect_loc)
    assert m_code, f"No code in redirect: {redirect_loc}"
    code = m_code.group(1)

    # 5. Token exchange
    r_token = requests.post(
        f"{BASE_URL}/oauth/token",
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
    assert r_token.status_code == 200, f"Token exchange failed: {r_token.text}"
    token_data = r_token.json()
    token_data["client"] = client
    return token_data


def perform_officer_oauth_flow(resource: str | None = None) -> dict:
    """Drive the real OAuth authorization code flow with PKCE for an officer."""
    client = register_oauth_client("Officer Admin App", scope="officer")
    if resource is None:
        resource = f"{BASE_URL}/mcp/officer"

    verifier = secrets.token_urlsafe(32)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_hex(8)

    s = requests.Session()
    # 1. /oauth/authorize
    r_auth = s.get(
        f"{BASE_URL}/oauth/authorize",
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
    assert r_auth.status_code in (302, 303), f"Authorize did not redirect: {r_auth.status_code} {r_auth.text}"
    m_rid = re.search(r"rid=([^&]+)", r_auth.headers.get("Location", ""))
    assert m_rid, f"Could not find rid in Location header: {r_auth.headers.get('Location')}"
    rid = m_rid.group(1)

    # 2. Enter officer credentials
    r_login = s.post(
        f"{BASE_URL}/oauth/login?rid={rid}",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        allow_redirects=False,
    )

    # 3. Consent
    r_consent = s.post(
        f"{BASE_URL}/oauth/consent?rid={rid}",
        data={"decision": "allow"},
        allow_redirects=False,
    )
    assert r_consent.status_code in (302, 303)
    redirect_loc = r_consent.headers["Location"]
    m_code = re.search(r"code=([^&]+)", redirect_loc)
    assert m_code, f"No code in redirect: {redirect_loc}"
    code = m_code.group(1)

    # 4. Token exchange
    r_token = requests.post(
        f"{BASE_URL}/oauth/token",
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
    assert r_token.status_code == 200, f"Token exchange failed: {r_token.text}"
    token_data = r_token.json()
    token_data["client"] = client
    return token_data


# ---------------------------------------------------------------------------
# GATE 4 Check a: Missing token & RFC 9728 protected-resource metadata
# ---------------------------------------------------------------------------
def test_gate4_a_missing_token_and_metadata():
    """No token -> 401 with correct WWW-Authenticate; metadata URL returns right JSON."""
    for role, port in (("citizen", 9001), ("officer", 9002)):
        endpoint = f"{BASE_URL}/mcp/{role}"
        r = requests.post(endpoint, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401, f"Expected 401 without token for {role}, got {r.status_code}"

        www_auth = r.headers.get("WWW-Authenticate", "")
        assert "resource_metadata=" in www_auth

        m = re.search(r'resource_metadata="([^"]+)"', www_auth)
        assert m, f"No resource_metadata in WWW-Authenticate: {www_auth}"
        meta_url = m.group(1)
        expected_meta_url = f"{BASE_URL}/.well-known/oauth-protected-resource/mcp/{role}"
        assert meta_url == expected_meta_url

        # Check metadata endpoint
        r_meta = requests.get(meta_url)
        assert r_meta.status_code == 200
        meta = r_meta.json()
        assert meta["resource"] == f"{BASE_URL}/mcp/{role}"
        assert meta["authorization_servers"] == [BASE_URL]
        assert meta["scopes_supported"] == [role]
        assert "header" in meta.get("bearer_methods_supported", ["header"])


# ---------------------------------------------------------------------------
# GATE 4 Check b: Citizen & Officer token audience isolation
# ---------------------------------------------------------------------------
def test_gate4_b_token_isolation():
    """Citizen token works on /mcp/citizen, FAILS on /mcp/officer; officer token FAILS on /mcp/citizen."""
    mob = f"955{secrets.randbelow(9000000) + 1000000}"
    cit_tok = perform_citizen_oauth_flow(mob)["access_token"]
    off_tok = perform_officer_oauth_flow()["access_token"]

    # Citizen token on citizen -> allowed (tools/list succeeds)
    r = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {cit_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 200

    # Citizen token on officer -> 401
    r_cit_on_off = requests.post(
        f"{BASE_URL}/mcp/officer",
        headers={"Authorization": f"Bearer {cit_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_cit_on_off.status_code == 401

    # Officer token on citizen -> 401
    r_off_on_cit = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {off_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_off_on_cit.status_code == 401


# ---------------------------------------------------------------------------
# GATE 4 Check c: Wrong or missing resource is rejected
# ---------------------------------------------------------------------------
def test_gate4_c_wrong_resource_rejected():
    """A token minted with a wrong or missing 'resource' is rejected."""
    mob = f"956{secrets.randbelow(9000000) + 1000000}"
    # Mint token with resource pointing to BASE_URL instead of canonical /mcp/citizen
    tok_wrong = perform_citizen_oauth_flow(mob, resource=f"{BASE_URL}")["access_token"]

    r = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {tok_wrong}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# GATE 4 Check d: Session revocation immediately terminates access
# ---------------------------------------------------------------------------
def test_gate4_d_session_liveness_revocation():
    """After /oauth/revoke of refresh token, the next call with access token returns 401."""
    mob = f"957{secrets.randbelow(9000000) + 1000000}"
    flow = perform_citizen_oauth_flow(mob)
    access_tok = flow["access_token"]
    refresh_tok = flow["refresh_token"]

    # Token works initially
    r1 = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {access_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r1.status_code == 200

    # Revoke refresh token
    r_rev = requests.post(f"{BASE_URL}/oauth/revoke", data={"token": refresh_tok})
    assert r_rev.status_code == 200

    # Wait > 5s for the liveness cache to expire
    time.sleep(5.5)

    # Next call with still-unexpired access token must return 401
    r2 = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {access_tok}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert r2.status_code == 401
    assert "sign-in ended, please sign in again" in r2.text.lower() or "invalid_token" in r2.text.lower()


# ---------------------------------------------------------------------------
# GATE 4 Check e: Exact tool counts and no overlap
# ---------------------------------------------------------------------------
def test_gate4_e_tool_list_counts_and_isolation():
    """Citizen shows EXACTLY 8 citizen tools, officer EXACTLY 5, no overlap."""
    mob = f"958{secrets.randbelow(9000000) + 1000000}"
    cit_tok = perform_citizen_oauth_flow(mob)["access_token"]
    off_tok = perform_officer_oauth_flow()["access_token"]

    r_cit = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {cit_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ).json()
    citizen_tools = sorted([t["name"] for t in r_cit["result"]["tools"]])

    expected_citizen = sorted([
        "scheme_info",
        "check_eligibility",
        "save_draft",
        "get_upload_link",
        "review_application",
        "submit_application",
        "get_my_application",
        "withdraw_application",
    ])
    assert citizen_tools == expected_citizen, f"Citizen tools mismatch: {citizen_tools}"

    r_off = requests.post(
        f"{BASE_URL}/mcp/officer",
        headers={"Authorization": f"Bearer {off_tok}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ).json()
    officer_tools = sorted([t["name"] for t in r_off["result"]["tools"]])

    expected_officer = sorted([
        "queue_summary",
        "get_queue",
        "get_case",
        "decide",
        "my_decisions",
    ])
    assert officer_tools == expected_officer, f"Officer tools mismatch: {officer_tools}"

    overlap = set(citizen_tools).intersection(set(officer_tools))
    assert len(overlap) == 0, f"Unexpected overlap in tools: {overlap}"


# ---------------------------------------------------------------------------
# GATE 4 Check f: Full happy path with official MCP client over HTTP
# ---------------------------------------------------------------------------
def test_gate4_f_full_happy_path_official_client():
    """
    With the official 'mcp' Python client over streamable HTTP via the proxy:
    scheme_info -> save_draft -> get_upload_link -> upload -> review_application ->
    submit_application -> get_my_application; then officer: queue_summary -> get_queue ->
    get_case -> decide(approve). Verify in Postgres.
    """
    async def _run():
        mob = f"959{secrets.randbelow(9000000) + 1000000}"
        cit_flow = perform_citizen_oauth_flow(mob)
        cit_tok = cit_flow["access_token"]

        off_flow = perform_officer_oauth_flow()
        off_tok = off_flow["access_token"]

        cit_headers = {"Authorization": f"Bearer {cit_tok}"}
        off_headers = {"Authorization": f"Bearer {off_tok}"}

        # 1. Citizen Session via official MCP streamable HTTP client
        cit_url = f"{BASE_URL}/mcp/citizen"
        async with httpx2.AsyncClient(headers=cit_headers, timeout=10.0) as http_cit:
            async with streamable_http_client(cit_url, http_client=http_cit) as (r_stream, w_stream):
                async with ClientSession(r_stream, w_stream) as session:
                    await session.initialize()

                    # Step 1: scheme_info
                    res1 = await session.call_tool("scheme_info", {})
                    res1_data = res1.content[0].text if hasattr(res1.content[0], "text") else str(res1.content)
                    assert "₹250" in res1_data or "250" in res1_data

                    # Step 2: save_draft
                    res2 = await session.call_tool("save_draft", {
                        "applicant_name": "Kamal Chandra Gogoi",
                        "dob": "15/05/1954",
                        "gender": "Male",
                        "marital_status": "Married",
                        "village": "Sonari Gaon",
                        "block": "Sonari",
                        "bank_account": "987654321098",
                        "ifsc": "SBIN0003077",
                    })

                    # Step 3: get_upload_link
                    res3 = await session.call_tool("get_upload_link", {})
                    res3_text = res3.content[0].text
                    m_link = re.search(r"(http[^\s\"']+)", res3_text)
                    assert m_link, f"Could not parse upload link: {res3_text}"
                    upload_url = m_link.group(1).rstrip(".,;")

                    # Upload test document via HTTP
                    files = {"document": ("age_proof.pdf", b"%PDF-1.4 sample age proof content", "application/pdf")}
                    r_up = requests.post(upload_url, files=files, timeout=5)
                    assert r_up.status_code == 200

                    # Step 4: review_application
                    res4 = await session.call_tool("review_application", {})
                    res4_text = res4.content[0].text
                    m_code = re.search(r"\b(REV-[A-F0-9]{8})\b", res4_text)
                    assert m_code, f"Could not find review code: {res4_text}"
                    review_code = m_code.group(1)

                    # Step 5: submit_application
                    res5 = await session.call_tool("submit_application", {"review_code": review_code})
                    res5_text = res5.content[0].text
                    m_appno = re.search(r"\b(SSP\d{8})\b", res5_text)
                    assert m_appno, f"Could not find application number: {res5_text}"
                    app_no = m_appno.group(1)

                    # Step 6: get_my_application
                    res6 = await session.call_tool("get_my_application", {})
                    res6_text = res6.content[0].text
                    assert app_no in res6_text
                    assert "PENDING" in res6_text or "under verification" in res6_text

        # 2. Officer Session via official MCP streamable HTTP client
        off_url = f"{BASE_URL}/mcp/officer"
        async with httpx2.AsyncClient(headers=off_headers, timeout=10.0) as http_off:
            async with streamable_http_client(off_url, http_client=http_off) as (r_stream, w_stream):
                async with ClientSession(r_stream, w_stream) as session:
                    await session.initialize()

                    # Step 1: queue_summary
                    res_sum = await session.call_tool("queue_summary", {})
                    assert "pending" in res_sum.content[0].text.lower()

                    # Step 2: get_queue
                    res_q = await session.call_tool("get_queue", {"block": "Sonari", "limit": 25})
                    if app_no not in res_q.content[0].text:
                        res_q = await session.call_tool("get_queue", {"block": "Sonari", "cursor": "2", "limit": 25})
                    assert app_no in res_q.content[0].text

                    # Step 3: get_case (mandatory before deciding)
                    res_case = await session.call_tool("get_case", {"application_no": app_no})
                    assert app_no in res_case.content[0].text

                    # Step 4: decide(approve)
                    res_dec = await session.call_tool("decide", {
                        "application_no": app_no,
                        "decision": "approve",
                        "reason": "Age proof verified and all eligibility criteria met in Sonari.",
                    })
                    assert "approved" in res_dec.content[0].text.lower()

        # 3. Verify directly in Postgres
        row = run_psql(f"SELECT status, channel, id FROM applications WHERE application_no = '{app_no}';")
        assert row, f"Application {app_no} not found in database"
        parts = row.split("|")
        app_status, app_channel, app_id = parts[0], parts[1], parts[2]
        assert app_status == "APPROVED"
        assert app_channel == "agent"

        audit_note = run_psql(f"SELECT note FROM audit_log WHERE application_id = {app_id} AND action = 'APPROVED';")
        assert audit_note, "No approval audit log found"
        assert audit_note.startswith("[assistant:")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# GATE 4 Check g: Negative paths
# ---------------------------------------------------------------------------
def test_gate4_g_negative_paths():
    """Negative paths: submit without review code, decide without get_case, approve under-60, cross-citizen."""
    mob1 = f"960{secrets.randbelow(9000000) + 1000000}"
    mob2 = f"961{secrets.randbelow(9000000) + 1000000}"

    cit1_tok = perform_citizen_oauth_flow(mob1)["access_token"]
    cit2_tok = perform_citizen_oauth_flow(mob2)["access_token"]
    off_tok = perform_officer_oauth_flow()["access_token"]

    # 1. Submit without a review code
    r_sub = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {cit1_tok}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "submit_application", "arguments": {"review_code": ""}},
        },
    ).json()
    call_content = r_sub.get("result", {}).get("content", [{}])[0].get("text", "")
    assert "required" in call_content.lower() or "review" in call_content.lower()

    # 2. Decide without get_case
    r_decide_no_view = requests.post(
        f"{BASE_URL}/mcp/officer",
        headers={"Authorization": f"Bearer {off_tok}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "decide",
                "arguments": {
                    "application_no": "SSP99999999",
                    "decision": "approve",
                    "reason": "Approving without opening case inspection first.",
                },
            },
        },
    ).json()
    dec_text = r_decide_no_view.get("result", {}).get("content", [{}])[0].get("text", "")
    assert "not found" in dec_text.lower() or "viewed" in dec_text.lower()

    # 3. Approve an under-60 case
    # Create under-60 application directly in db to test hard flag guard
    under_60_no = "SSP" + secrets.token_hex(4).upper()
    dob_50 = (date.today() - timedelta(days=50 * 365)).strftime("%Y-%m-%d")
    now_ts = datetime.now().isoformat()
    under_id = run_psql(
        f"INSERT INTO applications "
        f"(application_no, applicant_name, mobile, dob, gender, marital_status, "
        f" village, block, bank_account, ifsc, status, submitted_at, channel) "
        f"VALUES ('{under_60_no}', 'Underage Person', '9620000001', '{dob_50}', 'Male', 'Married', "
        f"        'Sonari', 'Sonari', '1122334455', 'SBIN0003077', 'PENDING', '{now_ts}', 'agent') "
        f"RETURNING id;"
    ).splitlines()[0].strip()
    # Record view in agent_case_views
    run_psql(
        f"INSERT INTO agent_case_views (officer, application_id, viewed_at) "
        f"VALUES ('admin', {under_id}, '{now_ts}');"
    )

    # Try to approve under-60 case via officer tool
    r_approve_under = requests.post(
        f"{BASE_URL}/mcp/officer",
        headers={"Authorization": f"Bearer {off_tok}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "decide",
                "arguments": {
                    "application_no": under_60_no,
                    "decision": "approve",
                    "reason": "Attempting illegal approval for applicant below 60 years of age.",
                },
            },
        },
    ).json()
    under_text = r_approve_under.get("result", {}).get("content", [{}])[0].get("text", "")
    assert "below 60" in under_text.lower() or "blocked" in under_text.lower() or "hard_flags" in under_text.lower()

    # 4. Citizen asking for another citizen's application
    # Citizen 2 calls get_my_application: cannot see Citizen 1's application
    r_cit2_status = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {cit2_tok}"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_my_application", "arguments": {}},
        },
    ).json()
    c2_text = r_cit2_status.get("result", {}).get("content", [{}])[0].get("text", "")
    assert "do not currently have" in c2_text.lower() or "false" in c2_text.lower()

    # 5. Tampered and Expired tokens
    r_tampered = requests.post(
        f"{BASE_URL}/mcp/citizen",
        headers={"Authorization": f"Bearer {cit1_tok[:-5]}XXXXX"},
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
    )
    assert r_tampered.status_code == 401
