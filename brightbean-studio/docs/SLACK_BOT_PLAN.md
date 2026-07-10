# Slack Analytics Bot — Implementation Plan

> **From:** Muhammad Abdullah
> **Date:** July 10, 2026
> **Status:** For team lead review before development begins
> **Base:** Brightbean Studio (Django 5.1.15, Python 3.14)

---

## 1. Overview

We are building an interactive Slack bot that lives inside a Slack channel and
answers natural language questions about our social media analytics. The bot
uses an LLM (Claude, with z.ai as fallback) to interpret questions and call
existing analytics services in Brightbean to fetch real data.

**Example questions the bot will handle:**
- "What is the most trending post on Instagram?"
- "Which post on LinkedIn had the most impressions?"
- "How can I gain more reach on Facebook?"
- "Send me all analytics of Instagram"
- "Compare Facebook vs Instagram this month"

---

## 2. Architecture

The bot is built as a **Django app inside Brightbean** (`apps/slack_bot/`).
This gives it direct access to the database, existing analytics services,
background task infrastructure, and settings — no separate service needed.

```
User types in Slack
    │
    ▼
Slack API ──HTTP POST──► Django view (/slack/commands/ or /slack/events/)
    │
    ▼
Preprocessing Layer (fast regex checks for "hi", "help", "status" and other simple commands that do not require LLM to prevent unnecessary api calls)
    │
    ├── simple? ──► return canned response immediately
    │
    └── complex? ──► LLM Layer
                        │
                        ▼
                     Claude (primary) / z.ai (fallback)
                        │
                        ▼
                     LLM decides which tool(s) to call
                        │
                        ▼
                     Tool Layer (Python functions wrapping analytics services)
                        │
                        ▼
                     Raw structured data returned
                        │
                        ▼
                     LLM synthesizes natural language answer
                        │
                        ▼
                     Formatter (Slack Block Kit JSON)
                        │
                        ▼
                     POST response back to Slack channel
```

---

## 3. Component Breakdown

### 3.1 Slack Integration Layer

**Two entry points:**

| Entry Point | How it works | Example |
|---|---|---|
| Slash Command | User types `/analytics <question>` → Slack sends HTTP POST to our Django view | `/analytics what's trending on instagram` |
| App Mention | User @mentions the bot in a channel → Slack sends an event via Events API | `@SocialMediaAgent which post on linkedin had the most impressions?` |

**Slack App setup (one-time, documented for the team):**
1. Create app at `api.slack.com/apps`
2. Enable Slash Commands → point to `https://<our-domain>/slack/commands/analytics`
3. Enable Event Subscriptions → subscribe to `app_mention` → point to `https://<our-domain>/slack/events`
4. Enable Bot Token Scopes: `chat:write`, `commands`, `app_mentions:read`
5. Install to workspace → get Bot User OAuth Token (`xoxb-...`) and Signing Secret
6. Store both in `.env`

**Local testing:** Slack needs a public URL. We use ngrok during development:
```
ngrok http 8000  →  https://abc123.ngrok.io
```
On the VM, the domain is already public — no ngrok needed.

### 3.2 Preprocessing Layer

A lightweight layer that intercepts simple queries **before** they reach the LLM.
This saves API calls and reduces latency for trivial interactions.

| User input | Response | How |
|---|---|---|
| `hi`, `hello`, `hey` | "Hi! Ask me about your social media analytics." | Regex match |
| `help`, `what can you do` | List of example questions + capabilities | Regex match |
| `status` | "✅ Connected: Facebook, Instagram, LinkedIn" | Direct DB query |
| Everything else | → Forward to LLM Layer | Fall through |

### 3.3 LLM Layer

**Primary:** Claude (Anthropic API)
**Fallback:** z.ai (GLM-5.2)

**How it works:**
1. The user's question is sent to Claude along with a system prompt that
   describes all available tools and their parameters.
2. Claude decides which tool(s) to call and with what arguments.
3. We execute the tool(s) and return the raw data to Claude.
4. Claude generates a natural language answer with insights.

**Fallback triggers (Claude → z.ai):**
- Claude API is unreachable (network error)
- Claude rate limit hit (HTTP 429)
- Claude API timeout (>15 seconds)
- Claude returns an error response

**Fallback does NOT trigger for:**
- Low-quality answers (that's a prompt engineering issue, not a fallback case)

**Conversation context:**
- Store the last 10 messages per Slack channel in a database model.
- Pass as context to the LLM so follow-up questions work:
  - User: "What's the top post on Instagram?"
  - Bot: (answers)
  - User: "What about Facebook?" → bot understands "Facebook" replaces "Instagram"

### 3.4 Tool Layer

Python functions that wrap Brightbean's existing analytics services.
Each tool returns **structured data** (dicts/lists), not formatted strings.

**Complete tool list:**

| Tool | Parameters | What it does | Data source |
|---|---|---|---|
| `get_account_stats` | `platform`, `days=30` | Account-level metrics (followers, reach, engagement, views) | `services.account_analytics_bundle()` |
| `get_top_posts` | `platform`, `metric`, `limit=5`, `days=30` | Top posts ranked by a specific metric | `services.all_posts_for()` sorted |
| `get_post_detail` | `post_id` | Full metrics for one specific post | `PostInsightsSnapshot` query |
| `get_all_posts` | `platform`, `days=30` | All posts with their metrics in a time range | `services.all_posts_for()` |
| `get_recommendations` | `platform`, `goal` | AI recommendations for improving a metric | Analyzes posting patterns, best times, content types |
| `compare_platforms` | `metric`, `days=30` | Compare a metric across all connected platforms | Multi-account query |
| `get_follower_growth` | `platform`, `days=30` | Follower count + growth trend over time | `services.follower_growth()` |
| `get_engagement_summary` | `platform`, `days=30` | Engagement breakdown (reactions, comments, shares) | `services.engagement_card()` |
| `list_connected_accounts` | none | Which platforms are connected + status | `SocialAccount` query |

**Why structured data:** The LLM takes the raw data and writes the natural
language response. This separation means tools are testable independently and
the same tools work for Slack, email, API, or any future interface.

### 3.5 Response Formatter

Takes the LLM's natural language answer + raw data and wraps it in Slack's
Block Kit JSON for rich display.

**Slack constraints:**
- Max 50 blocks per message
- Max ~4096 characters per message
- Max 3000 characters per text block

**Handling large responses:**
- If data fits: single rich message with sections, metrics, and context.
- If data is too large: post a summary in the channel + send a CSV/JSON file
  attachment with the full data.
- Pagination with "Next →" interactive buttons for medium-sized responses.

---

## 4. Edge Cases & Error Handling

### 4.1 Slack Platform Constraints

| Edge Case | Solution |
|---|---|
| LLM takes >3 seconds to respond | Acknowledge with HTTP 200 immediately (Slack requires this within 3s). Process the question asynchronously. Post the result to the channel when ready. Show "🤔 Thinking..." as initial response. |
| Data too large for one Slack message | Post a summary + file attachment (CSV/JSON). Or paginate with "Next →" buttons. |
| Slack retrying the same request (happens if we don't respond in 3s) | Detect retry using `X-Slack-Request-Timestamp` header. If we've already processed this request, return 200 without re-processing. |
| Bot mentioned in a thread | Respond in the same thread (use `thread_ts` from the event). |

### 4.2 User Input Issues

| Edge Case | Solution |
|---|---|
| User asks about a platform not connected (e.g., "X", "Twitter", "TikTok") | Tool returns "No connected account for X". LLM explains: "You don't have X connected. Currently connected: Facebook, Instagram, LinkedIn." |
| User question is ambiguous (e.g., "LinkedIn" when both company and personal are connected) | LLM asks a clarifying question: "Did you mean your LinkedIn company page or your personal profile?" |
| User asks something unrelated to analytics (e.g., "What's the weather?") | LLM politely declines: "I'm an analytics assistant. I can help with your social media stats. Try asking 'What's my top post on Facebook?'" |
| User sends empty message or just punctuation | Preprocessing catches this → "Could you ask a specific question about your analytics?" |
| User asks in a non-English language | LLM handles multilingual naturally (Claude supports many languages). No special handling needed. |
| User asks multiple questions at once | LLM handles multi-step reasoning. It may call multiple tools and synthesize a combined answer. |

### 4.3 LLM Issues

| Edge Case | Solution |
|---|---|
| Claude API is down | Automatically fall back to z.ai (GLM-5.2). Log the fallback. |
| Claude rate limit hit (429) | Fall back to z.ai. Log a warning. |
| Claude timeout (>15s) | Fall back to z.ai. If z.ai also times out, respond with "Sorry, I'm having trouble right now. Please try again in a moment." |
| Both Claude and z.ai fail | Respond with error message. Log full error for debugging. |
| LLM hallucinates a tool that doesn't exist | Validate tool name against our registered tools list. If invalid, return error to LLM: "That tool doesn't exist. Available tools are: ...". LLM retries. |
| LLM calls a tool with wrong parameters | Validate parameters against the tool's schema. Return validation error to LLM. LLM retries with corrected params. |
| LLM loops (calls tools repeatedly without answering) | Set a max of 5 tool-call rounds. If exceeded, respond with whatever data we have + "I wasn't able to fully answer that question." |

### 4.4 Data Issues

| Edge Case | Solution |
|---|---|
| No analytics data exists for the requested platform/time range | Tool returns empty result. LLM explains: "No data found for Instagram in the last 30 days. Try a wider time range or make sure your account is connected." |
| Account is connected but analytics_needs_reconnect flag is set | Tool returns a special status. LLM explains: "Your Facebook account needs to be reconnected to fetch analytics. Please reconnect in Brightbean Settings." |
| Post discovery hasn't run yet (no PlatformPost records) | LLM suggests: "No posts found. Try clicking 'Sync Posts' in Brightbean first, or I can run the sync for you." |
| Stale data (last sync was >24h ago) | Include a note in the response: "⚠️ Data was last synced X hours ago. Click 'Sync Posts' in Brightbean for fresh data." |

### 4.5 Security & Access Control

| Edge Case | Solution |
|---|---|
| Request doesn't have valid Slack signature | Reject with HTTP 401. Log the attempt. |
| User from a different workspace tries to use the bot | Verify team_id matches our configured workspace. Reject if different. |
| Multiple users ask questions simultaneously | Each request is independent. No shared state between concurrent requests (except conversation history, which is keyed by channel). |
| Someone spoofs a Slack user ID | Map Slack user IDs to Brightbean users. If no mapping exists, respond with "You don't have access to analytics. Please contact an admin." |

### 4.6 Performance & Cost

| Edge Case | Solution |
|---|---|
| Same question asked repeatedly (rate limiting) | Cache LLM responses for identical questions within a 5-minute window per channel. Return cached response without calling the LLM. |
| User spamming the bot | Throttle: max 10 questions per user per minute. Respond with "You're asking too fast! Please wait a moment between questions." |
| LLM API costs growing | Log all LLM calls (question, tokens used, cost estimate). Review monthly. Switch to local LLM (Ollama) on the VM if costs exceed budget. |
| Background task queue backing up | Process Slack questions with priority over other background tasks. If queue is too long, respond with "I'm busy right now, try again in a minute." |

---

## 5. Proposed File Structure

```
apps/slack_bot/
    __init__.py
    apps.py                          # App config
    urls.py                          # /slack/commands/, /slack/events/, /slack/interactive/
    views.py                         # Request handlers (verify signature, route)
    signing.py                       # Slack request signature verification
    preprocessing.py                 # Fast regex checks for simple queries
    router.py                        # LLM router — takes question, calls LLM, dispatches tools
    tools.py                         # Tool definitions + handlers (wraps analytics services)
    formatter.py                     # Slack Block Kit JSON builders
    cache.py                         # Response caching for rate limiting
    models.py                        # Conversation history, user mappings
    tasks.py                         # Background task for async LLM processing
    llm/
        __init__.py
        base.py                      # Abstract LLM interface
        claude_client.py             # Claude (Anthropic) implementation
        glm_client.py                # z.ai (GLM-5.2) implementation
    management/commands/
        test_slack_bot.py            # CLI to test bot without Slack (local dev)
```

---

## 6. Environment Variables (to add to `.env`)

```env
# Slack Bot
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_BOT_PUBLIC_URL=https://abc123.ngrok.io   # ngrok for dev, real URL for VM

# LLM
LLM_PRIMARY=claude
ANTHROPIC_API_KEY=sk-ant-...
LLM_FALLBACK=glm
ZAI_API_KEY=...
LLM_TIMEOUT_SECONDS=15
LLM_MAX_TOOL_ROUNDS=5

# Bot behavior
SLACK_BOT_CACHE_TTL_SECONDS=300        # 5-minute response cache
SLACK_BOT_MAX_MSG_PER_MINUTE=10        # per-user throttle
SLACK_BOT_CONTEXT_WINDOW=10            # last N messages for conversation context
```

---

## 7. Local Dev vs VM Deployment

| Concern | Local Dev | VM (Production) |
|---|---|---|
| Public URL | ngrok (`ngrok http 8000`) | VM's domain (e.g., `bot.brightbean.xyz`) |
| Slack App URL | Point to ngrok URL | Point to VM domain |
| LLM | Claude API | Claude API or local Ollama (cost savings) |
| Database | SQLite | PostgreSQL |
| Background tasks | `python manage.py process_tasks` | systemd service |
| Server | `manage.py runserver` | gunicorn + nginx (existing setup) |
| Always on | Manual start | systemd / Docker restart policy |
| HTTPS | ngrok provides it | Caddy / Let's Encrypt (existing) |

**The architecture doesn't change between dev and VM** — only config values differ.

---

## 8. Build Phases

| Phase | What | Estimated Effort | Dependencies |
|---|---|---|---|
| 1 | Slack app setup + signature verification + basic echo bot | 1 day | None |
| 2 | Preprocessing layer (help, hi, status) | 0.5 day | Phase 1 |
| 3 | Tool layer — wrap existing analytics services as callable tools | 1-2 days | None (can parallelize with 1-2) |
| 4 | LLM router — connect Claude, function calling, tool dispatch, z.ai fallback | 2-3 days | Phases 1, 3 |
| 5 | Block Kit formatter — rich responses, file attachments, pagination | 1-2 days | Phase 4 |
| 6 | Conversation memory + clarifying questions | 1 day | Phase 4 |
| 7 | Edge case handling + caching + throttling | 1-2 days | Phases 4, 5 |
| 8 | Local LLM (Ollama) for VM deployment | 1 day | Phase 4 |
| 9 | Deploy to VM, systemd service, monitoring | 1 day | All above |

**Total estimated effort:** 9-13 days

---

## 9. Testing Strategy

- **Phase 1-2:** Test with `curl` commands simulating Slack requests
- **Phase 3:** Unit tests for each tool function (input → expected output)
- **Phase 4:** Test LLM router with a curated set of 20+ sample questions
- **Phase 5:** Verify Block Kit JSON renders correctly in Slack
- **Phase 7:** Test all edge cases from Section 4
- **`test_slack_bot` management command:** Lets you test the bot from CLI without Slack:
  ```bash
  python manage.py test_slack_bot "What's the top post on Instagram?"
  ```

---

## 10. Open Questions for Team Lead

1. **Which Slack workspace/channel?** Need the workspace to install the app.
2. **Claude API budget?** Need to know the monthly spend limit to decide when to switch to local LLM.
3. **z.ai API access?** Do we already have an API key, or need to set one up?
4. **VM specs?** If we want to run a local LLM (Ollama) as fallback on the VM, we need GPU/RAM specs.
5. **Who can create the Slack app?** Needs a workspace admin to install.
6. **Should the bot work in DMs or only in channels?** Slack supports both — do we want both?
7. **Multi-workspace?** Just our internal team, or do we want this to work for clients too?
