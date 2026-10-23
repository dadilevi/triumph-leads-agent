"""Generate samples/leads_dummy.xlsx — FAKE leads for local testing.

Column names are a guess until the dealer sends a real export; edge cases are included
on purpose (phone formats, unknown model, missing phone, duplicate).
"""
from pathlib import Path

from openpyxl import Workbook

ROWS = [
    ("שם", "שם משפחה", "מייל", "טלפון", "מודל"),
    ("יוסי", "כהן", "yossi.dummy@example.com", "050-000-0001", "Trident 660"),
    ("מיכל", "לוי", "michal.dummy@example.com", "0520000002", "טייגר 900"),
    ("אבי", "מזרחי", "avi.dummy@example.com", "+972 54 000 0003", "Street Triple 765"),
    ("נועה", "פרץ", "noa.dummy@example.com", 530000004, "Speed 400"),          # Excel number, leading 0 lost
    ("רון", "ביטון", "ron.dummy@example.com", "972-58-000-0005", "Bonneville T120"),
    ("דנה", "אברהם", "dana.dummy@example.com", "050 000 0006", "Rocket 3"),     # not in knowledge file
    ("עומר", "שלום", "omer.dummy@example.com", "", "Trident"),                    # missing phone
    ("יוסי", "כהן", "yossi.dummy@example.com", "0500000001", "Trident 660"),     # duplicate
]


def main(out: Path = Path(__file__).resolve().parent.parent / "samples" / "leads_dummy.xlsx") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    for r in ROWS:
        ws.append(list(r))
    wb.save(out)
    return out


if __name__ == "__main__":
    print(f"wrote {main()}")
