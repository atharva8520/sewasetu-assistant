# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import random
import hashlib
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import sys
import json
import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   render_template_string, flash, send_file, abort, jsonify)
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or config.get("app", "secret_key")
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST") or config.get("database", "host"),
        port=os.environ.get("POSTGRES_PORT") or config.get("database", "port"),
        dbname=os.environ.get("POSTGRES_DB") or config.get("database", "name"),
        user=os.environ.get("POSTGRES_USER") or config.get("database", "user"),
        password=os.environ.get("POSTGRES_PASSWORD") or config.get("database", "password"),
    )


def run_startup_migrations():
    """Apply startup schema updates idempotently. Log loudly on failure."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS channel VARCHAR(20) DEFAULT 'web';")
        cur.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS channel_client VARCHAR(100);")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_drafts (
                mobile VARCHAR(15) PRIMARY KEY,
                data JSONB,
                doc_path VARCHAR(200),
                updated_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_upload_tokens (
                token_hash VARCHAR(64) PRIMARY KEY,
                mobile VARCHAR(15),
                expires_at TIMESTAMP,
                used_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_reviews (
                mobile VARCHAR(15) PRIMARY KEY,
                review_hash VARCHAR(64),
                review_code_hash VARCHAR(64),
                created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS agent_case_views (
                id SERIAL PRIMARY KEY,
                officer VARCHAR(100),
                application_id INTEGER,
                viewed_at TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        app.logger.info("Startup migrations completed successfully.")
    except Exception as e:
        app.logger.error("Startup migration failed: %s" % e, exc_info=True)
        print("CRITICAL: Startup migration failed: %s" % e, file=sys.stderr)


run_startup_migrations()


def sanitize(value, maxlen=100):
    # Trim whitespace and cap at the column width, counting characters (not
    # bytes) so Assamese/Bangla names are stored intact.
    if value is None:
        return ""
    return value.strip()[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, note, datetime.now()))


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    errors = []
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": (data.get("village") or "").strip(),
        "block": (data.get("block") or "").strip(),
        "gender": data.get("gender") or "",
        "marital_status": data.get("marital_status") or "",
        "husband_name": (data.get("husband_name") or "").strip(),
        "husband_employer": "",  # no longer collected (not a scheme requirement)
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or ""),
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or "",
        "dob": None,
    }
    for label, key in (("full name", "applicant_name"), ("village", "village"),
                       ("block", "block"), ("bank account", "bank_account")):
        if not cleaned[key]:
            errors.append("%s is required" % label)
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
    if cleaned["bank_account"] and not re.fullmatch(r"\d{9,18}", cleaned["bank_account"]):
        errors.append("bank account number must be 9 to 18 digits")
    if cleaned["ifsc"] and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        if age_on(dob, now_ist().date()) < MIN_AGE:
            errors.append("applicant must be %d years of age or above" % MIN_AGE)
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s "
                "ORDER BY submitted_at DESC LIMIT 1", (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


def create_application(cur, mobile, cleaned, channel="web", client_name=None):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    app_no = new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at, channel, channel_client)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s)
           RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], datetime.now(), channel, client_name))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    audit_note = ("via assistant: %s" % client_name) if (channel == "agent" and client_name) else ("via assistant" if channel == "agent" else "")
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile, audit_note)
    return new_id, app_no


def submit_application_for(mobile, data, doc_path, channel="web", client_name=None):
    """Unified application submission core used by web and agent channels.
    validate_application -> active_application check -> create_application -> commit -> SMS.
    Returns: {ok, errors[], existing:{application_no,status}|None, application_no, application_id}
    """
    errors, cleaned = validate_application(data, doc_path)
    if errors:
        return {
            "ok": False,
            "errors": errors,
            "existing": None,
            "application_no": None,
            "application_id": None,
        }

    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    if existing:
        cur.close()
        conn.close()
        return {
            "ok": False,
            "errors": [
                "Application %s (%s) already exists for this mobile number. A pending "
                "application can be withdrawn from the status portal if you need to "
                "apply afresh." % (existing[1], existing[2])
            ],
            "existing": {"application_no": existing[1], "status": existing[2]},
            "application_no": None,
            "application_id": None,
        }

    new_id, app_no = create_application(cur, mobile, cleaned, channel=channel, client_name=client_name)
    conn.commit()
    cur.close()
    conn.close()

    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)

    return {
        "ok": True,
        "errors": [],
        "existing": None,
        "application_no": app_no,
        "application_id": new_id,
    }


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            return render_template("upload.html")
        filename = f.filename.lower()
        content = f.read()
        if not filename.rsplit(".", 1)[-1] in ("pdf", "jpg", "jpeg", "png"):
            flash("Please upload a JPG, PNG or PDF file.")
            return render_template("upload.html")
        if len(content) > 5 * 1024 * 1024:
            flash("File is larger than 5 MB. Please upload a smaller file.")
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "%s_%s" % (session["verified_mobile"], filename))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")
    res = submit_application_for(mobile, data, doc_path, channel="web", client_name=None)
    if not res["ok"]:
        if res["existing"]:
            flash(res["errors"][0])
            return redirect(url_for("index"))
        flash("Please correct the following before submitting: " + "; ".join(res["errors"]) + ".")
        return redirect(url_for("form_step", step=1))

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    return render_template("confirmation.html", app_no=res["application_no"], app_id=res["application_id"])


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may submit a new application." % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s ORDER BY submitted_at ASC",
                    (mobile,))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit)


def decide_application(app_id, new_status, actor, note=""):
    """Approve or reject a PENDING application, recording who decided and when.

    Returns dict: {"ok": bool, "application_no": str | None, "status": str, "message": str}
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
        "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
        (new_status, datetime.now(), actor, app_id),
    )
    row = cur.fetchone()
    if row:
        audit_actor = actor if ":" in str(actor) else ("admin:%s" % actor)
        write_audit(cur, app_id, new_status, audit_actor, note)
        conn.commit()
        cur.close()
        conn.close()
        return {
            "ok": True,
            "application_no": row[0],
            "status": new_status,
            "message": "Application %s %s." % (row[0], new_status.lower()),
        }
    else:
        cur.execute("SELECT application_no, status FROM applications WHERE id = %s", (app_id,))
        curr = cur.fetchone()
        cur.close()
        conn.close()
        if curr:
            return {
                "ok": False,
                "application_no": curr[0],
                "status": curr[1],
                "message": "Only a pending application can be decided.",
            }
        return {
            "ok": False,
            "application_no": None,
            "status": None,
            "message": "Application not found.",
        }


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    res = decide_application(app_id, new_status, actor, note)
    if res["ok"]:
        flash(res["message"])
    else:
        flash(res["message"])
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


# ---------------------------------------------------------------------------
# Agent channel public upload routes
# ---------------------------------------------------------------------------

@app.route("/agent/upload/<token>", methods=["GET", "POST"])
def agent_upload(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT mobile, expires_at, used_at FROM agent_upload_tokens WHERE token_hash = %s",
        (token_hash,)
    )
    row = cur.fetchone()

    template_upload = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sewa Setu - Age Proof Upload</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #f0f2f5; color: #222; }
.header { background: #0b3d6e; color: #fff; padding: 16px 24px; }
.header h1 { margin: 0; font-size: 20px; }
.card { max-width: 480px; margin: 24px auto; background: #fff; padding: 24px; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
.title { font-size: 20px; font-weight: bold; margin-bottom: 8px; color: #0b3d6e; }
.desc { font-size: 15px; color: #555; line-height: 1.5; margin-bottom: 20px; }
.file-box { border: 2px dashed #0b3d6e; border-radius: 8px; padding: 24px; text-align: center; background: #f8fafc; margin-bottom: 20px; }
.file-box input[type=file] { font-size: 16px; width: 100%; }
.btn { display: block; width: 100%; background: #0b3d6e; color: #fff; border: none; padding: 14px; font-size: 17px; font-weight: bold; border-radius: 6px; cursor: pointer; box-sizing: border-box; }
.btn:hover { background: #092c4f; }
.err-box { background: #fde8e8; border-left: 4px solid #e02424; padding: 14px; border-radius: 4px; color: #9b1c1c; font-size: 15px; margin-bottom: 16px; }
.ok-box { background: #def7ec; border-left: 4px solid #0e9f6e; padding: 16px; border-radius: 4px; color: #03543f; font-size: 16px; line-height: 1.5; }
</style>
</head>
<body>
<div class="header"><h1>Sewa Setu &mdash; Document Upload</h1></div>
<div class="card">
{% if error %}
  <div class="err-box">{{ error }}</div>
{% endif %}
{% if success %}
  <div class="ok-box">
    <strong>Upload successful!</strong><br>
    Your age proof document has been attached to your application draft. You can now return to your assistant.
  </div>
{% else %}
  <div class="title">Upload Age Proof Document</div>
  <div class="desc">Please upload a clear photo or PDF of your age proof (JPG, PNG, or PDF up to 5 MB).</div>
  <form method="post" enctype="multipart/form-data">
    <div class="file-box">
      <input type="file" name="document" accept=".pdf,.jpg,.jpeg,.png" required>
    </div>
    <button type="submit" class="btn">Upload Document</button>
  </form>
{% endif %}
</div>
</body>
</html>"""

    if not row:
        cur.close(); conn.close()
        return render_template_string(template_upload, error="This upload link is invalid or does not exist. Please ask your assistant for a new link.", success=False), 400

    mobile, expires_at, used_at = row
    if used_at is not None:
        cur.close(); conn.close()
        return render_template_string(template_upload, error="This upload link has already been used. Please ask your assistant for a new link if you need to upload again.", success=False), 400

    if datetime.now() > expires_at:
        cur.close(); conn.close()
        return render_template_string(template_upload, error="This upload link has expired (links are valid for 30 minutes). Please ask your assistant for a fresh link.", success=False), 400

    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            cur.close(); conn.close()
            return render_template_string(template_upload, error="Please select a file to upload.", success=False), 400

        orig_filename = f.filename.lower()
        ext = orig_filename.rsplit(".", 1)[-1] if "." in orig_filename else ""
        if ext not in ("pdf", "jpg", "jpeg", "png"):
            cur.close(); conn.close()
            return render_template_string(template_upload, error="Only JPG, JPEG, PNG, or PDF files are accepted.", success=False), 400

        content = f.read()
        if len(content) > 5 * 1024 * 1024:
            cur.close(); conn.close()
            return render_template_string(template_upload, error="The file size exceeds the 5 MB limit. Please choose a smaller file.", success=False), 400

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        safe_name = secure_filename(f.filename) or ("proof." + ext)
        save_filename = "%s_%s" % (mobile, safe_name)
        save_path = os.path.join(UPLOAD_DIR, save_filename)
        with open(save_path, "wb") as fh:
            fh.write(content)

        # Mark token used
        cur.execute(
            "UPDATE agent_upload_tokens SET used_at = %s WHERE token_hash = %s",
            (datetime.now(), token_hash)
        )
        # Update agent_drafts
        cur.execute(
            """INSERT INTO agent_drafts (mobile, data, doc_path, updated_at)
               VALUES (%s, '{}', %s, %s)
               ON CONFLICT (mobile) DO UPDATE
               SET doc_path = EXCLUDED.doc_path, updated_at = EXCLUDED.updated_at""",
            (mobile, save_path, datetime.now())
        )
        conn.commit()
        cur.close(); conn.close()
        return render_template_string(template_upload, error=None, success=True)

    cur.close(); conn.close()
    return render_template_string(template_upload, error=None, success=False)


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)

from agent_api import agent_api as _agent_api_blueprint   # noqa: E402
app.register_blueprint(_agent_api_blueprint, url_prefix="/internal/agent")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)

