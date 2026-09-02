"""Email campaign tracking dashboard for Vercel and Vercel Postgres."""

import argparse
import csv
import io
import os
import re
import smtplib
import sys
import uuid
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import unquote

import psycopg2
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request
from psycopg2.extras import RealDictCursor

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent if BASE_DIR.name == "api" else BASE_DIR
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import mailer

load_dotenv()

RESULTS_DIR = PROJECT_DIR / "results"
TEMPLATE_PATH = PROJECT_DIR / "template.html"
MAILS_PATH = PROJECT_DIR / "mails.txt"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SENDER_NAME = os.environ.get("SENDER_NAME", SMTP_USER)
SUBJECT = os.environ.get("SUBJECT", "Update")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")

app = Flask(__name__, template_folder=str(PROJECT_DIR / "templates"))
application = app
database_initialized = False

TRANSPARENT_PIXEL = (
    b"GIF89a" b"\x01\x00\x01\x00" b"\x80\x00\x00" b"\x00\x00\x00"
    b"\xff\xff\xff" b"!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00" b"\x02\x02D\x01\x00;"
)


def get_db():
    db_url = (
        os.environ.get("POSTGRES_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("dash_storage_DATABASE_URL")
        or os.environ.get("dash_storage_POSTGRES_URL")
    )

    if not db_url:
        raise RuntimeError(
            "No PostgreSQL connection string found in environment variables."
        )

    return psycopg2.connect(
        db_url,
        cursor_factory=RealDictCursor,
        sslmode="require"
    )


def init_db():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recipients (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_at TIMESTAMP,
                error TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clicks (
                id SERIAL PRIMARY KEY,
                recipient_id TEXT NOT NULL,
                url TEXT NOT NULL,
                clicked_at TIMESTAMP NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS opens (
                id SERIAL PRIMARY KEY,
                recipient_id TEXT NOT NULL,
                opened_at TIMESTAMP NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_recipients_sent_at ON recipients (sent_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_clicks_recipient_id ON clicks (recipient_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_opens_recipient_id ON opens (recipient_id)")
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


@app.before_request
def ensure_database_initialized():
    """Initialize the schema once per warm serverless function instance."""
    global database_initialized
    if not database_initialized:
        init_db()
        database_initialized = True


def register_recipient_in_db(recipient_id, email, status, sent_at, error=None):
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO recipients (id, email, status, sent_at, error)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                status = EXCLUDED.status,
                sent_at = EXCLUDED.sent_at,
                error = EXCLUDED.error;
        """, (recipient_id, email, status, sent_at, error))
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def load_recipients():
    if not MAILS_PATH.exists():
        print(f"Missing {MAILS_PATH}")
        sys.exit(1)
    with MAILS_PATH.open("r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]


def load_template():
    if not TEMPLATE_PATH.exists():
        print(f"Missing {TEMPLATE_PATH}")
        sys.exit(1)
    with TEMPLATE_PATH.open("r", encoding="utf-8") as file:
        return file.read()


def send_one(smtp_conn, to_email, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = SUBJECT
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    plain_text = re.sub(r"<[^>]+>", "", html_body)
    plain_text = re.sub(r"\s+", " ", plain_text).strip()
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    smtp_conn.sendmail(SMTP_USER, [to_email], msg.as_string())


def build_recipient_tracked_html(raw_html, recipient_id):
    """Add recipient-specific click links and a recipient-specific open pixel."""
    return mailer.build_tracked_html(
        raw_html,
        campaign_name="microsoft_offer_2026",
        recipient_id=recipient_id,
        public_base_url=PUBLIC_BASE_URL,
    )


def run_campaign():
    if not SMTP_USER or not SMTP_PASSWORD:
        print("Set SMTP_USER and SMTP_PASSWORD in your .env file first.")
        sys.exit(1)

    init_db()
    recipients = load_recipients()
    raw_html = load_template()
    sent_list, bounced_list = [], []
    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} as {SMTP_USER} ...")
    try:
        smtp_conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        smtp_conn.login(SMTP_USER, SMTP_PASSWORD)
        print("Connected via SSL")
    except Exception as error:
        print(f"SSL failed: {error}, trying STARTTLS...")
        smtp_conn = smtplib.SMTP(SMTP_HOST, 587, timeout=30)
        smtp_conn.starttls()
        smtp_conn.login(SMTP_USER, SMTP_PASSWORD)
        print("Connected via STARTTLS")

    try:
        for email in recipients:
            recipient_id = uuid.uuid4().hex
            now = datetime.utcnow()
            try:
                send_one(smtp_conn, email, build_recipient_tracked_html(raw_html, recipient_id))
            except Exception as error:
                try:
                    register_recipient_in_db(recipient_id, email, "bounced", now, str(error))
                except Exception as database_error:
                    print(f"  failed to record bounce for {email}: {database_error}")
                bounced_list.append(email)
                print(f"  BOUNCED -> {email} ({error})")
                continue

            sent_list.append(email)
            print(f"  sent -> {email}")
            try:
                register_recipient_in_db(recipient_id, email, "sent", now)
            except Exception as error:
                print(f"  sent but failed to record {email}: {error}")
    finally:
        try:
            smtp_conn.quit()
        except Exception:
            pass

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "sent.txt").write_text("\n".join(sent_list), encoding="utf-8")
    (RESULTS_DIR / "delivred.txt").write_text("\n".join(sent_list), encoding="utf-8")
    (RESULTS_DIR / "bounced.txt").write_text("\n".join(bounced_list), encoding="utf-8")
    print(f"\nDone. Sent: {len(sent_list)}  Bounced: {len(bounced_list)}")
    print(f"View dashboard at {PUBLIC_BASE_URL}/dashboard")


@app.route("/track/open/<recipient_id>")
def track_open(recipient_id):
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO opens (recipient_id, opened_at) VALUES (%s, %s)",
            (recipient_id, datetime.utcnow()),
        )
        conn.commit()
    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.warning("Open tracking failed: %s", error)
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

    return Response(
        TRANSPARENT_PIXEL,
        mimetype="image/gif",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.route("/api/register_recipient", methods=["POST"])
def register_recipient():
    try:
        data = request.get_json(force=True)
        required_fields = ("id", "email", "status", "sent_at")
        if not data or any(field not in data for field in required_fields):
            return jsonify({"success": False, "error": "Missing required fields"}), 400
        sent_at = datetime.fromisoformat(data["sent_at"].replace("Z", "+00:00"))
        register_recipient_in_db(
            data["id"], data["email"], data["status"], sent_at, data.get("error") or None
        )
        return jsonify({"success": True})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 400


@app.route("/health")
def health():
    try:
        conn = get_db()
        conn.close()
        return jsonify({
            "status": "ok",
            "database": "connected"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


@app.route("/track/click/<recipient_id>")
def track_click(recipient_id):
    target_url = unquote(request.args.get("url", PUBLIC_BASE_URL))
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO clicks (recipient_id, url, clicked_at) VALUES (%s, %s, %s)",
            (recipient_id, target_url, datetime.utcnow()),
        )
        conn.commit()
    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.warning("Click tracking failed: %s", error)
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()
    return redirect(target_url, code=302)


def get_tracking_maps(cur):
    cur.execute("SELECT recipient_id, COUNT(*) AS c FROM clicks GROUP BY recipient_id")
    clicks_map = {row["recipient_id"]: row["c"] for row in cur.fetchall()}
    cur.execute("SELECT recipient_id, COUNT(*) AS c FROM opens GROUP BY recipient_id")
    opens_map = {row["recipient_id"]: row["c"] for row in cur.fetchall()}
    return clicks_map, opens_map


@app.route("/dashboard")
def dashboard():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM recipients ORDER BY sent_at DESC")
        recipients = cur.fetchall()
        clicks_map, opens_map = get_tracking_maps(cur)

        rows = []
        for recipient in recipients:
            sent_at = recipient["sent_at"]
            rows.append({
                "email": recipient["email"],
                "status": recipient["status"],
                "sent_at": sent_at.isoformat() if sent_at else "",
                "opens": opens_map.get(recipient["id"], 0),
                "clicks": clicks_map.get(recipient["id"], 0),
                "error": recipient["error"] or "",
            })

        sent = sum(1 for row in rows if row["status"] == "sent")
        bounced = sum(1 for row in rows if row["status"] == "bounced")
        clicked = sum(1 for row in rows if row["clicks"] > 0)
        opened = sum(1 for row in rows if row["opens"] > 0)
        totals = {
            "sent": sent,
            "bounced": bounced,
            "clicked": clicked,
            "opened": opened,
            "total_opens": sum(row["opens"] for row in rows),
            "total_recipients": len(rows),
            "ctr": round(clicked / max(1, sent) * 100, 2),
            "open_rate": round(opened / max(1, sent) * 100, 2),
            "bounce_rate": round(bounced / max(1, len(rows)) * 100, 2),
            "delivery_rate": round(sent / max(1, len(rows)) * 100, 2),
        }

        start_day = (datetime.utcnow() - timedelta(days=29)).date()
        cur.execute("""
            SELECT sent_at::date AS day,
                   COUNT(*) FILTER (WHERE status = 'sent') AS sent,
                   COUNT(*) FILTER (WHERE status = 'bounced') AS bounced
            FROM recipients
            WHERE sent_at >= %s AND status IN ('sent', 'bounced')
            GROUP BY sent_at::date
            ORDER BY day DESC
        """, (start_day,))
        time_series = [
            {"date": row["day"].isoformat(), "sent": row["sent"], "bounced": row["bounced"]}
            for row in cur.fetchall()
        ]
        return render_template("dashboard.html", rows=rows, totals=totals, time_series=time_series)
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


@app.route("/api/export")
def export_csv():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM recipients ORDER BY sent_at DESC")
        recipients = cur.fetchall()
        clicks_map, opens_map = get_tracking_maps(cur)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["email", "status", "sent_at", "opens", "clicks", "error"])
        for recipient in recipients:
            sent_at = recipient["sent_at"]
            writer.writerow([
                recipient["email"], recipient["status"],
                sent_at.isoformat() if sent_at else "",
                opens_map.get(recipient["id"], 0),
                clicks_map.get(recipient["id"], 0), recipient["error"] or "",
            ])
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=campaign.csv"},
        )
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


@app.route("/api/alltime")
def alltime_stats():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS count FROM recipients")
        total_recipients = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) AS count FROM recipients WHERE status = 'sent'")
        total_sent = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) AS count FROM recipients WHERE status = 'bounced'")
        total_bounced = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) AS count FROM clicks")
        total_clicks = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(DISTINCT recipient_id) AS count FROM clicks")
        unique_clicked = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) AS count FROM opens")
        total_opens = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(DISTINCT recipient_id) AS count FROM opens")
        unique_opened = cur.fetchone()["count"]
        return jsonify({
            "total_recipients": total_recipients,
            "total_sent": total_sent,
            "total_bounced": total_bounced,
            "total_clicks": total_clicks,
            "unique_clicked": unique_clicked,
            "total_opens": total_opens,
            "unique_opened": unique_opened,
            "ctr": round(unique_clicked / max(1, total_sent) * 100, 2),
            "open_rate": round(unique_opened / max(1, total_sent) * 100, 2),
            "bounce_rate": round(total_bounced / max(1, total_recipients) * 100, 2),
            "delivery_rate": round(total_sent / max(1, total_recipients) * 100, 2),
        })
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


@app.route("/api/reset")
def reset_db():
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM opens")
        cur.execute("DELETE FROM clicks")
        cur.execute("DELETE FROM recipients")
        conn.commit()
        return jsonify({"success": True, "message": "Database reset successfully"})
    except Exception as error:
        if conn is not None:
            conn.rollback()
        return jsonify({"success": False, "error": str(error)}), 400
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


@app.route("/")
def index():
    return redirect("/dashboard")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["send", "serve"])
    args = parser.parse_args()
    if args.mode == "send":
        run_campaign()
    else:
        init_db()
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
