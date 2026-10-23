# CLAUDE.md — triumph-leads-agent

WhatsApp leads agent for a friend's (David's) Triumph motorcycle dealership. Full brief: `SPEC.md` (Hebrew).
Owner: Dadi. Talk to him in Hebrew; code, comments and commits in English.

## Direction (decided 2026-10-23)
The goal is an agent that **runs the conversation by itself**: it replies, offers slots, books the meeting and
hands off to a human. Phase 1 (the wa.me co-pilot) stays available as a channel/mode, not the end state.

Three switches in `.env`:
- `CHANNEL`: `wame` (human sends via a wa.me link) · `simulator` (Dadi plays the customer at /simulator) ·
  `cloud` (WhatsApp Business Cloud API, real customers).
- `AGENT_MODE`: `supervised` (agent drafts, human approves each message and clicks to book) · `auto`.
- `ANTHROPIC_API_KEY`: empty = rule-based fallback engine.

Go-live plan: the dealer's dedicated business number on the Cloud API in **Coexistence** mode. The same number
stays in the WhatsApp Business app on the manager's phone, so he sees every agent message and can jump in. An
`smb_message_echoes` webhook then pauses the agent for that lead. Run supervised first, then auto.
Coexistence availability in Israel is unverified.

Running on **dummy data** until the dealer sends: a real leads Excel, the model list + info, FAQ answers,
business hours/address, meeting owner + length, consent wording, Meta Business account. See SPEC.md.

## Hard rules (never break)
1. The agent answers **only** from `knowledge/` files. No invented prices, promotions, delivery times,
   specs or financing terms. Missing/null field → `intent="handoff"`. Tests enforce this; keep them.
2. Every conversation can go to a human at any moment: `handoff` → `agent_paused=1` + alert. A human reply
   from the phone (echo) or the "take over" button also pauses. Paused leads get no agent replies.
3. WhatsApp only via official routes: `wa.me` or the Cloud API. **Never** automate WhatsApp Web, a browser or the
   desktop app (ToS + ban risk for the business number). This was suggested and declined; keep it declined.
4. Lead PII stays local: `data/` is git-ignored (DB, Google tokens). Never commit real lead data,
   real Excel exports, `.env`, `credentials.json`, `token.json`. `samples/` holds FAKE data only.
5. Tone: Hebrew, personal, short (1–3 sentences), like a good salesperson, not robotic.
6. The app binds to `127.0.0.1`. For the Cloud API, expose **only** `/webhook/whatsapp` through a tunnel
   (e.g. cloudflared). The webhook verifies `X-Hub-Signature-256`; the UI has no auth and must stay local.
7. The agent never claims to be human (prompt rule + `agent_name`); the opening text says it's a digital assistant.
8. Supervised mode never touches the calendar. Only auto mode books by itself.

## Layout
```
app/
  __init__.py          Flask app factory + routes (leads, lead page, /simulator, /alerts, /webhook/whatsapp)
  conversation.py      ConversationEngine: inbound → agent → act on intent (book/handoff/close) → send or draft
  agent.py             the "brain": Claude (tool-forced JSON) or rule-based fallback; match_slot_choice
  channels.py          WameChannel / SimulatorChannel / CloudChannel, webhook parsing + signature check
  config.py            env/.env → Config
  db.py                SQLite (data/leads.db): leads, messages, meetings, alerts; column migrations
  excel_import.py      loose Hebrew/English column matching (COLUMN_ALIASES)
  phone.py             Israeli phone normalization → 9725XXXXXXXX, wa.me links
  knowledge.py         loads knowledge YAML, model matching, prompt context
  calendar_service.py  slot finding, MockCalendar (data/mock_calendar.json), GoogleCalendar (OAuth)
  templates/, static/  Hebrew RTL UI (_thread/_draft/_slots/_paused partials shared by lead page + simulator)
knowledge/*.dummy.yaml placeholder knowledge; real files will replace them (MODELS_FILE / BUSINESS_FILE)
samples/leads_dummy.xlsx  fake leads (regenerate: python scripts/make_dummy_excel.py)
tests/                 unittest-style, runs under pytest too (test_core: phase-1 pieces; test_conversation: engine/webhook)
```

## Commands
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # CHANNEL=simulator AGENT_MODE=auto to try the agent at /simulator
python run.py                   # http://localhost:5050
python -m pytest                # or: python -m unittest discover tests
```

## Statuses & intents
`new` חדש → `sent` נשלח (waiting for customer) → `replied` ענה (waiting for us) → `meeting` נקבעה פגישה;
plus `handoff` הועבר לנציג, `not_relevant` לא רלוונטי.
Agent intents: `answer` · `meeting` (offers slots, stored in `offered_slots`) · `book` (customer picked one; auto
mode creates the event) · `handoff` · `not_interested`.

## WhatsApp Cloud API specifics
- The first message to a lead must be an approved template (`WHATSAPP_OPENING_TEMPLATE`, params {{1}} name, {{2}} model).
- Free text only within 24h of the customer's last message. `send()` refuses outside that window and raises an alert.
- Webhook processing runs in a thread (Meta wants a fast 200). `wa_id` is unique, so retries are ignored.
- `DEBOUNCE_SECONDS` batches several quick customer messages into one reply.

## Conventions
- Python 3.10+, Flask, no frontend build step. Keep it small: ~20 leads/month.
- Agent output is always `Suggestion(message, intent, handoff_reason, used_ai, slots, chosen)`.
- Any AI failure must fall back to the rule engine and never block.
- `anthropic` and Google libs are imported lazily so the app runs without them.
- When the real Excel arrives: add its exact headers to `COLUMN_ALIASES` + a test with those headers.
- When the real knowledge arrives: create `knowledge/models.yaml` + `knowledge/business.yaml`
  (drop `_dummy: true`), point `.env` at them, and update the tests that assert dummy values.
- Don't press send in real WhatsApp with the dummy Excel: the fake numbers may belong to real people.

## Status / open items
- [x] Phase 1: import, opening drafts, wa.me, paste reply → suggestion, slot proposal, booking, statuses
- [x] Conversation engine (auto/supervised), simulator, alerts, human takeover, Cloud API channel + webhook (coexistence echoes)
- [x] Mock calendar. Google Calendar provider written but **not yet tested against a real account**
- [ ] Cloud API tested only against a fake HTTP layer; verify with the dealer's Meta app
- [ ] Claude path tested only with a fake LLM; try with a real API key and tune the prompt on real conversations
- [ ] Hosting for go-live (always-on machine + tunnel, or a small VPS): not decided
- [ ] David had a version built before and lost the file. Asking him (via Dadi) what it did and what's left
