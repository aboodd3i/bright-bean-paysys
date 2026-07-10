# Project Modifications — Brightbean Studio

> **Purpose:** This document records all custom modifications and features added
> to the original Brightbean Studio codebase. New team members should read this
> to understand what we changed, why, and how to use the new features.
>
> **Base repo:** Cloned from Brightbean's official GitHub repository.
> **Date of modifications:** July 9–10, 2026.

---

## Table of Contents

1. [Environment & Dependencies](#1-environment--dependencies)
2. [Email Automation System](#2-email-automation-system)
3. [Post Discovery & Auto-Sync System](#3-post-discovery--auto-sync-system)
4. [Slack Bot (Planned — Not Yet Built)](#4-slack-bot-planned--not-yet-built)
5. [File Change Summary](#5-file-change-summary)
6. [Setup for New Team Members](#6-setup-for-new-team-members)
7. [Useful Commands](#7-useful-commands)

---

## 1. Environment & Dependencies

### Python
- **Python version:** 3.14.6 (system Python at `C:/Python314/python.exe`)
- **Django version:** 5.1.15

### Pillow upgrade
- **Why:** Pillow 10.4 is incompatible with Python 3.14.
- **Change:** Upgraded to Pillow 12.3.0. Updated `requirements.txt` constraint from
  `Pillow>=10.4,<11.0` to `Pillow>=10.4,<13.0`.

### `.env` configuration
The following variables were added/modified in `.env` (copy from `.env.example`):

```env
# Email (Gmail SMTP)
EMAIL_BACKEND_TYPE=smtp
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=<your-email@gmail.com>
EMAIL_HOST_PASSWORD=<gmail-app-password>
EMAIL_USE_TLS=true
DEFAULT_FROM_EMAIL=<your-email@gmail.com>

# Analytics report recipient
ANALYTICS_REPORT_RECIPIENT=<your-email@gmail.com>
```

### `config/settings/development.py`
- **Change:** The email backend was hardcoded to console output. Made it conditional:
  if `EMAIL_HOST_USER` is set, use SMTP; otherwise fall back to console backend.
  This lets developers test email delivery without accidentally printing to console.

### `config/settings/base.py`
- **Change:** Added `ANALYTICS_REPORT_RECIPIENT = env("ANALYTICS_REPORT_RECIPIENT", default="")`.

---

## 2. Email Automation System

### What it does
Compiles analytics statistics from all connected social accounts and sends a
branded HTML email report on a daily schedule.

### Files created

| File | Purpose |
|------|---------|
| `apps/analytics/report.py` | Core report generation + email sending logic |
| `apps/analytics/management/commands/send_analytics_report.py` | CLI command to manually trigger the report |
| `templates/analytics/email/report.html` | Branded HTML email template |
| `templates/analytics/email/report.txt` | Plain-text fallback |

### Files modified

| File | Change |
|------|--------|
| `apps/analytics/tasks.py` | Added `send_daily_analytics_report()` background task (runs every 24h) |
| `apps/analytics/apps.py` | Added `_register_daily_report_task()` to schedule the daily email via `post_migrate` signal |

### How it works

1. **`generate_report(days=30)`** — collects data for all connected accounts:
   - Follower growth (count + delta)
   - Account-level insights (reach, views, engagement, etc.)
   - Per-post engagement metrics
   - Top posts by engagement
   - Full posts table with sortable metrics

2. **`send_report_email(recipient, days=30)`** — renders the HTML + plain-text
   templates and sends via Django's email backend (Gmail SMTP).

3. **Scheduling** — A `django-background-tasks` recurring task runs every 24 hours.
   The first send is scheduled for **5:37 PM PKT (12:37 UTC)**. The schedule is
   computed in `apps/analytics/apps.py` via `REPORT_SEND_TIME_UTC = (12, 37)`.

### To change the send time
Edit `apps/analytics/apps.py`:
```python
# For 9:00 AM PKT → 04:00 UTC
REPORT_SEND_TIME_UTC = (4, 0)

# For 5:37 PM PKT → 12:37 UTC (current setting for testing only)
REPORT_SEND_TIME_UTC = (12, 37)
```
Then run `python manage.py migrate` to re-register the task, or manually update
the existing task's `run_at` in the database.

### To manually send the report
```bash
python manage.py send_analytics_report --days 30 --email someone@example.com
```

---

## 3. Post Discovery & Auto-Sync System

### The problem
When posts are published directly on Facebook/Instagram/LinkedIn (not through
Brightbean's composer), no `PlatformPost` record exists in the database. The
analytics sync has nothing to fetch per-post metrics for, so reports show
"0 posts" even though posts exist on the platform.

### The solution
A post discovery system that fetches each platform's recent feed and creates
`PlatformPost` records for posts not yet tracked. Once imported, the existing
analytics sync automatically fetches per-post metrics.

### Files created

| File | Purpose |
|------|---------|
| `apps/analytics/post_discovery.py` | Discovery service — fetches feeds, imports new posts |
| `apps/analytics/management/commands/sync_posts.py` | CLI command to manually trigger discovery |

### Files modified

| File | Change |
|------|--------|
| `providers/base.py` | Added abstract `get_recent_posts()` method |
| `providers/facebook.py` | Implemented `get_recent_posts()` — GET `/{page_id}/feed` |
| `providers/instagram.py` | Implemented `get_recent_posts()` — GET `/{ig_user_id}/media` |
| `providers/linkedin.py` | Implemented `get_recent_posts()` — GET `/rest/posts?q=author` |
| `apps/analytics/tasks.py` | Added `sync_platform_posts()` background task (runs hourly) |
| `apps/analytics/apps.py` | Added `_register_post_discovery_task()` to register the hourly sync |
| `apps/analytics/views.py` | Added `sync_posts()` view — handles the "Sync Posts" button click, also triggers analytics sync after discovery |
| `apps/analytics/urls.py` | Added `sync-posts/` URL route |
| `templates/analytics/_page_header.html` | Added "Sync Posts" button next to the date range toggle |
| `templates/analytics/_page.html` | Added container for sync result banner |
| `templates/analytics/_sync_result.html` | Success banner showing discovery results |

### How it works

```
User clicks "Sync Posts" button
        │
        ▼
POST /analytics/sync-posts/
        │
        ▼
discover_all_posts(limit=25)
  ├── For each connected account (Facebook, Instagram, LinkedIn):
  │     ├── Resolve provider (FacebookProvider, InstagramProvider, etc.)
  │     ├── Call provider.get_recent_posts(access_token, limit=25)
  │     │     → Returns list of {platform_post_id, caption, published_at, permalink, media_type}
  │     ├── Check which platform_post_ids already exist in DB
  │     └── For each new post:
  │           ├── Create Post record (workspace-scoped)
  │           └── Create PlatformPost record (status=PUBLISHED)
  │
  └── If new posts were discovered:
        └── Run sync_all_account_analytics() to immediately fetch per-post metrics
```

### Background task
`sync_platform_posts` runs **hourly** (`POST_DISCOVERY_INTERVAL_SECONDS = 3600`)
to automatically discover new externally-published posts without manual intervention.

### Supported platforms
- Facebook (page feed)
- Instagram (Graph API media)
- Instagram Login (same as Instagram)
- LinkedIn Company (REST API posts)
- LinkedIn Personal (REST API posts)

### To manually trigger discovery
```bash
# All connected accounts
python manage.py sync_posts

# Single account
python manage.py sync_posts --account <uuid>

# Fetch more posts per account
python manage.py sync_posts --limit 50
```

### UI
The "Sync Posts" button appears in the analytics page header, next to the
7D/30D/90D range toggle. Clicking it:
1. Discovers new posts from all connected accounts
2. Fetches analytics for any newly discovered posts
3. Shows a green success banner with the results

---

## 4. Slack Bot (Planned — Not Yet Built)

### Goal
An interactive Slack bot with an integrated LLM (Claude primary, z.ai fallback)
that answers natural language questions about analytics data.

### Architecture (agreed upon)
- Built as a **Django app inside Brightbean** (`apps/slack_bot/`)
- Uses existing analytics services (`apps/analytics/services.py`) as tool functions
- LLM router interprets user questions and calls appropriate tools
- Responds with Slack Block Kit rich formatting
- Works locally via ngrok, deploys to VM for 24/7 operation

### Status
Architecture discussed and documented. Development not yet started.
See the full architecture plan in the conversation history.

---

## 5. File Change Summary

### New files
```
apps/analytics/report.py
apps/analytics/post_discovery.py
apps/analytics/management/commands/send_analytics_report.py
apps/analytics/management/commands/sync_posts.py
templates/analytics/email/report.html
templates/analytics/email/report.txt
templates/analytics/_sync_result.html
```

### Modified files
```
.env.example                          # Added ANALYTICS_REPORT_RECIPIENT
.gitignore                            # Added debug scripts, OS files, IDE files
requirements.txt                      # Relaxed Pillow version constraint
config/settings/base.py               # Added ANALYTICS_REPORT_RECIPIENT setting
config/settings/development.py        # Conditional email backend
providers/base.py                     # Added get_recent_posts() abstract method
providers/facebook.py                 # Implemented get_recent_posts()
providers/instagram.py                # Implemented get_recent_posts()
providers/linkedin.py                 # Implemented get_recent_posts()
apps/analytics/apps.py                # Registered daily report + post discovery tasks
apps/analytics/tasks.py               # Added send_daily_analytics_report + sync_platform_posts
apps/analytics/views.py               # Added sync_posts view
apps/analytics/urls.py                # Added sync-posts/ route
templates/analytics/_page.html        # Added sync result container
templates/analytics/_page_header.html # Added Sync Posts button
```

---

## 6. Setup for New Team Members

### Prerequisites
- Python 3.12+ (we use 3.14.6)
- Node.js 20+ (for Tailwind CSS)
- A Gmail account with an app password (for email reports)

### Steps

```bash
# 1. Clone the repo
git clone <repo-url>
cd brightbean-studio

# 2. Copy env template
cp .env.example .env

# 3. Edit .env — fill in:
#    - SECRET_KEY (generate a random one)
#    - DATABASE_URL=sqlite:///db.sqlite3 (for local dev)
#    - EMAIL_HOST_USER and EMAIL_HOST_PASSWORD (Gmail app password)
#    - ANALYTICS_REPORT_RECIPIENT (your email)
#    - Platform credentials (Facebook/Instagram/LinkedIn app IDs/secrets)

# 4. Set up Python
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
# or
.venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -r requirements.txt

# 5. Set up Tailwind (optional for backend-only work)
cd theme/static_src && npm install && cd ../..

# 6. Database
python manage.py migrate
python manage.py createsuperuser

# 7. Run the server
python manage.py runserver

# 8. (Optional) Run background task worker in another terminal
python manage.py process_tasks
```

Open http://localhost:8000 and log in.

### Connecting social accounts
1. Go to Settings → Social Accounts
2. Connect Facebook / Instagram / LinkedIn
3. Grant the required permissions
4. Navigate to Analytics to see stats

### Using the Sync Posts button
1. Go to the Analytics page
2. Click "Sync Posts" (next to the date range toggle)
3. New posts published externally will be imported with their analytics

---

## 7. Useful Commands

| Command | What it does |
|---------|-------------|
| `python manage.py runserver 0.0.0.0:8000` | Start dev server |
| `python manage.py process_tasks` | Run background task worker (email reports, post discovery, analytics sync) |
| `python manage.py send_analytics_report --days 30` | Manually send the analytics email report |
| `python manage.py sync_posts` | Manually discover and import external posts (all accounts) |
| `python manage.py sync_posts --account <uuid>` | Discover posts for a single account |
| `python manage.py sync_posts --limit 50` | Fetch up to 50 posts per account |
| `python manage.py backfill_analytics --account <uuid>` | Backfill analytics for a single account |

---

## Key Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| Jul 9 | Use Gmail SMTP for email reports | Simplest setup, free, reliable for low volume |
| Jul 9 | Schedule daily email at 5:37 PM PKT | Team preference for end-of-day summary |
| Jul 9 | Post discovery runs hourly | Catches externally published posts quickly without manual sync |
| Jul 9 | Sync button also triggers analytics fetch | User sees stats immediately, no waiting for hourly cron |
| Jul 10 | Slack bot as Django app (not standalone service) | Direct access to existing analytics services, single deployment |
| Jul 10 | Claude as primary LLM, z.ai as fallback | Claude for accuracy, z.ai for cost/fallback resilience |
