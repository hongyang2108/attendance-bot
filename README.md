# Telegram Group Attendance Tracker

A Telegram bot for managing **attendance, transport requirements, and volunteering hours** within a group.

The bot creates non-anonymous Telegram polls, records each person's responses in a local SQLite database, automatically tracks volunteering hours, and provides attendance summaries and CSV exports.

## Features

* 📋 Create weekly **Yes/No attendance polls**
* 📅 Support custom attendance dates
* ⏱️ Automatically log volunteering hours when someone selects **Yes**
* ⏰ Automatically close polls after a specified deadline
* 🚌 Create transport/bus polls
* 📊 View attendance summaries
* 📈 View results from the latest bus poll
* 🕐 Manually log volunteering hours
* 👤 View your own volunteering-hour record
* 📥 Export attendance and volunteering data as CSV
* 💾 Store data locally using SQLite
* 🔄 Restore scheduled poll deadlines after bot restarts
* ❤️ Built-in HTTP health-check server for Render/UptimeRobot monitoring

---

## Commands

### `/start`

Displays the available commands and basic instructions.

### `/attendance`

Creates an attendance poll using today's date.

```text
/attendance
```

### Custom date

```text
/attendance Sat 23 Aug
```

### Attendance + volunteering hours

```text
/attendance Sat 23 Aug,3.5
```

Anyone who selects **Yes** will automatically have `3.5` volunteering hours logged.

### Attendance + hours + deadline

```text
/attendance Sat 23 Aug,3.5,2h
```

This will:

1. Create the attendance poll.
2. Automatically log 3.5 hours for people who select Yes.
3. Close the poll after 2 hours.

Supported deadline formats:

```text
30m
90m
2h
1.5h
1d
```

You can also leave fields blank:

```text
/attendance ,3.5
```

This uses today's date and assigns 3.5 hours per Yes response.

---

## `/stoppoll`

Manually closes the most recent poll.

```text
/stoppoll
```

You can also specify the poll type:

```text
/stoppoll attendance
/stoppoll bus
```

---

## `/summary`

Displays each person's all-time attendance percentage.

```text
/summary
```

Example:

```text
Attendance summary (across 10 week(s))

• John Tan (@johntan): 9/10 weeks — 90%
• Sarah Lim (@sarahlim): 8/10 weeks — 80%
```

---

## `/export`

Exports attendance and volunteering-hour records into a CSV file.

```text
/export
```

The CSV contains:

| Column   | Description        |
| -------- | ------------------ |
| Name     | User's full name   |
| Username | Telegram username  |
| Poll     | Attendance poll    |
| Answer   | Selected answer    |
| Hours    | Volunteering hours |
| Note     | Optional note      |
| Date     | Date/time recorded |

---

# 🚌 Transport Commands

## `/bus`

Creates a transport poll.

```text
/bus
```

Or specify a date:

```text
/bus 22 Aug
```

The poll contains four options:

1. Two-way transport (to and fro AMK)
2. One way transport from AMK
3. One way transport to AMK
4. No transport needed

---

## `/busreport`

Shows the results of the latest transport poll.

```text
/busreport
```

---

# 🕐 Volunteering Hours

## `/loghours`

Manually records volunteering hours for yourself.

```text
/loghours 3.5
```

You can also include a note:

```text
/loghours 3.5 VDAD setup
```

### Log hours for another person

Reply to someone's Telegram message and use:

```text
/loghours 2
```

The 2 hours will be recorded for the person whose message you replied to.

---

## `/myhours`

Displays your personal volunteering-hour record.

```text
/myhours
```

Example:

```text
Your volunteering hours — total: 12

• 12 Aug: 3h — VDAD session
• 15 Aug: 4h
• 18 Aug: 5h — Event setup
```

---

# 🗄️ Database

The bot uses **SQLite** for persistent storage.

By default, the database is:

```text
attendance.db
```

You can specify a different database location using:

```bash
ATTENDANCE_DB=/path/to/attendance.db
```

The database contains three main tables.

### `polls`

Stores information about polls created by the bot.

Includes:

* Poll ID
* Chat ID
* Question
* Poll type
* Poll options
* Telegram message ID
* Deadline
* Closed status

### `responses`

Stores each user's poll response.

Includes:

* Poll ID
* Telegram user ID
* Username
* Full name
* Selected answer
* Response timestamp

### `volunteer_hours`

Stores volunteering-hour records.

Includes:

* Chat ID
* User ID
* Name
* Hours
* Notes
* Timestamp
* Related attendance poll ID

---

# ⚙️ Installation

## 1. Clone the repository

```bash
git clone <your-repository-url>
cd <your-repository-folder>
```

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

A typical `requirements.txt` should contain:

```text
python-telegram-bot[job-queue]
```

---

# 🔑 Environment Variables

The bot requires a Telegram Bot Token.

Create a bot using **BotFather** on Telegram and obtain your token.

Set the token as an environment variable.

### macOS / Linux

```bash
export BOT_TOKEN="YOUR_BOT_TOKEN"
```

### Windows PowerShell

```powershell
$env:BOT_TOKEN="YOUR_BOT_TOKEN"
```

The application also supports:

```text
ATTENDANCE_DB
```

to specify a custom SQLite database location.

The `PORT` environment variable is used by the built-in health-check server. If it is not provided, the bot defaults to port `8080`.

---

# ▶️ Running the Bot

Start the bot with:

```bash
python bot.py
```

If the `BOT_TOKEN` environment variable is missing, the application will stop and display an error.

Once running, you should see:

```text
Bot starting...
```

Add the bot to your Telegram group.

For reliable operation, make the bot a **group administrator**, especially if the group uses Telegram's privacy mode.

---

# 🌐 Deployment on Render

The bot includes a small HTTP server because Render's web-service environment expects the application to listen on a port.

The server responds to HTTP requests with:

```text
Attendance bot is running.
```

The application automatically uses Render's `PORT` environment variable.

### Render environment variables

Add:

```text
BOT_TOKEN=your_telegram_bot_token
```

Optional:

```text
ATTENDANCE_DB=attendance.db
```

Render should start the application using:

```bash
python bot.py
```

---

# ❤️ Uptime Monitoring

The bot includes a health-check endpoint that can be monitored by an uptime-monitoring service.

The health server listens on:

```text
PORT
```

or defaults to:

```text
8080
```

The health-check response is:

```text
Attendance bot is running.
```

You can configure an uptime monitor to periodically send an HTTP request to your deployed Render service.

For example:

```text
https://your-render-service.onrender.com
```

A successful response indicates that the web server is responding.

> **Note:** An uptime monitor only checks whether the HTTP endpoint responds. It does not directly verify that Telegram polling is functioning correctly.

---

# 🔄 Automatic Poll Deadlines

When an attendance poll is created with a deadline, the bot schedules an automatic close.

For example:

```text
/attendance 23 Aug,3.5,2h
```

The poll will automatically close after two hours.

The bot also stores the deadline in SQLite.

When the application restarts, it checks for polls that:

* Have a deadline
* Have not been closed
* Have a Telegram message ID

It then re-schedules the automatic close.

If the deadline already passed while the bot was offline, the bot attempts to close the poll shortly after restarting.

---

# 🔐 Privacy & Data

The attendance polls are intentionally **non-anonymous** because the bot needs to identify who selected each option.

The bot stores Telegram user information including:

* Telegram user ID
* Username
* First and last name
* Attendance responses
* Volunteering hours

The database is stored locally by the application.

Make sure the database is stored securely and is not publicly exposed.

---

# 🛠️ Project Structure

```text
.
├── bot.py
├── requirements.txt
├── attendance.db          # Created automatically when the bot runs
└── README.md
```

---

# 🧩 Main Components

### Telegram Bot

Built using:

```text
python-telegram-bot
```

### Database

```text
SQLite
```

### Web Health Check

```text
Python HTTPServer
```

### Scheduling

Telegram's Job Queue is used to schedule automatic poll closures.

---

# 📌 Example Workflow

A typical volunteering session could work like this:

### 1. Create the attendance poll

```text
/attendance Sat 23 Aug,3.5,2h
```

### 2. Volunteers vote

```text
Yes
No
```

### 3. Bot automatically records hours

Anyone selecting:

```text
Yes
```

gets:

```text
3.5 hours
```

logged automatically.

### 4. Poll closes

After 2 hours, the bot automatically closes the poll.

### 5. Check attendance

```text
/summary
```

### 6. Check personal hours

```text
/myhours
```

### 7. Export the records

```text
/export
```

This produces a CSV containing the attendance and volunteering records.

---

# 📄 License

This project was created and developed by **Cheong Hong Yang**.

You are free to use, modify, and adapt this project for personal, educational, or non-commercial purposes, provided that appropriate credit is given to the original creator.

© 2026 Cheong Hong Yang. All rights reserved.
