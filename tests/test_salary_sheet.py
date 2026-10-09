import base64
import io
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from openpyxl import Workbook

from app import ai_insights
from app.database import get_db
from app.main import app

MONTH = "2026-05"


def _client(role="super_admin"):
    client = TestClient(app)
    session = {"role": role, "user_name": "HR Tester", "admin": True} if role == "super_admin" else \
        {"role": role, "user_name": "V", "hr_id": 987654}
    client.cookies.set("session", TimestampSigner(os.environ["SESSION_SECRET"]).sign(
        base64.b64encode(json.dumps(session).encode())).decode())
    return client


@pytest.fixture
def people():
    stamp = int(time.time() * 1000) % 1000000
    ids = {}
    with get_db() as c:
        for key in ("draft", "paid", "none", "zero"):
            staff = f"S{stamp}{key[:2].upper()}"
            c.execute("INSERT INTO employees(staff_id,name,is_active) VALUES(?,?,?)", (staff, f"Emp {key}", True))
            ids[key] = (c.execute("SELECT id FROM employees WHERE staff_id=?", (staff,)).fetchone()["id"], staff)
        c.execute("INSERT INTO payroll_records(employee_id,salary_month,fixed_salary,net_salary,payment_status) VALUES(?,?,?,?,?)",
                  (ids["draft"][0], MONTH, 10500, 14990, "draft"))
        c.execute("INSERT INTO payroll_records(employee_id,salary_month,fixed_salary,net_salary,payment_status) VALUES(?,?,?,?,?)",
                  (ids["paid"][0], MONTH, 9500, 9000, "paid"))
    return ids


def _xlsx(people, total_override=None):
    book = Workbook()
    ws = book.active
    ws.append(["Buraq Consumar"])
    ws.append(["Salary Sheet"])
    ws.append(["Month: May"])
    ws.append(["SL", "Name", "Designation", "Staff ID", "Mobile", "Basic", "Night", "Friday", "Present", "Absent", "Leave",
               "Grs Salary", "Absent Duduction", "Over Time", "Extra", "Net Salary", "Total Salary"])
    rows = [
        (1, "Emp draft", "SE", people["draft"][1], "017", 10500, 1260, 1400, 26, 0, 5, 13160, 0, 1800, 50, 15010, 15000),
        (2, "Emp paid", "SE", people["paid"][1], "018", 9500, 0, 1050, 26, 0, 5, 10550, 2100, 900, 0, 9350, 9300),
        (3, "Emp none", "SE", people["none"][1], "019", 9500, 1260, 1400, 26, 0, 5, 12160, 0, 2250, 70, 14480, 14400),
        (4, "Emp zero", "MD", people["zero"][1], "020", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (5, "Stranger", "SE", "B999999", "021", 9500, 0, 0, 26, 0, 5, 9500, 0, 0, 0, 9500, 9500),
    ]
    for row in rows:
        ws.append(list(row))
    ws.append([None] * 16 + [total_override or 48200])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _upload(client, content, name="may.xlsx"):
    r = client.post("/payroll/sheet", data={"month": MONTH}, files={"sheet": (name, content)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def test_excel_preview_then_confirm_then_undo(people):
    admin = _client()
    review = _upload(admin, _xlsx(people))
    import_id = int(review.rsplit("/", 1)[1])
    page = admin.get(review).text
    assert "3 will be marked Paid" not in page                                     # paid + zero + stranger are skipped
    assert "2 will be marked Paid" in page and "৳29,400" in page
    assert "আগেই Paid" in page and "টাকা ০" in page and "employee নেই" in page
    assert "পার্থক্য ৳10" in page                                                   # system 14,990 vs sheet 15,000
    assert "নিচের মোট" not in page                                                   # printed total matches the rows
    with get_db() as c:
        assert c.execute("SELECT payment_status FROM payroll_records WHERE employee_id=?", (people["draft"][0],)).fetchone()["payment_status"] == "draft"

    picks = page.count("name='pick'")
    assert picks == 2
    r = admin.post(f"/payroll/sheet/{import_id}/apply", data={"pick": ["0", "2"], "method": "Cash", "reference": "May sheet"})
    assert "2 জনের বেতন Paid" in r.text
    with get_db() as c:
        draft = c.execute("SELECT * FROM payroll_records WHERE employee_id=? AND salary_month=?", (people["draft"][0], MONTH)).fetchone()
        made = c.execute("SELECT * FROM payroll_records WHERE employee_id=? AND salary_month=?", (people["none"][0], MONTH)).fetchone()
        untouched = c.execute("SELECT net_salary FROM payroll_records WHERE employee_id=?", (people["paid"][0],)).fetchone()
        log = c.execute("SELECT reason FROM payroll_change_logs WHERE payroll_id=? ORDER BY id DESC", (draft["id"],)).fetchone()
    assert (draft["payment_status"], draft["net_salary"], draft["payment_reference"]) == ("paid", 15000, "May sheet")
    assert made["payment_status"] == "paid" and made["net_salary"] == 14400 and made["night_allowance"] == 1260
    assert untouched["net_salary"] == 9000
    assert "14,990" in log["reason"] and "15,000" in log["reason"]
    pdf = admin.get(f"/payroll/{made['id']}/payslip.pdf")                           # imported records still print
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert admin.post(f"/payroll/sheet/{import_id}/apply", data={"pick": ["0"], "reference": "x"}).status_code == 409

    r = admin.post(f"/payroll/sheet/{import_id}/undo")
    assert "2টি record আগের অবস্থায়" in r.text
    with get_db() as c:
        draft = c.execute("SELECT payment_status,net_salary FROM payroll_records WHERE employee_id=? AND salary_month=?", (people["draft"][0], MONTH)).fetchone()
        gone = c.execute("SELECT 1 FROM payroll_records WHERE employee_id=? AND salary_month=?", (people["none"][0], MONTH)).fetchone()
    assert (draft["payment_status"], draft["net_salary"]) == ("draft", 14990) and gone is None
    assert admin.get(f"/payroll/sheet/{import_id}/file").content[:2] == b"PK"            # the sheet is kept


def test_wrong_total_and_wrong_month_are_flagged(people):
    admin = _client()
    page = admin.get(_upload(admin, _xlsx(people, total_override=50000))).text
    assert "নিচের মোট ৳50,000" in page
    r = admin.post("/payroll/sheet", data={"month": "2026-06"}, files={"sheet": ("may.xlsx", _xlsx(people))}, follow_redirects=True)
    assert "Sheet-এ মাস লেখা &#x27;May&#x27;" in r.text


def test_photo_is_read_by_ai_and_bad_rows_are_flagged(people, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    sent = {}

    def fake_post(url, headers, body):
        sent["parts"] = body["contents"][0]["parts"]
        answer = {"month_label": "May", "printed_total": 15000, "rows": [
            {"name": "Emp draft", "staff_id": people["draft"][1].lower(), "gross": 13160, "absent_deduction": 0,
             "overtime": 1800, "extra": 50, "net": 15010, "total": 15000},
            {"name": "Emp none", "staff_id": people["none"][1], "gross": 12160, "absent_deduction": 0,
             "overtime": 2250, "extra": 70, "net": 14880, "total": 14800}]}            # misread: 14480 → 14880
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}], "usageMetadata": {}}

    monkeypatch.setattr(ai_insights, "_post", fake_post)
    admin = _client()
    page = admin.get(_upload(admin, b"\xff\xd8\xff\xe0fakejpeg", "sheet.jpg")).text
    assert sent["parts"][0]["inline_data"]["mime_type"] == "image/jpeg"
    assert "AI পড়েছে" in page and "number মিলিয়ে দেখুন" in page and "নিচের মোট" in page
    assert page.count("name='pick'") == 2                                            # lower-case Staff ID still matches


def test_permissions_and_bad_files(people):
    viewer = _client("viewer")
    assert viewer.get("/payroll/sheet").status_code == 403
    assert viewer.post("/payroll/sheet", data={"month": MONTH}, files={"sheet": ("a.xlsx", b"PK")}).status_code == 403
    assert TestClient(app).get("/payroll/sheet").status_code == 401
    admin = _client()
    r = admin.post("/payroll/sheet", data={"month": MONTH}, files={"sheet": ("a.txt", b"hello")}, follow_redirects=True)
    assert "শুধু .xlsx" in r.text
    r = admin.post("/payroll/sheet", data={"month": MONTH}, files={"sheet": ("a.xlsx", b"PK not really")}, follow_redirects=True)
    assert "Excel file খোলা যায়নি" in r.text
