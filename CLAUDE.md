# CLAUDE.md — triumph-leads-agent

WhatsApp leads co-pilot for a friend's Triumph motorcycle dealership. Full brief: `SPEC.md` (Hebrew).
Owner: Dadi. Talk to him in Hebrew; code, comments and commits in English.

## Current phase
**Phase 1 — "co-pilot"** (local web app on Dadi's / the dealer's Mac). Nothing is sent automatically:
the salesperson reviews every draft and presses "send" in WhatsApp via a `wa.me` link.
Phase 2 (WhatsApp Business Cloud API, autonomous agent) — do NOT start until phase 1 is validated.

Running on **dummy data** until the dealer sends: a real leads Excel, the model list + info, FAQ answers,
business hours/address, meeting owner + length, consent wording, sending number. See checklist in SPEC.md.

## Hard rules (from SPEC — never break)
1. The agent answers **only** from `knowledge/` files. No invented prices, promotions, delivery times,
   specs or financing terms. Missing/null field → `intent="handoff"`. Tests enforce this — keep them.
2. Every conversation can be handed to a human at any moment (`handoff` status + reason shown).
3. WhatsApp only via official routes: `wa.me` (phase 1), Cloud API (phase 2). **Never** automate WhatsApp Web.
4. Lead PII stays local: `data/` is git-ignored (DB, uploads, Google tokens). Never commit real lead data,
   real Excel exports, `.env`, `credentials.json`, `token.json`. `samples/` holds FAKE data only.
5. Tone: Hebrew, personal, short (1–3 sentences), like a good salesperson — not robotic.
6. The app binds to `127.0.0.1` only.

## Layout
```
app/
  __init__.py          Flask app factory + all routes
  config.py            env/.env → Config
  db.py                SQLite (data/leads.db): leads, messages, meetings; STATUSES
  excel_import.py      loose Hebrew/English column matching (COLUMN_ALIASES)
  phone.py             Israeli phone normalization → 9725XXXXXXXX, wa.me links
  knowledge.py         loads knowledge YAML, model matching, prompt context
  agent.py             opening + reply drafts: Claude (tool-forced JSON) or rule-based fallback
  calendar_service.py  slot finding, MockCalendar (data/mock_calendar.json), GoogleCalendar (OAuth)
  templates/, static/  Hebrew RTL UI
knowledge/*.dummy.yaml placeholder knowledge — real files will replace them (set MODELS_FILE / BUSINESS_FILE)
samples/leads_dummy.xlsx  fake leads (regenerate: python scripts/make_dummy_excel.py)
tests/test_core.py     unittest-style; runs under pytest too
```

## Commands
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY to enable Claude; empty = no-AI mode
python run.py                   # http://localhost:5050
python -m pytest                # or: python -m unittest discover tests
```

## Statuses
`new` חדש → `sent` נשלח → `replied` ענה → `meeting` נקבעה פגישה; plus `handoff` הועבר לנציג, `not_relevant` לא רלוונטי.
Pasting a customer reply sets `replied` (or `handoff` / `not_relevant` by the draft's intent). Sending a reply → `sent`.

## Conventions
- Python 3.10+, Flask, no frontend build step. Keep it small — ~20 leads/month.
- Agent output is always `Suggestion(message, intent ∈ {answer, meeting, handoff, not_interested}, handoff_reason, slots)`.
- Any AI failure must fall back to the rule engine, never block the salesperson.
- `anthropic` and Google libs are imported lazily so the app runs without them.
- When the real Excel arrives: add its exact headers to `COLUMN_ALIASES` + a test with those headers.
- When the real knowledge arrives: create `knowledge/models.yaml` + `knowledge/business.yaml`
  (drop `_dummy: true`), point `.env` at them, and update the rule-engine tests that assert dummy values.
- When testing with the dummy Excel, do not actually press send in WhatsApp — the fake numbers may belong to real people.

## Status / open items
- [x] Phase 1 skeleton: import, opening drafts, wa.me, paste reply → suggestion, slot proposal, booking, statuses
- [x] Mock calendar; Google Calendar provider written but **not yet tested against a real account**
- [ ] Claude path tested only with a fake LLM — try with a real API key and tune the prompt on real FAQ
- [ ] Real Excel columns, real knowledge, real business hours (waiting on the dealer)
- [ ] Consent question: does the lead form say they'll be contacted on WhatsApp?
