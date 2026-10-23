# Triumph Leads Agent

סוכן וואטסאפ ללידים של סוכנות אופנועי Triumph: פונה ללידים, עונה על שאלות מתוך קובץ ידע מאושר בלבד,
מציע מועדים פנויים מהיומן, קובע נסיעת מבחן ומעביר לנציג כשצריך. אפיון: [SPEC.md](SPEC.md).

> ⚠️ כרגע רץ על **נתוני דמה** (`knowledge/*.dummy.yaml`, `samples/leads_dummy.xlsx`) עד שיגיעו החומרים מהסוכנות.

## הרצה

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py             # http://localhost:5050
```

## שלושה מתגים ב-`.env`

| מתג | אפשרויות |
|---|---|
| `CHANNEL` | `simulator` — אתה משחק את הלקוח ב-/simulator · `wame` — איש המכירות שולח ידנית דרך קישור wa.me · `cloud` — WhatsApp Business Cloud API (לקוחות אמיתיים) |
| `AGENT_MODE` | `supervised` — הסוכן מנסח, נציג מאשר כל הודעה ולוחץ לקביעת פגישה · `auto` — הסוכן עונה וקובע לבד |
| `ANTHROPIC_API_KEY` | ריק = מנוע חוקים פשוט (תבניות + מילות מפתח, מעביר לנציג בכל ספק). עם מפתח = Claude |

**לנסות את הסוכן עכשיו:** `CHANNEL=simulator`, `AGENT_MODE=auto` → פותחים /simulator, יוצרים לקוח בדיקה ומדברים איתו.

## העברה לנציג

- הסוכן מעביר לנציג כשאין לו מידע מאושר, כשהלקוח מבקש אדם, או כשמשהו לא ברור. השיחה מסומנת "אצל נציג" והוא מפסיק לענות בה.
- נציג יכול לקחת שיחה בכל רגע (כפתור "אני לוקח את השיחה"), ולהחזיר אותה לסוכן.
- ב-Cloud API במצב Coexistence: אם מנהל התצוגה עונה מאפליקציית WhatsApp Business בטלפון, הסוכן מזהה ונעצר בשיחה הזו.
- כל פגישה / העברה / תקלה מופיעה ב"התראות".

## חיבור ל-WhatsApp Business Cloud API (כשמוכנים)

1. חשבון Meta Business של הסוכנות + אפליקציה ב-developers.facebook.com עם מוצר WhatsApp.
2. חיבור המספר העסקי. אם הוא כבר באפליקציית WhatsApp Business — לבדוק אם Coexistence זמין בישראל (משאיר את המספר גם באפליקציה).
3. תבנית פתיחה לאישור מטא, עם `{{1}}` = שם פרטי ו-`{{2}}` = דגם. אותו נוסח כמו `opening_template` בקובץ העסק.
4. ב-`.env`: `CHANNEL=cloud`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TOKEN` (System User token קבוע), `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_OPENING_TEMPLATE`.
5. חשיפת ה-webhook בלבד דרך מנהרה, למשל `cloudflared tunnel --url http://127.0.0.1:5050`, והגדרת `https://<tunnel>/webhook/whatsapp` במטא (שדות: `messages`, ובמצב Coexistence גם `smb_message_echoes`).
6. להתחיל ב-`AGENT_MODE=supervised`, ורק אחרי שהתשובות טובות לעבור ל-`auto`.

מגבלות של מטא: הודעה ראשונה לליד = תבנית מאושרת בלבד; הודעה חופשית רק תוך 24 שעות מההודעה האחרונה של הלקוח.

## Google Calendar

1. ב-Google Cloud Console: פרויקט, להפעיל Google Calendar API, OAuth Client מסוג **Desktop app**.
2. קובץ ה-JSON ל-`data/credentials.json` (לא נכנס ל-git).
3. ב-`.env`: `CALENDAR_PROVIDER=google` (ו-`GOOGLE_CALENDAR_ID` אם זה לא היומן הראשי).
4. בהפעלה הראשונה ייפתח דפדפן לאישור; הטוקן נשמר ב-`data/token.json`.

## בדיקות

```bash
python -m pytest
```

## פרטיות

כל המידע על הלידים נשמר מקומית ב-`data/` (מוחרג מ-git). הממשק מאזין רק ל-`127.0.0.1`; כלפי חוץ נחשף רק ה-webhook, עם אימות חתימה של מטא.
