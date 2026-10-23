"""Availability + event creation. MockCalendar (local JSON) or GoogleCalendar."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .knowledge import WEEKDAYS

HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # Python weekday() order


@dataclass
class Slot:
    start: datetime
    end: datetime

    def label(self) -> str:
        return f"יום {HEB_DAYS[self.start.weekday()]} {self.start:%d/%m} בשעה {self.start:%H:%M}"

    def key(self) -> str:
        return self.start.isoformat()


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def find_free_slots(
    busy: list[tuple[datetime, datetime]],
    meetings_cfg: dict,
    tz: ZoneInfo,
    now: datetime | None = None,
    step_minutes: int = 30,
) -> list[Slot]:
    """Propose up to N slots, at most one per day, alternating morning/afternoon for variety."""
    now = now or datetime.now(tz)
    duration = timedelta(minutes=int(meetings_cfg.get("duration_minutes", 45)))
    notice = timedelta(hours=float(meetings_cfg.get("min_notice_hours", 3)))
    days = int(meetings_cfg.get("search_days", 7))
    want = int(meetings_cfg.get("slots_to_offer", 3))
    hours: dict = meetings_cfg.get("hours", {})
    preferred = [time(10, 0), time(16, 0), time(12, 0)]

    earliest = now + notice
    out: list[Slot] = []
    for d in range(days + 1):
        if len(out) >= want:
            break
        day: date = (now + timedelta(days=d)).date()
        rng = hours.get(WEEKDAYS[day.weekday()])
        if not rng:
            continue
        open_t = datetime.combine(day, time.fromisoformat(rng[0]), tz)
        close_t = datetime.combine(day, time.fromisoformat(rng[1]), tz)
        candidates = []
        t = open_t
        while t + duration <= close_t:
            if t >= earliest and not any(_overlaps(t, t + duration, b0, b1) for b0, b1 in busy):
                candidates.append(t)
            t += timedelta(minutes=step_minutes)
        if not candidates:
            continue
        pref = preferred[len(out) % len(preferred)]
        pick = next((c for c in candidates if c.time() >= pref), candidates[-1])
        out.append(Slot(pick, pick + duration))
    return out


class MockCalendar:
    """Fake calendar stored in data/mock_calendar.json — seeded with a few busy blocks."""

    name = "יומן דמה (מקומי)"

    def __init__(self, path: Path, tz: ZoneInfo):
        self.path = Path(path)
        self.tz = tz
        if not self.path.exists():
            self._seed()

    def _seed(self) -> None:
        base = datetime.now(self.tz).replace(minute=0, second=0, microsecond=0)
        events = []
        for d, h, length in [(0, 14, 2), (1, 9, 3), (1, 16, 1), (2, 11, 2), (3, 15, 3)]:
            s = (base + timedelta(days=d)).replace(hour=h)
            events.append({"id": f"seed-{d}-{h}", "title": "תפוס (דמה)", "start": s.isoformat(),
                           "end": (s + timedelta(hours=length)).isoformat()})
        self._save(events)

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def _save(self, events: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        out = []
        for e in self._load():
            s, en = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
            if _overlaps(s, en, start, end):
                out.append((s, en))
        return out

    def create_event(self, title: str, start: datetime, end: datetime, description: str = "") -> str:
        events = self._load()
        eid = "mock-" + uuid.uuid4().hex[:8]
        events.append({"id": eid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
                       "description": description})
        self._save(events)
        return eid


class GoogleCalendar:
    """Google Calendar via OAuth (Desktop app client). First run opens a browser to authorize.

    Not exercised against a real account yet — verify once the dealer's calendar is available.
    """

    name = "Google Calendar"
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, credentials_file: Path, token_file: Path, calendar_id: str, tz: ZoneInfo):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if Path(token_file).exists():
            creds = Credentials.from_authorized_user_file(str(token_file), self.SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not Path(credentials_file).exists():
                    raise FileNotFoundError(
                        f"חסר קובץ OAuth של Google: {credentials_file}. ראה README → Google Calendar."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), self.SCOPES)
                creds = flow.run_local_server(port=0)
            Path(token_file).write_text(creds.to_json(), encoding="utf-8")
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.calendar_id = calendar_id
        self.tz = tz

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]}
        resp = self.svc.freebusy().query(body=body).execute()
        periods = resp["calendars"][self.calendar_id]["busy"]
        return [
            (datetime.fromisoformat(p["start"].replace("Z", "+00:00")).astimezone(self.tz),
             datetime.fromisoformat(p["end"].replace("Z", "+00:00")).astimezone(self.tz))
            for p in periods
        ]

    def create_event(self, title: str, start: datetime, end: datetime, description: str = "") -> str:
        ev = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": str(self.tz)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(self.tz)},
        }
        return self.svc.events().insert(calendarId=self.calendar_id, body=ev).execute()["id"]


def make_calendar(cfg, tz: ZoneInfo):
    if cfg.calendar_provider == "google":
        return GoogleCalendar(cfg.google_credentials_file, cfg.google_token_file, cfg.google_calendar_id, tz)
    return MockCalendar(cfg.mock_calendar_file, tz)


def propose_slots(calendar, meetings_cfg: dict, tz: ZoneInfo, now: datetime | None = None) -> list[Slot]:
    now = now or datetime.now(tz)
    horizon = now + timedelta(days=int(meetings_cfg.get("search_days", 7)) + 1)
    return find_free_slots(calendar.busy(now, horizon), meetings_cfg, tz, now=now)
