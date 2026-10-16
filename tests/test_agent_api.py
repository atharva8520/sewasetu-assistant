# -*- coding: utf-8 -*-
"""
Tests for Phase 2: Shared submission core + internal agent API.
Run against live stack: pytest tests/test_agent_api.py
"""

import re
import time
import secrets
import hashlib
import base64
import requests
import pytest
from datetime import datetime, date, timedelta

BASE_URL = "http://localhost:8000"
INTERNAL_KEY = "dev-agent-key-sewasetu-2026"
INTERNAL_HEADERS = {"X-Internal-Key": INTERNAL_KEY}


def get_sms_otp(mobile, max_wait=10):
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


# ---------------------------------------------------------------------------
# 1. Web Flow Regression
# ---------------------------------------------------------------------------

def test_web_flow_regression():
    """Verify that the original web submission flow works end-to-end identically."""
    mobile = f"900{secrets.randbelow(9000000) + 1000000}"
    s = requests.Session()

    # Step 1: /apply with captcha
    r = s.get(f"{BASE_URL}/apply")
    assert r.status_code == 200
    m = re.search(r"(\d+)\s*\+\s*(\d+)", r.text)
    assert m, "Captcha equation not found on /apply"
    captcha_ans = str(int(m.group(1)) + int(m.group(2)))

    r_post = s.post(f"{BASE_URL}/apply", data={"mobile": mobile, "captcha": captcha_ans}, allow_redirects=True)
    assert r_post.status_code == 200
    assert "verify" in r_post.url

    # Step 2: Read OTP & verify
    otp = get_sms_otp(mobile)
    r_verify = s.post(f"{BASE_URL}/verify", data={"otp": otp}, allow_redirects=True)
    assert r_verify.status_code == 200
    assert "form/1" in r_verify.url

    # Step 3: Form steps
    s.post(f"{BASE_URL}/form/1", data={
        "applicant_name": "Web Regression Applicant",
        "dob": "15/05/1954",
        "gender": "Female",
        "marital_status": "Widowed",
        "husband_name": "Late Husband"
    }, allow_redirects=True)

    s.post(f"{BASE_URL}/form/2", data={
        "village": "Bhojo",
        "block": "Sonari"
    }, allow_redirects=True)

    s.post(f"{BASE_URL}/form/3", data={
        "bank_account": "123456789012",
        "ifsc": "SBIN0003077"
    }, allow_redirects=True)

    # Step 4: Upload document
    dummy_pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
    r_up = s.post(f"{BASE_URL}/upload", files={"document": ("proof.pdf", dummy_pdf, "application/pdf")}, allow_redirects=True)
    assert r_up.status_code == 200
    assert "declaration" in r_up.url

    # Step 5: Declaration & submission
    r_decl = s.post(f"{BASE_URL}/declaration", allow_redirects=True)
    assert r_decl.status_code == 200
    assert "SSP" in r_decl.text or "confirmation" in r_decl.url


# ---------------------------------------------------------------------------
# 2. Rule Parity (>= 200 applications: valid and invalid)
# ---------------------------------------------------------------------------

def test_parity_200_cases():
    """Assert verdict and error list are identical between validate_application and the agent API across >= 200 cases."""
    # Direct import of validate_application for local ground truth
    import sys
    sys.path.insert(0, "app")
    import app as portal

    cases = []
    blocks = portal.BLOCKS

    # 1. Valid cases: varied ages 60-90, various blocks, valid IFSCs, valid bank accounts (40 cases)
    for i in range(40):
        year = 1966 - (i % 30)
        cases.append({
            "applicant_name": f"Valid Citizen {i}",
            "dob": f"10/05/{year}",
            "gender": "Female" if i % 2 == 0 else "Male",
            "marital_status": "Married",
            "husband_name": "Spouse Name",
            "village": f"Village {i}",
            "block": blocks[i % len(blocks)],
            "bank_account": f"10002000300{i:02d}",
            "ifsc": "SBIN0003077",
            "doc_path": f"/var/sewasetu/uploads/proof_{i}.pdf"
        })

    # 2. Invalid under-60: ages 18 to 59 (40 cases)
    for i in range(40):
        year = 2000 - (i % 25)  # 1975 to 2000 (age 26 to 51 in 2026)
        cases.append({
            "applicant_name": f"Young Applicant {i}",
            "dob": f"12/08/{year}",
            "gender": "Male",
            "marital_status": "Single",
            "husband_name": "",
            "village": "Sonari Town",
            "block": "Sonari",
            "bank_account": "987654321012",
            "ifsc": "SBIN0003077",
            "doc_path": "/var/sewasetu/uploads/proof.pdf"
        })

    # 3. Invalid missing fields: missing name, missing village, missing block, missing bank_account, missing doc_path (40 cases)
    for i in range(40):
        c = {
            "applicant_name": "Applicant With Missing",
            "dob": "01/01/1950",
            "gender": "Female",
            "marital_status": "Widowed",
            "husband_name": "",
            "village": "Some Village",
            "block": "Sonari",
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077",
            "doc_path": "/var/sewasetu/uploads/proof.pdf"
        }
        mod = i % 5
        if mod == 0:
            c["applicant_name"] = ""
        elif mod == 1:
            c["village"] = ""
        elif mod == 2:
            c["block"] = ""
        elif mod == 3:
            c["bank_account"] = ""
        elif mod == 4:
            c["doc_path"] = ""
        cases.append(c)

    # 4. Invalid bad IFSC: wrong length, invalid 5th char, lowercase, special characters, legacy format (40 cases)
    bad_ifscs = [
        "SBIN000307", "SBIN00030778", "sbin0003077", "SBIN1003077", "UTBI0PUR2819",
        "12345678901", "SBIN!003077", "SBIN 003077", "SBI0003077", "SBIN0000000A"
    ]
    for i in range(40):
        cases.append({
            "applicant_name": f"Bad IFSC Applicant {i}",
            "dob": "10/02/1952",
            "gender": "Male",
            "marital_status": "Married",
            "husband_name": "",
            "village": "Village",
            "block": "Sonari",
            "bank_account": "123456789012",
            "ifsc": bad_ifscs[i % len(bad_ifscs)],
            "doc_path": "/var/sewasetu/uploads/proof.pdf"
        })

    # 5. Invalid bad bank digits: < 9 digits, > 18 digits, non-numeric (30 cases)
    bad_banks = [
        "12345678", "1234567", "1234567890123456789", "12345678901234567890",
        "12345ABC89", "1234-5678-90", "1234 5678 90", "0000", "9999999999999999999"
    ]
    for i in range(30):
        cases.append({
            "applicant_name": f"Bad Bank Applicant {i}",
            "dob": "14/04/1955",
            "gender": "Female",
            "marital_status": "Widowed",
            "husband_name": "",
            "village": "Village",
            "block": "Sonari",
            "bank_account": bad_banks[i % len(bad_banks)],
            "ifsc": "SBIN0003077",
            "doc_path": "/var/sewasetu/uploads/proof.pdf"
        })

    # 6. Invalid DOB formats and invalid dates (25 cases)
    bad_dobs = [
        "1950-01-01", "01-01-1950", "32/01/1950", "00/01/1950", "15/13/1950",
        "15/00/1950", "not-a-date", "", "1950/05/10", "1/1/1950", "31/02/1950"
    ]
    for i in range(25):
        cases.append({
            "applicant_name": f"Bad DOB Applicant {i}",
            "dob": bad_dobs[i % len(bad_dobs)],
            "gender": "Male",
            "marital_status": "Single",
            "husband_name": "",
            "village": "Village",
            "block": "Sonari",
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077",
            "doc_path": "/var/sewasetu/uploads/proof.pdf"
        })

    assert len(cases) >= 215, f"Expected >= 200 cases, generated {len(cases)}"

    # Test each case via direct python function vs internal agent validation endpoint
    for idx, c in enumerate(cases):
        doc = c.get("doc_path", "")
        expected_errs, _ = portal.validate_application(c, doc)
        expected_ok = len(expected_errs) == 0

        # Query internal agent API
        resp = requests.post(
            f"{BASE_URL}/internal/agent/validate",
            headers=INTERNAL_HEADERS,
            json={"data": c, "doc_path": doc},
            timeout=5
        )
        assert resp.status_code == 200, f"Case {idx} failed API call: {resp.text}"
        res = resp.json()
        api_ok = res["ok"]
        api_errs = res["errors"]

        assert api_ok == expected_ok, f"Case {idx} verdict mismatch! Expected {expected_ok}, got {api_ok}. Data: {c}"
        assert api_errs == expected_errs, f"Case {idx} error list mismatch!\nExpected: {expected_errs}\nGot: {api_errs}\nData: {c}"


# ---------------------------------------------------------------------------
# 3. Agent-created application in /admin/applications and audit trail
# ---------------------------------------------------------------------------

def test_agent_application_in_admin():
    """An agent-created application appears in /admin with channel 'agent' and audit note."""
    mobile = f"900{secrets.randbelow(9000000) + 1000000}"
    client_name = "ClaudeAgentClient"
    cit_headers = {
        "X-Internal-Key": INTERNAL_KEY,
        "X-Subject": f"citizen:{mobile}",
        "X-Client": client_name
    }

    # 1. PUT draft
    r_draft = requests.put(f"{BASE_URL}/internal/agent/draft", headers=cit_headers, json={
        "applicant_name": "Agent Created Citizen",
        "dob": "10/10/1950",
        "gender": "Male",
        "marital_status": "Married",
        "husband_name": "",
        "village": "Sonari Basti",
        "block": "Sonari",
        "bank_account": "987654321098",
        "ifsc": "SBIN0003077"
    })
    assert r_draft.status_code == 200

    # 2. Upload link & upload doc
    r_link = requests.post(f"{BASE_URL}/internal/agent/upload-link", headers=cit_headers)
    assert r_link.status_code == 200
    upload_url = r_link.json()["upload_url"]

    dummy_doc = b"%PDF-1.4 dummy agent doc"
    r_up = requests.post(upload_url, files={"document": ("proof.pdf", dummy_doc, "application/pdf")})
    assert r_up.status_code == 200

    # 3. Review
    r_rev = requests.post(f"{BASE_URL}/internal/agent/review", headers=cit_headers)
    assert r_rev.status_code == 200
    review_code = r_rev.json()["review_code"]
    assert review_code

    # 4. Submit
    r_sub = requests.post(f"{BASE_URL}/internal/agent/submit", headers=cit_headers, json={"review_code": review_code})
    assert r_sub.status_code == 200
    app_no = r_sub.json()["application_no"]
    app_id = r_sub.json()["application_id"]
    assert app_no and app_id

    # 5. Check in /admin
    s_admin = requests.Session()
    s_admin.post(f"{BASE_URL}/admin", data={"username": "admin", "password": "devpass"})

    r_view = s_admin.get(f"{BASE_URL}/admin/application/{app_id}")
    assert r_view.status_code == 200
    assert app_no in r_view.text
    assert "Agent Created Citizen" in r_view.text
    # Audit note should contain client name
    assert f"via assistant: {client_name}" in r_view.text


# ---------------------------------------------------------------------------
# 4. Security: Missing / Incorrect X-Internal-Key -> 401
# ---------------------------------------------------------------------------

def test_internal_key_security():
    """All /internal/agent routes require correct X-Internal-Key."""
    routes = [
        ("GET", f"{BASE_URL}/internal/agent/scheme"),
        ("POST", f"{BASE_URL}/internal/agent/eligibility"),
        ("GET", f"{BASE_URL}/internal/agent/draft"),
        ("PUT", f"{BASE_URL}/internal/agent/draft"),
        ("POST", f"{BASE_URL}/internal/agent/upload-link"),
        ("POST", f"{BASE_URL}/internal/agent/review"),
        ("POST", f"{BASE_URL}/internal/agent/submit"),
        ("GET", f"{BASE_URL}/internal/agent/my-application"),
        ("POST", f"{BASE_URL}/internal/agent/withdraw"),
        ("GET", f"{BASE_URL}/internal/agent/queue"),
        ("GET", f"{BASE_URL}/internal/agent/case/SSP26000000"),
        ("POST", f"{BASE_URL}/internal/agent/decide"),
        ("GET", f"{BASE_URL}/internal/agent/summary")
    ]

    for method, url in routes:
        # Missing header
        r1 = requests.request(method, url)
        assert r1.status_code == 401, f"{method} {url} allowed missing internal key"

        # Wrong header
        r2 = requests.request(method, url, headers={"X-Internal-Key": "invalid-wrong-key"})
        assert r2.status_code == 401, f"{method} {url} allowed wrong internal key"


# ---------------------------------------------------------------------------
# 5. Subject Isolation: Citizen A vs Citizen B
# ---------------------------------------------------------------------------

def test_subject_isolation():
    """Citizen A cannot view or withdraw citizen B's application."""
    mob_a = f"900{secrets.randbelow(9000000) + 1000000}"
    mob_b = f"900{secrets.randbelow(9000000) + 1000000}"

    headers_a = {"X-Internal-Key": INTERNAL_KEY, "X-Subject": f"citizen:{mob_a}", "X-Client": "test"}
    headers_b = {"X-Internal-Key": INTERNAL_KEY, "X-Subject": f"citizen:{mob_b}", "X-Client": "test"}

    # Citizen A submits application
    requests.put(f"{BASE_URL}/internal/agent/draft", headers=headers_a, json={
        "applicant_name": "Citizen A",
        "dob": "01/01/1950",
        "village": "Sonari A",
        "block": "Sonari",
        "bank_account": "111122223333",
        "ifsc": "SBIN0003077"
    })
    r_link = requests.post(f"{BASE_URL}/internal/agent/upload-link", headers=headers_a)
    requests.post(r_link.json()["upload_url"], files={"document": ("proof.pdf", b"%PDF dummy", "application/pdf")})
    r_rev = requests.post(f"{BASE_URL}/internal/agent/review", headers=headers_a)
    r_sub = requests.post(f"{BASE_URL}/internal/agent/submit", headers=headers_a, json={"review_code": r_rev.json()["review_code"]})
    assert r_sub.status_code == 200
    app_no_a = r_sub.json()["application_no"]

    # Citizen B queries my-application -> should NOT see Citizen A's application
    r_b_app = requests.get(f"{BASE_URL}/internal/agent/my-application", headers=headers_b)
    assert r_b_app.status_code == 200
    assert r_b_app.json()["has_application"] is False

    # Citizen B attempts withdraw -> cannot withdraw Citizen A's application
    r_b_with = requests.post(f"{BASE_URL}/internal/agent/withdraw", headers=headers_b, json={"confirm": True})
    assert r_b_with.status_code == 400
    assert "Only a pending application can be withdrawn" in r_b_with.json()["error"]

    # Citizen A's application remains active
    r_a_app = requests.get(f"{BASE_URL}/internal/agent/my-application", headers=headers_a)
    assert r_a_app.status_code == 200
    assert r_a_app.json()["application"]["application_no"] == app_no_a
    assert r_a_app.json()["application"]["status"] == "PENDING"


# ---------------------------------------------------------------------------
# 6. Review Code Invalidation & Upload Token Single-Use
# ---------------------------------------------------------------------------

def test_review_code_and_upload_token_security():
    """Modifying draft invalidates review code; upload token cannot be reused."""
    mobile = f"900{secrets.randbelow(9000000) + 1000000}"
    headers = {"X-Internal-Key": INTERNAL_KEY, "X-Subject": f"citizen:{mobile}", "X-Client": "test"}

    requests.put(f"{BASE_URL}/internal/agent/draft", headers=headers, json={
        "applicant_name": "Tamper Test",
        "dob": "10/10/1950",
        "village": "Village",
        "block": "Sonari",
        "bank_account": "123456789012",
        "ifsc": "SBIN0003077"
    })

    # Upload doc
    r_link = requests.post(f"{BASE_URL}/internal/agent/upload-link", headers=headers)
    upload_url = r_link.json()["upload_url"]
    r_up1 = requests.post(upload_url, files={"document": ("proof.pdf", b"%PDF doc", "application/pdf")})
    assert r_up1.status_code == 200

    # Token reuse should fail
    r_up2 = requests.post(upload_url, files={"document": ("proof2.pdf", b"%PDF doc2", "application/pdf")})
    assert r_up2.status_code == 400

    # Generate review code
    r_rev = requests.post(f"{BASE_URL}/internal/agent/review", headers=headers)
    assert r_rev.status_code == 200
    code = r_rev.json()["review_code"]

    # Modify draft AFTER review code generated
    requests.put(f"{BASE_URL}/internal/agent/draft", headers=headers, json={"village": "Tampered Village"})

    # Attempt submit with earlier review code -> must fail
    r_sub = requests.post(f"{BASE_URL}/internal/agent/submit", headers=headers, json={"review_code": code})
    assert r_sub.status_code == 400
    assert "draft was modified" in r_sub.json()["errors"][0].lower() or "review" in r_sub.json()["errors"][0].lower()


# ---------------------------------------------------------------------------
# 7. Officer Decide Guards: Prior Case View & Hard Flags
# ---------------------------------------------------------------------------

def test_officer_decide_guards():
    """Decide fails without prior case view; approve fails on under-60 case; reject succeeds."""
    mob_under = f"900{secrets.randbelow(9000000) + 1000000}"

    # Insert an under-60 pending application via docker compose exec to simulate legacy/fraudulent row
    import subprocess
    app_no = f"SSP26{secrets.randbelow(900000) + 100000}"
    sql_cmd = (
        f"INSERT INTO applications "
        f"(application_no, applicant_name, mobile, dob, village, block, bank_account, ifsc, status, submitted_at, channel) "
        f"VALUES ('{app_no}', 'Underage Person', '{mob_under}', '2000-01-01', 'Sonari', 'Sonari', '999888777666', 'SBIN0003077', 'PENDING', NOW(), 'web');"
    )
    subprocess.check_call([
        "docker", "compose", "exec", "-T", "db",
        "psql", "-U", "sewasetu", "-d", "sewasetu", "-c", sql_cmd
    ])

    officer_headers = {
        "X-Internal-Key": INTERNAL_KEY,
        "X-Subject": "officer:sonari_officer",
        "X-Client": "OfficerTestClient"
    }

    # 1. Decide without prior case view -> MUST fail
    r_dec1 = requests.post(f"{BASE_URL}/internal/agent/decide", headers=officer_headers, json={
        "application_no": app_no,
        "decision": "approve",
        "reason": "Approving without inspecting case details."
    })
    assert r_dec1.status_code == 400
    assert "viewed" in r_dec1.json()["error"].lower()

    # 2. View case
    r_view = requests.get(f"{BASE_URL}/internal/agent/case/{app_no}", headers=officer_headers)
    assert r_view.status_code == 200
    case_data = r_view.json()
    assert "age_below_60_at_submission" in case_data["flags"]

    # 3. Approve under-60 case -> MUST fail due to hard flag
    r_appr = requests.post(f"{BASE_URL}/internal/agent/decide", headers=officer_headers, json={
        "application_no": app_no,
        "decision": "approve",
        "reason": "Trying to approve an underage applicant anyway."
    })
    assert r_appr.status_code == 400
    assert "hard_flags" in r_appr.json()
    assert "age_below_60_at_submission" in r_appr.json()["hard_flags"]

    # 4. Reject under-60 case -> MUST succeed
    r_rej = requests.post(f"{BASE_URL}/internal/agent/decide", headers=officer_headers, json={
        "application_no": app_no,
        "decision": "reject",
        "reason": "Rejected: applicant is only 26 years old, minimum age is 60."
    })
    assert r_rej.status_code == 200
    assert r_rej.json()["ok"] is True
    assert r_rej.json()["status"] == "REJECTED"


# ---------------------------------------------------------------------------
# 8. Session Liveness: Active vs Revoked Refresh Token
# ---------------------------------------------------------------------------

def test_session_live():
    """session-live returns True for active token, False after revocation."""
    # 1. Register client
    r = requests.post(f"{BASE_URL}/oauth/register", json={
        "client_name": "Liveness Test Client",
        "redirect_uris": ["http://localhost/callback"],
        "scope": "citizen"
    })
    client_id = r.json()["client_id"]

    # 2. Start authorize flow with PKCE
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    s = requests.Session()
    r_auth = s.get(f"{BASE_URL}/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://localhost/callback",
        "scope": "citizen",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{BASE_URL}/mcp/citizen"
    }, allow_redirects=False)

    rid = re.search(r"rid=([^&]+)", r_auth.headers["Location"]).group(1)
    test_mobile = f"900{secrets.randbelow(9000000) + 1000000}"

    s.post(f"{BASE_URL}/oauth/login?rid={rid}", data={"mobile": test_mobile})
    otp = get_sms_otp(test_mobile)
    s.post(f"{BASE_URL}/oauth/login?rid={rid}", data={"otp": otp}, allow_redirects=False)
    r_consent = s.post(f"{BASE_URL}/oauth/consent?rid={rid}", data={"decision": "allow"}, allow_redirects=False)
    code = re.search(r"code=([^&]+)", r_consent.headers["Location"]).group(1)

    r_token = requests.post(f"{BASE_URL}/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": "http://localhost/callback",
        "code_verifier": verifier
    })
    refresh_token = r_token.json()["refresh_token"]

    # Query session-live -> must be True
    r_live1 = requests.get(f"{BASE_URL}/internal/agent/session-live", params={
        "sub": f"citizen:{test_mobile}",
        "client_id": client_id,
        "aud": f"{BASE_URL}/mcp/citizen"
    }, headers=INTERNAL_HEADERS)
    assert r_live1.status_code == 200
    assert r_live1.json()["live"] is True

    # Revoke
    requests.post(f"{BASE_URL}/oauth/revoke", data={"token": refresh_token, "client_id": client_id})

    # Query session-live again -> must be False
    r_live2 = requests.get(f"{BASE_URL}/internal/agent/session-live", params={
        "sub": f"citizen:{test_mobile}",
        "client_id": client_id,
        "aud": f"{BASE_URL}/mcp/citizen"
    }, headers=INTERNAL_HEADERS)
    assert r_live2.status_code == 200
    assert r_live2.json()["live"] is False
