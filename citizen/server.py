# -*- coding: utf-8 -*-
"""
Sewa Setu Citizen MCP Server.
Provides 8 citizen tools for applying for the Old Age Pension Scheme.
Exposes MCP endpoint at /mcp/citizen and health check at /healthz on port 9001.
"""

import os
import sys
import re
from typing import Literal, Optional

# Ensure parent directory is in path for common imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.mcpserver import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations

from common.auth import create_mcp_auth_app
from common.portal_client import PortalClient
from common.rate_limit import RateLimiter
from common.response import tool_response, error_response

PORTAL_INTERNAL_URL = os.environ.get("PORTAL_INTERNAL_URL", "http://app:8000")
AGENT_INTERNAL_KEY = os.environ.get("AGENT_INTERNAL_KEY", "dev-agent-key-sewasetu-2026")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080")
ROLE = "citizen"

portal_client = PortalClient(PORTAL_INTERNAL_URL, AGENT_INTERNAL_KEY)
rate_limiter = RateLimiter(max_calls=60, window_seconds=600.0)

SERVER_INSTRUCTIONS = (
    "You are an empathetic, polite, and helpful assistant helping senior citizens of "
    "Purvanchal apply for and track their Old Age Pension on the Sewa Setu portal. "
    "Speak in short, plain sentences suitable to read aloud to an elderly person. "
    "Always check scheme facts via scheme_info. Never guess, assume, or invent details. "
    "Never ask for or accept OTPs, passwords, or status portal credentials in chat. "
    "Always read the full application summary aloud to the citizen and get their explicit agreement "
    "before calling submit_application."
)

server = MCPServer(
    name="sewasetu-citizen",
    instructions=SERVER_INSTRUCTIONS,
    version="1.0.0",
)


def _check_auth_and_rate_limit():
    token = get_access_token()
    if not token or not token.subject:
        return None, None, error_response(
            "Authentication required. Please sign in to continue.",
            next_step="Sign in with your mobile number via the portal.",
        )
    allowed, rate_msg = rate_limiter.is_allowed(token.subject)
    if not allowed:
        return token.subject, token.client_id, error_response(
            rate_msg,
            next_step="Please wait a few minutes before trying again.",
            rate_limited=True,
        )
    return token.subject, token.client_id, None


# ---------------------------------------------------------------------------
# Tool 1: scheme_info
# ---------------------------------------------------------------------------
@server.tool(
    name="scheme_info",
    description=(
        "Always call this instead of relying on memory; scheme facts change. "
        "Retrieve the official rules, eligibility criteria, pension amount, statutory deadlines, "
        "and currently enabled blocks for the Sewa Setu Old Age Pension Scheme. "
        "Use this at the beginning of conversations or whenever answering questions about the scheme. "
        "Do not invent or assume scheme rules."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def scheme_info() -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    status, data = await portal_client.call(
        "GET", "scheme", subject=subject, client_id=client_id
    )
    if status != 200:
        return error_response(
            "Unable to load scheme details right now.",
            next_step="Please try again in a moment.",
        )

    amt = data.get("amount", 250)
    blocks_str = ", ".join(data.get("enabled_blocks", []))
    msg = (
        f"The Sewa Setu Old Age Pension provides ₹{amt} every month to senior citizens "
        f"aged 60 and above living in {blocks_str}."
    )
    next_step = "Ask the citizen for their date of birth to check if they qualify."
    return tool_response(
        message=msg,
        next_step=next_step,
        amount_inr=amt,
        min_age=data.get("min_age", 60),
        sla_days=data.get("sla_days", 15),
        deadline=data.get("deadline_ist", ""),
        enabled_blocks=data.get("enabled_blocks", []),
        all_blocks=data.get("all_blocks", []),
    )


# ---------------------------------------------------------------------------
# Tool 2: check_eligibility
# ---------------------------------------------------------------------------
@server.tool(
    name="check_eligibility",
    description=(
        "Check if a citizen is eligible based on their date of birth and block. "
        "Use this before starting an application. Date of birth must be DD/MM/YYYY. "
        "Never guess or assume a date of birth or block. Tell the person clearly whether they qualify."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def check_eligibility(
    dob: str,
    block: Optional[Literal["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]] = None,
) -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    dob_clean = (dob or "").strip()[:10]
    block_clean = (block or "").strip()[:50] if block else None

    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", dob_clean):
        return error_response(
            "Please provide your date of birth in DD/MM/YYYY format, such as 15/08/1955.",
            next_step="Provide your date of birth as day, month, and year.",
        )

    payload = {"dob": dob_clean}
    if block_clean:
        payload["block"] = block_clean

    status, data = await portal_client.call(
        "POST", "eligibility", subject=subject, client_id=client_id, json_data=payload
    )

    if status != 200 or not data.get("eligible", False):
        reasons = data.get("reasons", ["Applicant does not meet the eligibility criteria."])
        reason_text = " ".join(reasons)
        b60 = data.get("date_of_60th_birthday")
        next_instruction = (
            f"You will be eligible on {b60}." if b60 else "Check if details were typed correctly."
        )
        return error_response(
            message=reason_text,
            next_step=next_instruction,
            eligible=False,
            age=data.get("age"),
            date_of_60th_birthday=b60,
        )

    msg = f"Good news! At age {data.get('age')}, you are eligible for the Old Age Pension Scheme."
    next_step = "Ask the citizen for their full name and village to start saving their draft."
    return tool_response(
        message=msg,
        next_step=next_step,
        eligible=True,
        age=data.get("age"),
    )


# ---------------------------------------------------------------------------
# Tool 3: save_draft
# ---------------------------------------------------------------------------
@server.tool(
    name="save_draft",
    description=(
        "Save or update the citizen's application draft details. "
        "You may call this tool multiple times as details are gathered. "
        "Never tell an assistant to guess a value, invent a bank detail, or pick a block for the person. "
        "All details must come directly from the citizen."
    ),
    annotations=ToolAnnotations(read_only_hint=False),
)
async def save_draft(
    applicant_name: Optional[str] = None,
    dob: Optional[str] = None,
    gender: Optional[Literal["Male", "Female", "Other"]] = None,
    marital_status: Optional[Literal["Married", "Unmarried", "Widowed"]] = None,
    husband_name: Optional[str] = None,
    village: Optional[str] = None,
    block: Optional[Literal["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]] = None,
    bank_account: Optional[str] = None,
    ifsc: Optional[str] = None,
) -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    incoming = {}
    if applicant_name is not None:
        incoming["applicant_name"] = str(applicant_name).strip()[:100]
    if dob is not None:
        incoming["dob"] = str(dob).strip()[:10]
    if gender is not None:
        incoming["gender"] = str(gender).strip()[:10]
    if marital_status is not None:
        incoming["marital_status"] = str(marital_status).strip()[:20]
    if husband_name is not None:
        incoming["husband_name"] = str(husband_name).strip()[:100]
    if village is not None:
        incoming["village"] = str(village).strip()[:100]
    if block is not None:
        incoming["block"] = str(block).strip()[:50]
    if bank_account is not None:
        incoming["bank_account"] = re.sub(r"[\s-]", "", str(bank_account))[:18]
    if ifsc is not None:
        incoming["ifsc"] = re.sub(r"[\s-]", "", str(ifsc)).upper()[:11]

    status, data = await portal_client.call(
        "PUT", "draft", subject=subject, client_id=client_id, json_data=incoming
    )

    if status != 200 or not data.get("ok"):
        err_msg = data.get("error") or "Could not save your draft."
        return error_response(err_msg, next_step="Review the entered details and try again.")

    missing = data.get("missing", [])
    has_doc = data.get("has_doc", False)
    errors = data.get("errors_so_far", [])

    if missing:
        field_names = {
            "applicant_name": "full name",
            "dob": "date of birth (DD/MM/YYYY)",
            "village": "village name",
            "block": "block name",
            "bank_account": "bank account number",
            "age_proof_document": "age-proof document upload",
        }
        needed = [field_names.get(m, m) for m in missing]
        msg = f"Your details so far have been saved. We still need your {', '.join(needed)}."
        if not has_doc and "age_proof_document" in missing:
            next_step = "Call get_upload_link to give the citizen their age-proof upload link."
        else:
            next_step = f"Ask the citizen for their {needed[0]}."
    else:
        msg = "All required details and documents are in place."
        next_step = "Call review_application to generate the review code and review with the citizen."

    return tool_response(
        message=msg,
        next_step=next_step,
        missing=missing,
        has_doc=has_doc,
        errors_so_far=errors,
    )


# ---------------------------------------------------------------------------
# Tool 4: get_upload_link
# ---------------------------------------------------------------------------
@server.tool(
    name="get_upload_link",
    description=(
        "Give this link to the citizen to tap and upload the age-proof photo or PDF. "
        "Do not ask them to send the document in chat. "
        "Generates a secure 30-minute upload link that attaches the document to the current draft."
    ),
    annotations=ToolAnnotations(read_only_hint=False),
)
async def get_upload_link() -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    status, data = await portal_client.call(
        "POST", "upload-link", subject=subject, client_id=client_id
    )

    if status != 200 or not data.get("ok"):
        return error_response(
            "Could not create an upload link at this moment.",
            next_step="Please try asking for the upload link again.",
        )

    upload_url = data.get("upload_url", "")
    msg = (
        f"Please tap this secure link to upload your age-proof photo or PDF: {upload_url}"
    )
    next_step = (
        "Ask the citizen to tap the link on their mobile, choose a photo or document, "
        "and confirm when upload is complete."
    )
    return tool_response(
        message=msg,
        next_step=next_step,
        upload_url=upload_url,
        expires_in_minutes=data.get("expires_in_minutes", 30),
        has_existing_document=data.get("has_existing_document", False),
    )


# ---------------------------------------------------------------------------
# Tool 5: review_application
# ---------------------------------------------------------------------------
@server.tool(
    name="review_application",
    description=(
        "Read the summary aloud to the citizen and get an explicit yes BEFORE calling submit_application. "
        "Validates the complete draft and returns a 10-minute review code. "
        "Any change to the draft after this will invalidate the code and require reviewing again."
    ),
    annotations=ToolAnnotations(read_only_hint=False),
)
async def review_application() -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    status, data = await portal_client.call(
        "POST", "review", subject=subject, client_id=client_id
    )

    if status != 200 or not data.get("ok"):
        errs = data.get("errors", ["Application draft is incomplete."])
        return error_response(
            "Your application is not ready for review yet: " + " ".join(errs),
            next_step="Update missing fields using save_draft or upload your document first.",
            errors=errs,
        )

    summary = data.get("summary", {})
    rev_code = data.get("review_code")
    name = summary.get("applicant_name", "")
    dob = summary.get("dob", "")
    village = summary.get("village", "")
    block = summary.get("block", "")
    bank_masked = summary.get("bank_account_masked", "")
    ifsc = summary.get("ifsc", "")

    msg = (
        f"Please confirm: Name {name}, Date of Birth {dob}, Village {village}, "
        f"Block {block}, Bank Account {bank_masked}, IFSC {ifsc}."
    )
    next_step = (
        "Read this summary aloud to the citizen and confirm their explicit yes "
        "before calling submit_application with the review code."
    )
    return tool_response(
        message=msg,
        next_step=next_step,
        review_code=rev_code,
        summary=summary,
        expires_in_minutes=data.get("expires_in_minutes", 10),
    )


# ---------------------------------------------------------------------------
# Tool 6: submit_application
# ---------------------------------------------------------------------------
@server.tool(
    name="submit_application",
    description=(
        "Submit the validated application to the government. "
        "ONLY call this after the citizen has confirmed the reviewed summary in review_application. "
        "Requires the 10-minute review_code. Non-idempotent: registers a real pension application."
    ),
    annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False),
)
async def submit_application(review_code: str) -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    code_clean = (review_code or "").strip()[:20]
    if not code_clean:
        return error_response(
            "A review code is required to submit. Please review your application first.",
            next_step="Call review_application to review your details and obtain a review code.",
        )

    status, data = await portal_client.call(
        "POST", "submit", subject=subject, client_id=client_id, json_data={"review_code": code_clean}
    )

    if status != 200 or not data.get("ok"):
        errs = data.get("errors") or [data.get("error", "Application submission failed.")]
        err_msg = " ".join(errs)
        return error_response(
            err_msg,
            next_step="Call review_application again to generate a fresh review code and retry.",
            errors=errs,
        )

    app_no = data.get("application_no", "")
    msg = (
        f"Congratulations! Your application has been submitted successfully. "
        f"Your application number is {app_no}."
    )
    next_step = "Keep your application number safe. You can check status anytime using get_my_application."
    return tool_response(
        message=msg,
        next_step=next_step,
        application_no=app_no,
        application_id=data.get("application_id"),
    )


# ---------------------------------------------------------------------------
# Tool 7: get_my_application
# ---------------------------------------------------------------------------
@server.tool(
    name="get_my_application",
    description=(
        "Check the current status, decision, or deemed-approval countdown of your pension application. "
        "Use this whenever a citizen asks about their submitted application."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_my_application() -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    status, data = await portal_client.call(
        "GET", "my-application", subject=subject, client_id=client_id
    )

    if status != 200 or not data.get("has_application", False):
        return tool_response(
            message="You do not currently have any active submitted application on file.",
            next_step="You may begin a new application anytime using check_eligibility and save_draft.",
            has_application=False,
            application=None,
        )

    app = data.get("application", {})
    app_no = app.get("application_no", "")
    app_status = app.get("status", "PENDING")
    days_left = app.get("days_until_deemed_approval")
    officer_reason = app.get("officer_reason")

    if app_status == "PENDING":
        msg = f"Your application {app_no} is currently under verification by the block development office."
        if days_left is not None:
            msg += f" Under the statutory 15-day SLA, {days_left} day(s) remain before automatic approval."
        next_step = "No action needed from you right now. Please check back later."
    elif app_status == "APPROVED":
        msg = f"Your application {app_no} has been APPROVED! Monthly pension will be credited to your bank account."
        next_step = "Your pension is active. Contact your bank or block office for disbursement schedule."
    elif app_status == "DEEMED_APPROVED":
        msg = f"Your application {app_no} is DEEMED APPROVED under the statutory 15-day SLA guarantee."
        next_step = "Your pension is active under the Purvanchal Right to Public Services Act."
    elif app_status == "REJECTED":
        reason_info = f" Reason: {officer_reason}." if officer_reason else ""
        msg = f"Your application {app_no} was not approved.{reason_info}"
        next_step = "You may review the officer's reason, update your documents, and apply again."
    elif app_status == "WITHDRAWN":
        msg = f"Your application {app_no} was withdrawn."
        next_step = "You may submit a fresh application whenever you wish."
    else:
        msg = f"Application {app_no} status: {app_status}."
        next_step = "Check back for updates."

    return tool_response(
        message=msg,
        next_step=next_step,
        has_application=True,
        application=app,
    )


# ---------------------------------------------------------------------------
# Tool 8: withdraw_application
# ---------------------------------------------------------------------------
@server.tool(
    name="withdraw_application",
    description=(
        "Withdraw a pending application. Only after the citizen clearly confirms in their own words. "
        "Never withdraw an application without explicit confirmation. Once withdrawn, a fresh application can be made."
    ),
    annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False),
)
async def withdraw_application(application_no: str, confirm: bool) -> dict:
    subject, client_id, err = _check_auth_and_rate_limit()
    if err:
        return err

    app_no_clean = (application_no or "").strip()[:20]
    if not confirm:
        return error_response(
            "Withdrawal was cancelled because explicit confirmation was not given.",
            next_step="Ask the citizen if they are certain they want to withdraw their application.",
        )

    status, data = await portal_client.call(
        "POST", "withdraw", subject=subject, client_id=client_id, json_data={"confirm": True}
    )

    if status != 200 or not data.get("ok"):
        err_msg = data.get("error", "Only pending applications can be withdrawn.")
        return error_response(
            err_msg,
            next_step="Check your application status with get_my_application.",
        )

    app_no = data.get("application_no", app_no_clean)
    msg = f"Application {app_no} has been successfully withdrawn."
    next_step = "You are free to submit a fresh application whenever you are ready."
    return tool_response(
        message=msg,
        next_step=next_step,
        application_no=app_no,
        withdrawn=True,
    )


# Assemble ASGI Starlette application with OAuth Resource Server middleware
app = create_mcp_auth_app(
    server=server,
    role=ROLE,
    portal_internal_url=PORTAL_INTERNAL_URL,
    public_base_url=PUBLIC_BASE_URL,
    agent_internal_key=AGENT_INTERNAL_KEY,
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9001")))
