"""Run: python -m pytest   (or: python -m unittest discover tests)"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agent import Agent
from app.calendar_service import MockCalendar, find_free_slots
from app.config import ROOT, Config
from app.excel_import import map_columns, parse_leads_excel
from app.knowledge import Knowledge
from app.phone import display_phone, normalize_il_phone, wa_link

TZ = ZoneInfo("Asia/Jerusalem")
KN = Knowledge.load(ROOT / "knowledge/models.dummy.yaml", ROOT / "knowledge/business.dummy.yaml")
SAMPLE = ROOT / "samples/leads_dummy.xlsx"


class TestPhone(unittest.TestCase):
    def test_formats(self):
        for raw in ["050-123-4567", "0501234567", "+972 50 123 4567", "972501234567", 501234567, 501234567.0, "00972501234567"]:
            self.assertEqual(normalize_il_phone(raw), "972501234567", raw)

    def test_invalid(self):
        for raw in [None, "", "123", "abc", float("nan")]:
            self.assertIsNone(normalize_il_phone(raw))

    def test_display_and_link(self):
        self.assertEqual(display_phone("972501234567"), "050-123-4567")
        link = wa_link("972501234567", "היי יוסי & שלום")
        self.assertTrue(link.startswith("https://wa.me/972501234567?text="))
        self.assertNotIn(" ", link)
        self.assertIn("%26", link)


class TestExcel(unittest.TestCase):
    def test_map_columns_hebrew_doesnt_confuse_name_and_surname(self):
        cols = map_columns(["שם", "שם משפחה", "מייל", "טלפון", "מודל"])
        self.assertEqual(cols, {"first_name": 0, "last_name": 1, "email": 2, "phone": 3, "model": 4})

    def test_map_columns_english(self):
        cols = map_columns(["First Name", "Last Name", "E-mail", "Mobile", "Interested Model"])
        self.assertEqual(cols["first_name"], 0)
        self.assertEqual(cols["last_name"], 1)
        self.assertEqual(cols["phone"], 3)
        self.assertEqual(cols["model"], 4)

    def test_parse_sample(self):
        res = parse_leads_excel(SAMPLE)
        self.assertEqual(res.missing_fields, [])
        self.assertEqual(len(res.leads), 8)
        noa = next(l for l in res.leads if l.first_name == "נועה")
        self.assertEqual(noa.phone, "972530000004")  # leading 0 dropped by Excel
        self.assertTrue(any("אין מספר טלפון" in w for w in res.warnings))


class TestKnowledge(unittest.TestCase):
    def test_find_model(self):
        self.assertEqual(KN.find_model("טייגר 900").id, "tiger-900")
        self.assertEqual(KN.find_model("Trident").id, "trident-660")
        self.assertEqual(KN.find_model("street triple 765 RS").id, "street-triple-765")
        self.assertIsNone(KN.find_model("Rocket 3"))

    def test_unknown_fields_marked_for_handoff(self):
        ctx = KN.prompt_context(KN.find_model("Street Triple"))
        self.assertIn("מחיר: לא ידוע — להעביר לנציג", ctx)


def _slots(now):
    return find_free_slots([], KN.meetings, TZ, now=now)


class TestSlots(unittest.TestCase):
    def test_respects_hours_notice_and_saturday(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=TZ)  # Friday noon: closes 13:00, notice 3h
        slots = _slots(now)
        self.assertEqual(len(slots), 3)
        self.assertTrue(all(s.start.weekday() != 5 for s in slots))  # no Saturday
        self.assertEqual(slots[0].start.date().isoformat(), "2026-10-25")  # Sunday
        for s in slots:
            self.assertGreaterEqual(s.start.hour, 9)
            self.assertLessEqual(s.end.hour * 60 + s.end.minute, 18 * 60)

    def test_avoids_busy(self):
        now = datetime(2026, 10, 25, 7, 0, tzinfo=TZ)  # Sunday morning
        busy = [(datetime(2026, 10, 25, 9, 0, tzinfo=TZ), datetime(2026, 10, 25, 18, 0, tzinfo=TZ))]
        slots = find_free_slots(busy, KN.meetings, TZ, now=now)
        self.assertNotEqual(slots[0].start.date().isoformat(), "2026-10-25")

    def test_mock_calendar_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            cal = MockCalendar(Path(d) / "c.json", TZ)
            s = datetime(2030, 1, 1, 10, 0, tzinfo=TZ)
            e = datetime(2030, 1, 1, 10, 45, tzinfo=TZ)
            cal.create_event("x", s, e)
            self.assertEqual(len(cal.busy(s, e)), 1)


class TestRuleAgent(unittest.TestCase):
    agent = Agent(KN, llm=None)
    slots = _slots(datetime(2026, 10, 25, 7, 0, tzinfo=TZ))

    def reply(self, text, model="Trident 660"):
        return self.agent.suggest_reply("יוסי", KN.find_model(model), [("out", "היי"), ("in", text)], self.slots)

    def test_opening_template(self):
        s = self.agent.opening("יוסי", KN.find_model("Trident 660"), "Trident 660")
        self.assertIn("יוסי", s.message)
        self.assertIn("Trident 660", s.message)
        self.assertFalse(s.used_ai)

    def test_known_price_answers(self):
        s = self.reply("כמה הוא עולה?")
        self.assertEqual(s.intent, "answer")
        self.assertIn("₪00,000 (דמה)", s.message)

    def test_unknown_price_hands_off_never_invents(self):
        s = self.reply("כמה עולה?", model="Street Triple 765")
        self.assertEqual(s.intent, "handoff")
        self.assertNotIn("₪", s.message)

    def test_meeting_offers_slots(self):
        s = self.reply("אשמח לבוא לנסיעת מבחן")
        self.assertEqual(s.intent, "meeting")
        self.assertEqual(len(s.slots), 3)

    def test_not_interested(self):
        self.assertEqual(self.reply("לא מעוניין תודה").intent, "not_interested")

    def test_asks_for_human(self):
        self.assertEqual(self.reply("אפשר שנציג יתקשר אליי?").intent, "handoff")

    def test_faq_without_approved_answer_hands_off(self):
        self.assertEqual(self.reply("יש אפשרות למימון?").intent, "handoff")

    def test_gibberish_hands_off(self):
        self.assertEqual(self.reply("מה דעתך על מזג האוויר").intent, "handoff")


class TestAIAgent(unittest.TestCase):
    def test_uses_tool_output_and_passes_knowledge(self):
        seen = {}

        def fake_llm(system, messages, tool):
            seen["prompt"] = messages[0]["content"]
            return {"message": "היי יוסי! בוא לנסות 🙂", "intent": "meeting"}

        agent = Agent(KN, llm=fake_llm)
        slots = _slots(datetime(2026, 10, 25, 7, 0, tzinfo=TZ))
        s = agent.suggest_reply("יוסי", KN.find_model("Trident"), [("in", "אפשר לנסות?")], slots)
        self.assertTrue(s.used_ai)
        self.assertEqual(s.intent, "meeting")
        self.assertEqual(len(s.slots), 3)
        self.assertIn("Trident 660", seen["prompt"])
        self.assertIn(slots[0].label(), seen["prompt"])

    def test_ai_failure_falls_back(self):
        def broken(*a):
            raise RuntimeError("boom")

        s = Agent(KN, llm=broken).suggest_reply("יוסי", None, [("in", "לא מעוניין")], [])
        self.assertFalse(s.used_ai)
        self.assertEqual(s.intent, "not_interested")
        self.assertIn("boom", s.handoff_reason)


class TestWebFlow(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        cfg = Config(db_path=d / "t.db", mock_calendar_file=d / "cal.json")
        self.app = create_app(cfg, llm=None, calendar=MockCalendar(d / "cal.json", TZ))
        self.c = self.app.test_client()
        self.db = self.app.extensions["tla"]["db"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow(self):
        with open(SAMPLE, "rb") as f:
            r = self.c.post("/import", data={"file": (f, "leads.xlsx")}, content_type="multipart/form-data",
                            follow_redirects=True)
        self.assertIn("נוספו 7 לידים חדשים", r.get_data(as_text=True))  # 8 rows, 1 duplicate
        lead = next(l for l in self.db.list_leads() if l["first_name"] == "יוסי")
        lid = lead["id"]

        page = self.c.get(f"/lead/{lid}").get_data(as_text=True)
        self.assertIn("הודעת פתיחה", page)
        self.assertIn("https://wa.me/972500000001", page)

        self.c.post(f"/lead/{lid}/sent", data={"text": "היי יוסי"})
        self.assertEqual(self.db.get_lead(lid)["status"], "sent")

        self.c.post(f"/lead/{lid}/incoming", data={"text": "אשמח לנסיעת מבחן"})
        lead = self.db.get_lead(lid)
        self.assertEqual(lead["status"], "replied")
        offered = json.loads(lead["offered_slots"])
        self.assertEqual(len(offered), 3)

        self.c.post(f"/lead/{lid}/sent", data={"text": "יש לי ..."})
        self.c.post(f"/lead/{lid}/book", data={"start": offered[0]["start"], "end": offered[0]["end"]})
        lead = self.db.get_lead(lid)
        self.assertEqual(lead["status"], "meeting")
        self.assertIsNone(lead["offered_slots"])
        self.assertEqual(len(self.db.meetings(lid)), 1)
        self.assertIn("סגרנו", json.loads(lead["pending_suggestion"])["message"])

    def test_index_renders_empty(self):
        self.assertIn("אין עדיין לידים", self.c.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
