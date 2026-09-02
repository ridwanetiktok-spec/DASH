"""
Simple internal email mailer with click tracking + dashboard.

Usage:
    python app.py send      # sends the campaign to everyone in mails.txt
    python app.py serve     # starts the web server (click redirects, dashboard)

Setup:
    1. Copy .env.example to .env and fill in your details.
    2. Put your recipients (one email per line) in mails.txt
    3. Write your message in template.html (plain HTML, use normal <a href="..."> links)
    4. Set PUBLIC_BASE_URL in .env to http://localhost:5000 for local testing
    5. Run `python app.py send` once to send. Keep `python app.py serve` running
       so clicks can be recorded and the dashboard works.
"""

import os
import re
import csv
import sys
import uuid
import sqlite3
import smtplib
import argparse
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import unquote

from dotenv import load_dotenv
from flask import Flask, request, Response, render_template, redirect, jsonify

import mailer  # shared tracking logic

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
DB_PATH = os.path.join(BASE_DIR, "tracking.db")
TEMPLATE_PATH = os.path.join(BASE_DIR, "template.html")
MAILS_PATH = os.path.join(BASE_DIR, "mails.txt")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SENDER_NAME = os.environ.get("SENDER_NAME", SMTP_USER)
SUBJECT = os.environ.get("SUBJECT", "Update")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")

os.makedirs(RESULTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recipients (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            status TEXT NOT NULL,       -- sent | bounced
            sent_at TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id TEXT NOT NULL,
            url TEXT NOT NULL,
            clicked_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recipients_email ON recipients(email);
        CREATE INDEX IF NOT EXISTS idx_recipients_status ON recipients(status);
        CREATE INDEX IF NOT EXISTS idx_recipients_sent_at ON recipients(sent_at);
        CREATE INDEX IF NOT EXISTS idx_clicks_recipient_id ON clicks(recipient_id);
        CREATE INDEX IF NOT EXISTS idx_clicks_clicked_at ON clicks(clicked_at);
        """
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Campaign sending
# ---------------------------------------------------------------------------

def load_recipients():
    if not os.path.exists(MAILS_PATH):
        print(f"Missing {MAILS_PATH}")
        sys.exit(1)
    with open(MAILS_PATH, "r", encoding="utf-8") as f:
        emails = [line.strip() for line in f if line.strip()]
    return emails

def load_template():
    if not os.path.exists(TEMPLATE_PATH):
        print(f"Missing {TEMPLATE_PATH}")
        sys.exit(1)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()

def send_one(smtp_conn, to_email: str, html_body: str):
    """Send email with both HTML and plain text versions."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = SUBJECT
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_email

    # Create plain text version (strip HTML tags)
    plain_text = re.sub(r'<[^>]+>', '', html_body)
    plain_text = re.sub(r'\s+', ' ', plain_text).strip()

    # Attach both versions
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    smtp_conn.sendmail(SMTP_USER, [to_email], msg.as_string())

def run_campaign():
    if not SMTP_USER or not SMTP_PASSWORD:
        print("Set SMTP_USER and SMTP_PASSWORD in your .env file first.")
        sys.exit(1)

    init_db()
    recipients = load_recipients()
    raw_html = load_template()

    sent_list, bounced_list = [], []

    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} as {SMTP_USER} ...")

    # Try SSL first, fallback to STARTTLS
    try:
        smtp_conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        smtp_conn.login(SMTP_USER, SMTP_PASSWORD)
        print("Connected via SSL")
    except Exception as e:
        print(f"SSL failed: {e}, trying STARTTLS...")
        smtp_conn = smtplib.SMTP(SMTP_HOST, 587, timeout=30)
        smtp_conn.starttls()
        smtp_conn.login(SMTP_USER, SMTP_PASSWORD)
        print("Connected via STARTTLS")

    conn = get_db()
    for email in recipients:
        recipient_id = uuid.uuid4().hex
        html_body = mailer.build_tracked_html(
            template_content,
            campaign_name="microsoft_offer_2026"
        )
        now = datetime.utcnow().isoformat()
        try:
            send_one(smtp_conn, email, html_body)
            conn.execute(
                "INSERT INTO recipients (id, email, status, sent_at, error) VALUES (?, ?, 'sent', ?, NULL)",
                (recipient_id, email, now),
            )
            sent_list.append(email)
            print(f"  sent -> {email}")
        except smtplib.SMTPException as e:
            conn.execute(
                "INSERT INTO recipients (id, email, status, sent_at, error) VALUES (?, ?, 'bounced', ?, ?)",
                (recipient_id, email, now, str(e)),
            )
            bounced_list.append(email)
            print(f"  BOUNCED -> {email} ({e})")
    conn.commit()
    conn.close()
    smtp_conn.quit()

    with open(os.path.join(RESULTS_DIR, "sent.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sent_list))
    with open(os.path.join(RESULTS_DIR, "delivred.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sent_list))
    with open(os.path.join(RESULTS_DIR, "bounced.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(bounced_list))

    print(f"\nDone. Sent: {len(sent_list)}  Bounced: {len(bounced_list)}")
    print(f"Results written to {RESULTS_DIR}/")
    print(f"View dashboard at {PUBLIC_BASE_URL}/dashboard")

# ---------------------------------------------------------------------------
# Web server: tracking + dashboard
# ---------------------------------------------------------------------------

app = Flask(__name__)

init_db()

@app.route("/api/register_recipient", methods=["POST"])
def register_recipient():
    """API endpoint for local sender to register recipients."""
    try:
        data = request.get_json(force=True)
        if not data or "id" not in data or "email" not in data:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO recipients (id, email, status, sent_at, error) VALUES (?, ?, ?, ?, ?)",
            (data["id"], data["email"], data["status"], data["sent_at"], data.get("error", ""))
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/health")
def health():
    return "OK"

@app.route("/track/click/<recipient_id>")
def track_click(recipient_id):
    target_url = unquote(request.args.get("url", PUBLIC_BASE_URL))
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO clicks (recipient_id, url, clicked_at) VALUES (?, ?, ?)",
            (recipient_id, target_url, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        print(f"🖱️ Click tracked: {recipient_id} -> {target_url}")
    except Exception as e:
        print(f"Error tracking click: {e}")
    return redirect(target_url, code=302)

@app.route("/dashboard")
def dashboard():
    conn = get_db()
    recipients = conn.execute("SELECT * FROM recipients ORDER BY sent_at DESC").fetchall()
    clicks = conn.execute(
        "SELECT recipient_id, COUNT(*) as c FROM clicks GROUP BY recipient_id"
    ).fetchall()
    conn.close()

    clicks_map = {row["recipient_id"]: row["c"] for row in clicks}

    rows = []
    for r in recipients:
        rows.append(
            {
                "email": r["email"],
                "status": r["status"],
                "sent_at": r["sent_at"],
                "clicks": clicks_map.get(r["id"], 0),
                "error": r["error"] or "",
            }
        )

    totals = {
        "sent": sum(1 for r in rows if r["status"] == "sent"),
        "bounced": sum(1 for r in rows if r["status"] == "bounced"),
        "clicked": sum(1 for r in rows if r["clicks"] > 0),
        "total_recipients": len(rows),
        "ctr": round((sum(1 for r in rows if r["clicks"] > 0) / max(1, sum(1 for r in rows if r["status"] == "sent")) * 100), 2),
        "bounce_rate": round((sum(1 for r in rows if r["status"] == "bounced") / max(1, len(rows)) * 100), 2),
        "delivery_rate": round((sum(1 for r in rows if r["status"] == "sent") / max(1, len(rows)) * 100), 2)
    }

    # Get time series for last 30 days
    time_series = []
    conn = get_db()
    for i in range(30):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        sent = conn.execute(
            "SELECT COUNT(*) FROM recipients WHERE status='sent' AND DATE(sent_at) = ?",
            (day,)
        ).fetchone()[0]
        bounced = conn.execute(
            "SELECT COUNT(*) FROM recipients WHERE status='bounced' AND DATE(sent_at) = ?",
            (day,)
        ).fetchone()[0]
        if sent > 0 or bounced > 0:
            time_series.append({"date": day, "sent": sent, "bounced": bounced})
    conn.close()

    return render_template("dashboard.html", rows=rows, totals=totals, time_series=time_series[:30])

@app.route("/api/export")
def export_csv():
    conn = get_db()
    recipients = conn.execute("SELECT * FROM recipients ORDER BY sent_at DESC").fetchall()
    clicks = conn.execute("SELECT recipient_id, COUNT(*) as c FROM clicks GROUP BY recipient_id").fetchall()
    conn.close()

    clicks_map = {r["recipient_id"]: r["c"] for r in clicks}

    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "status", "sent_at", "clicks", "error"])
    for r in recipients:
        writer.writerow([
            r["email"], r["status"], r["sent_at"],
            clicks_map.get(r["id"], 0),
            r["error"] or ""
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=campaign.csv"}
    )

@app.route("/api/alltime")
def alltime_stats():
    """Get all-time statistics."""
    conn = get_db()
    total_recipients = conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
    total_sent = conn.execute("SELECT COUNT(*) FROM recipients WHERE status='sent'").fetchone()[0]
    total_bounced = conn.execute("SELECT COUNT(*) FROM recipients WHERE status='bounced'").fetchone()[0]
    total_clicks = conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
    unique_clicked = conn.execute("SELECT COUNT(DISTINCT recipient_id) FROM clicks").fetchone()[0]
    conn.close()

    return jsonify({
        "total_recipients": total_recipients,
        "total_sent": total_sent,
        "total_bounced": total_bounced,
        "total_clicks": total_clicks,
        "unique_clicked": unique_clicked,
        "ctr": round((unique_clicked / max(1, total_sent) * 100), 2),
        "bounce_rate": round((total_bounced / max(1, total_recipients) * 100), 2),
        "delivery_rate": round((total_sent / max(1, total_recipients) * 100), 2)
    })

@app.route("/api/reset")
def reset_db():
    """⚠️ DANGER: Delete all data."""
    try:
        conn = get_db()
        conn.execute("DELETE FROM clicks")
        conn.execute("DELETE FROM recipients")
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "Database reset successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/")
def index():
    return redirect("/dashboard")

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["send", "serve"], help="'send' the campaign or 'serve' the dashboard/tracking server")
    args = parser.parse_args()

    if args.mode == "send":
        run_campaign()
    else:
        init_db()
        port = int(os.environ.get("PORT", 5000))
        print(f"📊 Serving dashboard at {PUBLIC_BASE_URL}/dashboard")
        print(f"🖱️ Click tracking at {PUBLIC_BASE_URL}/track/click/<id>")
        print(f"📨 Send emails with: python app.py send")
        print("=" * 50)
        app.run(host="0.0.0.0", port=port, debug=True)
