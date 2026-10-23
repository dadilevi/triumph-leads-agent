# Triumph Leads Agent

טייס משנה לפנייה ללידים בוואטסאפ עבור סוכנות אופנועי Triumph. אפיון מלא: [SPEC.md](SPEC.md).

**שלב 1 (נוכחי):** אפליקציה מקומית בדפדפן. מעלים Excel של לידים → מקבלים הודעת פתיחה מותאמת →
לוחצים "פתח בוואטסאפ" (קישור wa.me, איש המכירות לוחץ שלח) → מדביקים את תשובת הלקוח → מקבלים הצעת
תשובה מבוססת קובץ ידע בלבד → מציעים מועדים פנויים מהיומן → לחיצה על מועד יוצרת אירוע.

> ⚠️ כרגע רץ על **נתוני דמה** (`knowledge/*.dummy.yaml`, `samples/leads_dummy.xlsx`) עד שיגיעו החומרים מהסוכנות.

## הרצה

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # אופציונלי: ANTHROPIC_API_KEY להפעלת Claude
python run.py             # http://localhost:5050
```

בלי מפתח API האפליקציה עובדת ב"מצב ללא AI": תבנית פתיחה + זיהוי שאלות לפי מילות מפתח, והעברה לנציג בכל מקרה של ספק.

## בדיקות

```bash
python -m pytest
```

## Google Calendar (כשמוכנים)

1. ב-Google Cloud Console: ליצור פרויקט, להפעיל Google Calendar API, ליצור OAuth Client מסוג **Desktop app**.
2. להוריד את קובץ ה-JSON ל-`data/credentials.json` (לא נכנס ל-git).
3. ב-`.env`: `CALENDAR_PROVIDER=google` (ו-`GOOGLE_CALENDAR_ID` אם זה לא היומן הראשי).
4. בהפעלה הראשונה ייפתח דפדפן לאישור; הטוקן נשמר ב-`data/token.json`.

## פרטיות

כל המידע על הלידים נשמר מקומית ב-`data/` (מוחרג מ-git). האפליקציה מאזינה רק ל-`127.0.0.1`.
