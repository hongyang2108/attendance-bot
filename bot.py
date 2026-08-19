"""
Telegram Group Attendance Tracker
----------------------------------
Posts a weekly Yes/No poll and logs every response to a local SQLite
database, keyed by person + date, so you can pull weekly or cumulative
attendance reports straight in the chat.

Commands (use inside the group):
    /attendance   - Post a new Yes/No attendance poll
    /report       - Show results for the most recent poll
    /summary      - Show each person's overall attendance % (all-time)
    /export       - Send a CSV file with all raw attendance data

Setup:
    1. pip install -r requirements.txt
    2. Set your bot token as an environment variable:
         export BOT_TOKEN="123456:ABC-your-token-here"
    3. Run:  python bot.py
    4. Add the bot to your group and make it an ADMIN
       (it needs admin rights to post polls reliably and to read
       messages if group privacy mode is on).
"""

import csv
import io
import logging
import os
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("ATTENDANCE_DB", "attendance.db")


# ---------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS polls (
            poll_id     TEXT PRIMARY KEY,
            chat_id     INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            question    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS responses (
            poll_id     TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            full_name   TEXT,
            answer      TEXT NOT NULL,   -- 'Yes' or 'No'
            answered_at TEXT NOT NULL,
            PRIMARY KEY (poll_id, user_id),
            FOREIGN KEY (poll_id) REFERENCES polls(poll_id)
        );
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------
async def attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post a new non-anonymous Yes/No attendance poll.

    Usage:
        /attendance                -> uses today's date
        /attendance Sat 23 Aug     -> uses whatever text you type instead
    """
    chat_id = update.effective_chat.id

    if context.args:
        # User supplied a custom date/label, e.g. "/attendance Sat 23 Aug"
        date_label = " ".join(context.args)
    else:
        date_label = datetime.now().strftime("%d %b %Y")

    question = f"Attendance – {date_label}"

    message = await context.bot.send_poll(
        chat_id=chat_id,
        question=question,
        options=["Yes", "No"],
        is_anonymous=False,   # REQUIRED so we can see who answered
        allows_multiple_answers=False,
    )

    conn = get_conn()
    conn.execute(
        "INSERT INTO polls (poll_id, chat_id, created_at, question) VALUES (?, ?, ?, ?)",
        (message.poll.id, chat_id, datetime.now().isoformat(), question),
    )
    conn.commit()
    conn.close()

    logger.info(f"Posted attendance poll {message.poll.id} in chat {chat_id}")


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires whenever someone votes on ANY poll the bot created."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user

    conn = get_conn()
    row = conn.execute("SELECT chat_id FROM polls WHERE poll_id = ?", (poll_id,)).fetchone()
    if row is None:
        # Not one of our attendance polls, ignore
        conn.close()
        return

    if not answer.option_ids:
        # User retracted their vote
        conn.execute(
            "DELETE FROM responses WHERE poll_id = ? AND user_id = ?",
            (poll_id, user.id),
        )
    else:
        chosen = "Yes" if answer.option_ids[0] == 0 else "No"
        full_name = " ".join(filter(None, [user.first_name, user.last_name]))
        conn.execute(
            """
            INSERT INTO responses (poll_id, user_id, username, full_name, answer, answered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(poll_id, user_id) DO UPDATE SET
                answer = excluded.answer,
                answered_at = excluded.answered_at
            """,
            (poll_id, user.id, user.username, full_name, chosen, datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Yes/No breakdown for the most recent poll in this chat."""
    chat_id = update.effective_chat.id
    conn = get_conn()
    poll = conn.execute(
        "SELECT poll_id, question FROM polls WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
        (chat_id,),
    ).fetchone()

    if poll is None:
        await update.message.reply_text("No attendance poll has been posted yet. Use /attendance to start one.")
        conn.close()
        return

    poll_id, question = poll
    rows = conn.execute(
        "SELECT full_name, username, answer FROM responses WHERE poll_id = ? ORDER BY answer, full_name",
        (poll_id,),
    ).fetchall()
    conn.close()

    yes_list = [r for r in rows if r[2] == "Yes"]
    no_list = [r for r in rows if r[2] == "No"]

    def fmt(r):
        name, username, _ = r
        return f"• {name}" + (f" (@{username})" if username else "")

    lines = [f"*{question}*", ""]
    lines.append(f"✅ *Yes ({len(yes_list)})*")
    lines.extend(fmt(r) for r in yes_list) or lines.append("  —")
    lines.append("")
    lines.append(f"❌ *No ({len(no_list)})*")
    lines.extend(fmt(r) for r in no_list) or lines.append("  —")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show each person's all-time attendance percentage for this chat."""
    chat_id = update.effective_chat.id
    conn = get_conn()

    total_polls = conn.execute(
        "SELECT COUNT(*) FROM polls WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0]

    if total_polls == 0:
        await update.message.reply_text("No attendance data yet.")
        conn.close()
        return

    rows = conn.execute(
        """
        SELECT r.full_name, r.username,
               SUM(CASE WHEN r.answer = 'Yes' THEN 1 ELSE 0 END) AS yes_count,
               COUNT(*) AS answered_count
        FROM responses r
        JOIN polls p ON p.poll_id = r.poll_id
        WHERE p.chat_id = ?
        GROUP BY r.user_id
        ORDER BY yes_count DESC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()

    lines = [f"*Attendance summary* (across {total_polls} week(s))", ""]
    for name, username, yes_count, answered_count in rows:
        pct = round(100 * yes_count / total_polls)
        label = f"@{username}" if username else name
        lines.append(f"• {name} ({label}): {yes_count}/{total_polls} weeks — {pct}%")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a CSV of all raw attendance data for this chat."""
    chat_id = update.effective_chat.id
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT p.question, r.full_name, r.username, r.answer, r.answered_at
        FROM responses r
        JOIN polls p ON p.poll_id = r.poll_id
        WHERE p.chat_id = ?
        ORDER BY p.created_at, r.answer
        """,
        (chat_id,),
    ).fetchall()
    conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Poll", "Name", "Username", "Answer", "Answered At"])
    writer.writerows(rows)
    buffer.seek(0)

    data = io.BytesIO(buffer.getvalue().encode("utf-8"))
    data.name = "attendance_export.csv"

    await update.message.reply_document(document=InputFile(data, filename="attendance_export.csv"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Attendance bot ready.\n\n"
        "/attendance – post this week's Yes/No poll (uses today's date)\n"
        "/attendance <date> – post a poll for a specific date, e.g. /attendance Sat 23 Aug\n"
        "/report – results of the latest poll\n"
        "/summary – all-time attendance % per person\n"
        "/export – download all data as CSV"
    )


# ---------------------------------------------------------------------
# Tiny web server (required by Render's free "web service" tier, which
# expects something listening on a port and responding to HTTP requests
# to know the app is alive; ping it periodically with a free uptime
# monitor like UptimeRobot to keep it from sleeping)
# ---------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Attendance bot is running.")

    def log_message(self, format, *args):
        pass  # silence default access logging


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    server.serve_forever()


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing BOT_TOKEN environment variable.\n"
            "Set it with: export BOT_TOKEN='your-token-from-botfather'"
        )

    init_db()

    # Start the tiny web server in the background so Render sees the
    # app as "live" (this thread does nothing but answer health checks)
    threading.Thread(target=_start_health_server, daemon=True).start()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("attendance", attendance))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(PollAnswerHandler(poll_answer))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
