"""
Telegram Group Attendance Tracker
----------------------------------
Posts a weekly Yes/No poll and logs every response to a local SQLite
database, keyed by person + date, so you can pull weekly or cumulative
attendance reports straight in the chat.

Commands (use inside the group):
    /attendance                       - Post a plain Yes/No attendance poll (today's date)
    /attendance <date>                - Post a poll for a specific date/label
    /attendance <date>,<hours>        - Each "Yes" auto-logs that many hours for the person
    /attendance <date>,<hours>,<deadline>  - Also auto-closes the poll after a duration (e.g. 2h, 1d)
    /stoppoll     - Manually close the most recent poll (attendance or bus) early
    /summary      - Show each person's overall attendance % (all-time)
    /export       - Send a CSV file with all raw attendance + hours data
    /bus <date>   - Post a transport poll (4 options: two-way / one-way from / one-way to / none)
    /busreport    - Show results for the most recent bus poll

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
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
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
            poll_id            TEXT PRIMARY KEY,
            chat_id            INTEGER NOT NULL,
            created_at         TEXT NOT NULL,
            question           TEXT NOT NULL,
            hours_per_session  REAL,
            poll_type          TEXT DEFAULT 'attendance',   -- 'attendance' or 'bus'
            options_json       TEXT,                         -- JSON list of this poll's option texts
            message_id         INTEGER,                      -- needed to close/stop the poll later
            deadline           TEXT,                          -- ISO datetime; poll auto-closes after this
            closed             INTEGER DEFAULT 0             -- 1 once auto-closed by the deadline checker
        );

        CREATE TABLE IF NOT EXISTS responses (
            poll_id     TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            full_name   TEXT,
            answer      TEXT NOT NULL,   -- the option text the person chose
            answered_at TEXT NOT NULL,
            PRIMARY KEY (poll_id, user_id),
            FOREIGN KEY (poll_id) REFERENCES polls(poll_id)
        );

        CREATE TABLE IF NOT EXISTS volunteer_hours (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            poll_id     TEXT,            -- set when auto-logged from a poll's Yes vote; NULL for manual /loghours entries
            user_id     INTEGER NOT NULL,
            username    TEXT,
            full_name   TEXT,
            hours       REAL NOT NULL,
            note        TEXT,
            logged_at   TEXT NOT NULL
        );
        """
    )
    # Safe migrations for anyone running an older DB from before these columns existed.
    # Must run BEFORE the index below, since the index depends on poll_id existing.
    for stmt in (
        "ALTER TABLE polls ADD COLUMN poll_type TEXT DEFAULT 'attendance'",
        "ALTER TABLE polls ADD COLUMN options_json TEXT",
        "ALTER TABLE polls ADD COLUMN message_id INTEGER",
        "ALTER TABLE polls ADD COLUMN deadline TEXT",
        "ALTER TABLE polls ADD COLUMN closed INTEGER DEFAULT 0",
        "ALTER TABLE volunteer_hours ADD COLUMN poll_id TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_volunteer_hours_poll_user
            ON volunteer_hours(poll_id, user_id) WHERE poll_id IS NOT NULL
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.IGNORECASE)


def parse_duration(text: str):
    """Parse a short duration like '2h', '90m', '1.5d' into a timedelta, or None if invalid."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("m"):
        return timedelta(minutes=value)
    if unit.startswith("h"):
        return timedelta(hours=value)
    if unit.startswith("d"):
        return timedelta(days=value)
    return None


async def attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post a new non-anonymous Yes/No attendance poll.

    Usage (comma-separated — leave a field blank to skip it):
        /attendance                          -> today's date, no hours, no deadline
        /attendance Sat 23 Aug                -> just a custom date label
        /attendance ,3.5                      -> today's date, each "Yes" auto-logs 3.5 hours
        /attendance Sat 23 Aug,3.5             -> custom date + hours
        /attendance Sat 23 Aug,3.5,2h          -> date + hours + auto-closes in 2 hours
        /attendance ,,1d                       -> today's date, no hours, auto-closes in 1 day
        (deadline understands m/h/d, e.g. "30m", "2h", "1d")
    """
    chat_id = update.effective_chat.id

    raw_text = " ".join(context.args) if context.args else ""
    parts = [p.strip() for p in raw_text.split(",")] if raw_text else []
    while len(parts) < 3:
        parts.append("")
    date_part, hours_part, deadline_part = parts[0], parts[1], parts[2]

    date_label = date_part if date_part else datetime.now().strftime("%d %b %Y")

    hours_per_session = None
    if hours_part:
        try:
            hours_per_session = float(hours_part)
        except ValueError:
            await update.message.reply_text(
                f"Couldn't read \"{hours_part}\" as hours — skipping hour tracking for this poll."
            )

    deadline_at = None
    if deadline_part:
        duration = parse_duration(deadline_part)
        if duration is None:
            await update.message.reply_text(
                f"Couldn't read \"{deadline_part}\" as a deadline (use e.g. 30m, 2h, 1d) — skipping auto-close."
            )
        else:
            deadline_at = datetime.now() + duration

    question = f"Attendance – {date_label}"
    if hours_per_session is not None:
        question += f" ({hours_per_session:g}h)"

    message = await context.bot.send_poll(
        chat_id=chat_id,
        question=question,
        options=["Yes", "No"],
        is_anonymous=False,   # REQUIRED so we can see who answered
        allows_multiple_answers=False,
    )

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO polls (poll_id, chat_id, created_at, question, hours_per_session, poll_type, options_json, message_id, deadline)
        VALUES (?, ?, ?, ?, ?, 'attendance', ?, ?, ?)
        """,
        (
            message.poll.id, chat_id, datetime.now().isoformat(), question, hours_per_session,
            json.dumps(["Yes", "No"]), message.message_id,
            deadline_at.isoformat() if deadline_at else None,
        ),
    )
    conn.commit()
    conn.close()

    if deadline_at:
        if context.job_queue is not None:
            context.job_queue.run_once(
                _auto_close_poll,
                when=deadline_at,
                data={"chat_id": chat_id, "message_id": message.message_id, "poll_id": message.poll.id},
                name=f"autoclose_{message.poll.id}",
            )
            await update.message.reply_text(
                f"This poll will auto-close on {deadline_at.strftime('%d %b, %I:%M%p')}."
            )
        else:
            await update.message.reply_text(
                "Poll posted, but I couldn't schedule the auto-close — the job scheduler isn't "
                "available (check that requirements.txt includes the job-queue extra)."
            )

    logger.info(f"Posted attendance poll {message.poll.id} in chat {chat_id} (hours_per_session={hours_per_session}, deadline={deadline_at})")


async def _auto_close_poll(context: ContextTypes.DEFAULT_TYPE):
    """Job callback: stops a poll once its deadline has passed."""
    data = context.job.data
    chat_id, message_id, poll_id = data["chat_id"], data["message_id"], data["poll_id"]

    conn = get_conn()
    already_closed = conn.execute(
        "SELECT closed FROM polls WHERE poll_id = ?", (poll_id,)
    ).fetchone()
    if already_closed and already_closed[0]:
        conn.close()
        return  # someone already stopped it manually — nothing to do

    try:
        await context.bot.stop_poll(chat_id=chat_id, message_id=message_id)
        conn.execute("UPDATE polls SET closed = 1 WHERE poll_id = ?", (poll_id,))
        conn.commit()
        await context.bot.send_message(chat_id=chat_id, text="⏰ Poll auto-closed — its deadline was reached.")
    except Exception as e:
        logger.warning(f"Failed to auto-close poll {poll_id}: {e}")
    finally:
        conn.close()


def _reschedule_pending_deadlines(app):
    """Re-arms auto-close jobs for polls with a future deadline that haven't fired yet.
    Needed because scheduled jobs are only kept in memory and are lost on a restart/redeploy."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT poll_id, chat_id, message_id, deadline FROM polls "
        "WHERE deadline IS NOT NULL AND closed = 0 AND message_id IS NOT NULL"
    ).fetchall()
    conn.close()

    now = datetime.now()
    for poll_id, chat_id, message_id, deadline_str in rows:
        deadline_at = datetime.fromisoformat(deadline_str)
        # If the deadline already passed while the bot was offline, close it almost immediately;
        # otherwise schedule it for the remaining time.
        when = deadline_at if deadline_at > now else (now + timedelta(seconds=5))
        app.job_queue.run_once(
            _auto_close_poll,
            when=when,
            data={"chat_id": chat_id, "message_id": message_id, "poll_id": poll_id},
            name=f"autoclose_{poll_id}",
        )
        logger.info(f"Re-armed auto-close for poll {poll_id} at {when}")


async def bus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post a non-anonymous poll asking who needs the bus / transport.

    Usage:
        /bus 22 Aug     -> "Taking bus on 22 Aug?" with 4 transport options
        /bus            -> uses today's date
    """
    chat_id = update.effective_chat.id

    date_label = " ".join(context.args) if context.args else datetime.now().strftime("%d %b")
    question = f"Taking bus on {date_label}?"

    options = [
        "Two-way transport (to and fro AMK)",
        "One way transport from AMK",
        "One way transport to AMK",
        "No transport needed",
    ]

    message = await context.bot.send_poll(
        chat_id=chat_id,
        question=question,
        options=options,
        is_anonymous=False,   # REQUIRED so we can see who answered
        allows_multiple_answers=False,
    )

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO polls (poll_id, chat_id, created_at, question, hours_per_session, poll_type, options_json, message_id)
        VALUES (?, ?, ?, ?, NULL, 'bus', ?, ?)
        """,
        (message.poll.id, chat_id, datetime.now().isoformat(), question, json.dumps(options), message.message_id),
    )
    conn.commit()
    conn.close()

    logger.info(f"Posted bus poll {message.poll.id} in chat {chat_id}")


async def stoppoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close (stop) the most recent poll so no more votes can be cast.

    Usage:
        /stoppoll             -> closes whichever poll (attendance or bus) was posted most recently
        /stoppoll attendance  -> closes the most recent attendance poll specifically
        /stoppoll bus         -> closes the most recent bus poll specifically
    """
    chat_id = update.effective_chat.id
    poll_type_filter = context.args[0].lower() if context.args else None

    conn = get_conn()
    if poll_type_filter in ("attendance", "bus"):
        poll = conn.execute(
            "SELECT poll_id, message_id, question FROM polls WHERE chat_id = ? AND poll_type = ? ORDER BY created_at DESC LIMIT 1",
            (chat_id, poll_type_filter),
        ).fetchone()
    else:
        poll = conn.execute(
            "SELECT poll_id, message_id, question FROM polls WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    conn.close()

    if poll is None:
        await update.message.reply_text("No poll found to stop.")
        return

    poll_id, message_id, question = poll

    if message_id is None:
        await update.message.reply_text(
            f"Can't stop \"{question}\" — it was created before this feature was added, "
            "so I don't have its message ID. New polls from now on can be stopped."
        )
        return

    try:
        await context.bot.stop_poll(chat_id=chat_id, message_id=message_id)
        await update.message.reply_text(f"Stopped: \"{question}\" — no more votes can be cast on it. ✅")
    except Exception as e:
        await update.message.reply_text(f"Couldn't stop that poll (it may already be closed). Details: {e}")


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires whenever someone votes on ANY poll the bot created."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user

    conn = get_conn()
    row = conn.execute(
        "SELECT chat_id, hours_per_session, poll_type, options_json FROM polls WHERE poll_id = ?",
        (poll_id,),
    ).fetchone()
    if row is None:
        # Not one of our polls, ignore
        conn.close()
        return

    chat_id, hours_per_session, poll_type, options_json = row
    options = json.loads(options_json) if options_json else ["Yes", "No"]
    full_name = " ".join(filter(None, [user.first_name, user.last_name]))

    if not answer.option_ids:
        # User retracted their vote
        conn.execute(
            "DELETE FROM responses WHERE poll_id = ? AND user_id = ?",
            (poll_id, user.id),
        )
        # Also remove any hours that were auto-logged from this poll for them
        conn.execute(
            "DELETE FROM volunteer_hours WHERE poll_id = ? AND user_id = ?",
            (poll_id, user.id),
        )
    else:
        chosen_index = answer.option_ids[0]
        chosen = options[chosen_index] if chosen_index < len(options) else str(chosen_index)
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

        if hours_per_session is not None and chosen == "Yes":
            # Auto-log hours for this session on a "Yes" vote
            conn.execute(
                """
                INSERT INTO volunteer_hours (chat_id, poll_id, user_id, username, full_name, hours, note, logged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(poll_id, user_id) WHERE poll_id IS NOT NULL DO UPDATE SET
                    hours = excluded.hours,
                    logged_at = excluded.logged_at
                """,
                (
                    chat_id, poll_id, user.id, user.username, full_name,
                    hours_per_session, "Auto-logged from attendance poll",
                    datetime.now().isoformat(),
                ),
            )
        elif hours_per_session is not None and chosen == "No":
            # They switched to "No" — remove any hours auto-logged for this poll
            conn.execute(
                "DELETE FROM volunteer_hours WHERE poll_id = ? AND user_id = ?",
                (poll_id, user.id),
            )

    conn.commit()
    conn.close()


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Yes/No breakdown for the most recent attendance poll in this chat."""
    chat_id = update.effective_chat.id
    conn = get_conn()
    poll = conn.execute(
        "SELECT poll_id, question FROM polls WHERE chat_id = ? AND poll_type = 'attendance' ORDER BY created_at DESC LIMIT 1",
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


async def busreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the breakdown for the most recent bus/transport poll in this chat."""
    chat_id = update.effective_chat.id
    conn = get_conn()
    poll = conn.execute(
        "SELECT poll_id, question, options_json FROM polls WHERE chat_id = ? AND poll_type = 'bus' ORDER BY created_at DESC LIMIT 1",
        (chat_id,),
    ).fetchone()

    if poll is None:
        await update.message.reply_text("No bus poll has been posted yet. Use /bus to start one.")
        conn.close()
        return

    poll_id, question, options_json = poll
    options = json.loads(options_json) if options_json else []

    rows = conn.execute(
        "SELECT full_name, username, answer FROM responses WHERE poll_id = ?",
        (poll_id,),
    ).fetchall()
    conn.close()

    def fmt(r):
        name, username, _ = r
        return f"• {name}" + (f" (@{username})" if username else "")

    lines = [f"*{question}*", ""]
    for option in options:
        matches = [r for r in rows if r[2] == option]
        lines.append(f"*{option} ({len(matches)})*")
        lines.extend(fmt(r) for r in matches) or lines.append("  —")
        lines.append("")

    await update.message.reply_text("\n".join(lines).strip(), parse_mode=ParseMode.MARKDOWN)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show each person's all-time attendance percentage for this chat."""
    chat_id = update.effective_chat.id
    conn = get_conn()

    total_polls = conn.execute(
        "SELECT COUNT(*) FROM polls WHERE chat_id = ? AND poll_type = 'attendance'", (chat_id,)
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
        WHERE p.chat_id = ? AND p.poll_type = 'attendance'
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
    """Send a CSV of all data for this chat, grouped by person.

    Combines attendance poll responses and volunteer hours into one sheet,
    sorted by name so everything for one person sits together. Attendance
    rows leave the Hours column blank; hours rows leave Poll/Answer blank.
    """
    chat_id = update.effective_chat.id
    conn = get_conn()

    attendance_rows = conn.execute(
        """
        SELECT r.full_name, r.username, p.question AS poll, r.answer, NULL AS hours,
               NULL AS note, r.answered_at AS logged_at
        FROM responses r
        JOIN polls p ON p.poll_id = r.poll_id
        WHERE p.chat_id = ?
        """,
        (chat_id,),
    ).fetchall()

    hours_rows = conn.execute(
        """
        SELECT full_name, username, NULL AS poll, NULL AS answer, hours, note, logged_at
        FROM volunteer_hours
        WHERE chat_id = ?
        """,
        (chat_id,),
    ).fetchall()
    conn.close()

    all_rows = list(attendance_rows) + list(hours_rows)
    # Group by name: sort by full name first, then by date within each person
    all_rows.sort(key=lambda r: (r[0] or "", r[6]))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Username", "Poll", "Answer", "Hours", "Note", "Date"])
    for full_name, username, poll, answer, hours, note, logged_at in all_rows:
        date_str = datetime.fromisoformat(logged_at).strftime("%d %b %Y %H:%M")
        writer.writerow([
            full_name,
            f"@{username}" if username else "",
            poll or "",
            answer or "",
            f"{hours:g}" if hours is not None else "",
            note or "",
            date_str,
        ])
    buffer.seek(0)

    data = io.BytesIO(buffer.getvalue().encode("utf-8"))
    data.name = "attendance_export.csv"

    await update.message.reply_document(document=InputFile(data, filename="attendance_export.csv"))


async def loghours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log volunteering hours for a session (personal record, not posted publicly).

    Usage:
        /loghours 3.5              -> logs 3.5 hours for yourself
        /loghours 3.5 VDAD setup   -> same, with an optional note
        (reply to someone else's message with /loghours 2 to log for them)
    """
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Usage: /loghours <hours> [optional note]\n"
            "e.g. /loghours 3.5 VDAD session\n\n"
            "Tip: reply to someone's message with /loghours <hours> to log it for them."
        )
        return

    try:
        hours = float(context.args[0])
    except ValueError:
        await update.message.reply_text("First value after /loghours must be a number, e.g. /loghours 3.5")
        return

    note = " ".join(context.args[1:]) if len(context.args) > 1 else None

    # If used as a reply to someone's message, log the hours for THAT person.
    # Otherwise, log for whoever sent the command.
    if update.message.reply_to_message is not None:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    full_name = " ".join(filter(None, [target.first_name, target.last_name]))

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO volunteer_hours (chat_id, user_id, username, full_name, hours, note, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, target.id, target.username, full_name, hours, note, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    # Kept brief and low-key on purpose — not broadcasting totals to the group
    await update.message.reply_text(f"Logged {hours} hour(s) for {full_name}. ✅")


async def myhours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the requester's own personal volunteering hours record."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT hours, note, logged_at FROM volunteer_hours
        WHERE chat_id = ? AND user_id = ?
        ORDER BY logged_at
        """,
        (chat_id, user.id),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No volunteering hours logged for you yet.")
        return

    total = sum(r[0] for r in rows)
    lines = [f"*Your volunteering hours* — total: {total:g}", ""]
    for hours, note, logged_at in rows:
        date_str = datetime.fromisoformat(logged_at).strftime("%d %b")
        entry = f"• {date_str}: {hours:g}h"
        if note:
            entry += f" — {note}"
        lines.append(entry)

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Attendance bot ready.\n\n"
        "/attendance – plain Yes/No poll (today's date)\n"
        "/attendance <date> – custom date label, e.g. /attendance Sat 23 Aug\n"
        "/attendance <date>,<hours> – each \"Yes\" auto-logs hours, e.g. /attendance Sat 23 Aug,3.5\n"
        "/attendance <date>,<hours>,<deadline> – also auto-closes the poll, e.g. /attendance Sat 23 Aug,3.5,2h\n"
        "  (leave a field blank to skip it, e.g. /attendance ,3.5 for hours only; deadline uses m/h/d)\n"
        "/stoppoll – manually close the most recent poll early\n"
        "/summary – all-time attendance % per person\n"
        "/export – download all data as CSV\n\n"
        "/bus <date> – post a transport poll, e.g. /bus 22 Aug\n"
        "/busreport – results of the latest bus poll"
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
    app.add_handler(CommandHandler("bus", bus))
    app.add_handler(CommandHandler("stoppoll", stoppoll))
    app.add_handler(CommandHandler("busreport", busreport))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(PollAnswerHandler(poll_answer))

    if app.job_queue is not None:
        _reschedule_pending_deadlines(app)
    else:
        logger.warning("job_queue not available — deadlines won't auto-close. Check requirements.txt has the job-queue extra.")

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
