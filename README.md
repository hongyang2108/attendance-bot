# Telegram Group Attendance Tracker

Posts a weekly Yes/No poll in your group and logs every response, so you
can track who's attending week over week — right from the chat.

## 1. Create your bot (5 min, one-time)

1. Open Telegram, search for **@BotFather**, and start a chat.
2. Send `/newbot`.
3. Give it a name (e.g. "Study Group Attendance") and a username
   ending in `bot` (e.g. `studygroup_attendance_bot`).
4. BotFather will reply with a **token** — a long string like
   `123456789:AAExampleTokenHere`. Copy it, you'll need it below.

## 2. Add the bot to your group

1. Open your group → Group Info → Add Member.
2. Search for your bot's username and add it.
3. Make it an **admin** (Group Info → Administrators → Add Admin →
   select your bot). This ensures it can post polls and read
   commands reliably even if the group has privacy restrictions.

## 3. Run the bot

You need somewhere for this script to run continuously (your laptop
while testing, or a small always-on server/service later). Steps:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your bot token (replace with the real one from BotFather)
export BOT_TOKEN="123456789:AAExampleTokenHere"

# 3. Run it
python bot.py
```

You should see `Bot starting...` in the terminal. Leave this running —
if you stop it, the bot goes offline and won't post/collect polls
until you start it again.

## 4. Use it in the group

| Command       | What it does                                              |
|---------------|-------------------------------------------------------------|
| `/attendance` | Posts a new Yes/No poll for that week                      |
| `/report`     | Shows who said Yes/No on the most recent poll               |
| `/summary`    | Shows each person's all-time attendance % across all weeks  |
| `/export`     | Sends you a CSV file of all raw attendance data             |

Typical weekly flow: type `/attendance` in the group each week, let
people vote, then `/report` to see the breakdown, or `/summary`
anytime for the running tally.

## Notes

- The poll is created as **non-anonymous**, which is required for the
  bot to know *who* voted — this also means voters' names are visible
  to the group when they vote (same as the poll in your screenshot).
- Data is stored in a local file `attendance.db` (SQLite) next to the
  script. Back this file up if you care about long-term history.
- This currently tracks one "latest poll" per group for `/report` —
  if you run multiple groups with the same bot, each group's data
  stays separate automatically (keyed by chat ID).

## Keeping it running 24/7

Running it on your own machine only works while that machine is on.
For always-on hosting without touching Google/AWS consoles, easy
options include Railway, Render, or Fly.io (all have free/cheap
tiers) — deploy is basically: push these files, set the `BOT_TOKEN`
environment variable in their dashboard, set the start command to
`python bot.py`. Happy to walk you through whichever one you pick.
