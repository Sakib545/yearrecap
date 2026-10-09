import base64
import json
import os
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

import app.web_checkin as web
from app import services
from app.database import get_db
from app.main import app


def _admin():
    client = TestClient(app)
    session = {"role": "super_admin", "user_name": "Tester", "admin": True}
    client.cookies.set("session", TimestampSigner(os.environ["SESSION_SECRET"]).sign(
        base64.b64encode(json.dumps(session).encode())).decode())
    return client


@pytest.fixture
def staff():
    stamp = int(time.time() * 1000) % 10000000
    staff_id, phone = f"W{stamp}", f"88017{stamp:08d}"
    with get_db() as c:
        c.execute("INSERT INTO employees(staff_id,name,is_active,whatsapp_phone,registration_status) VALUES(?,?,?,?,?)",
                  (staff_id, "Web Tester", True, phone, "approved"))
        employee_id = c.execute("SELECT id FROM employees WHERE staff_id=?", (staff_id,)).fetchone()["id"]
    return {"id": employee_id, "staff_id": staff_id, "phone": phone}


def _pin(admin, employee_id):
    html = admin.post(f"/web-checkin/pin/{employee_id}").text
    return html.split("class='pin'>")[1].split("<")[0]


def _login(staff, pin):
    client = TestClient(app)
    r = client.post("/me/login", data={"staff_id": staff["staff_id"], "pin": pin}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me"
    return client


def test_static_qr_points_to_the_portal(staff):
    admin = _admin()
    svg = admin.get("/checkin/qr.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg") and b"<svg" in svg.content
    assert svg.content == admin.get("/checkin/qr.svg").content                            # fixed: print it once
    poster = admin.get("/checkin/print").text
    assert "<svg" in poster and "/me" in poster and "আমার BURAQ" in poster
    assert TestClient(app).get("/checkin/qr.svg").status_code == 401
    assert TestClient(app).get("/checkin/print").status_code == 401
    old = TestClient(app).get("/checkin?q=anything", follow_redirects=False)                # old QR links still land
    assert old.status_code == 303 and old.headers["location"] == "/me"


def test_pin_rules(staff):
    admin = _admin()
    client = TestClient(app)
    r = client.post("/me/login", data={"staff_id": staff["staff_id"], "pin": "1234"})
    assert r.status_code == 401 and "PIN এখনো দেওয়া হয়নি" in r.text
    pin = _pin(admin, staff["id"])
    wrong = "0000" if pin != "0000" else "1111"
    for _ in range(web.PIN_MAX_FAILURES - 1):
        assert "সঠিক নয়" in client.post("/me/login", data={"staff_id": staff["staff_id"], "pin": wrong}).text
    assert "১৫ মিনিট" in client.post("/me/login", data={"staff_id": staff["staff_id"], "pin": wrong}).text
    assert "১৫ মিনিট" in client.post("/me/login", data={"staff_id": staff["staff_id"], "pin": pin}).text
    assert "locked" in admin.get("/web-checkin").text
    pin = _pin(admin, staff["id"])                                                         # a new PIN unlocks
    assert client.post("/me/login", data={"staff_id": staff["staff_id"].lower(), "pin": pin}, follow_redirects=False).status_code == 303
    assert "সঠিক নয়" in TestClient(app).post("/me/login", data={"staff_id": "NOBODY", "pin": pin}).text


def test_bulk_pins_only_for_people_without_one(staff):
    admin = _admin()
    html = admin.post("/web-checkin/pins/missing").text
    assert staff["staff_id"] in html
    again = admin.post("/web-checkin/pins/missing").text
    assert "New PINs" not in again and "Create PINs for" not in again                        # nobody left without one


def test_check_in_from_the_portal_uses_the_whatsapp_steps(staff, monkeypatch):
    admin = _admin()
    admin.post("/web-checkin/toggle", data={"on": 0})
    client = _login(staff, _pin(admin, staff["id"]))
    page = client.get("/me").text
    assert "WhatsApp-এ" in page and "/me/attendance" not in page                          # off: no button
    admin.post("/web-checkin/toggle", data={"on": 1})
    assert "/me/attendance" in client.get("/me").text
    seen = []
    monkeypatch.setattr(services, "has_face", lambda employee_id: True)

    def fake_image(phone, media_id, image_bytes=None):
        seen.append((phone, media_id[:4], len(image_bytes)))
        current = services.state(phone)
        assert current and current["state"].startswith("checkin_selfie:")          # location step really ran
        services.clear_state(phone)
        return "⏳ Selfie Admin Approval-এ পাঠানো হয়েছে।"

    monkeypatch.setattr(services, "receive_image", fake_image)
    page = client.post("/me/attendance", data={"action": "checkin"}).text
    token = page.split("name='token' value='")[1].split("'")[0]
    assert services.state(staff["phone"])["state"] == "checkin_location"
    r = client.post("/checkin/location", json={"token": token, "latitude": 25.1889, "longitude": 89.8701})
    assert r.status_code == 200 and r.json()["ok"]
    r = client.post("/checkin/selfie", data={"token": token}, files={"photo": ("s.jpg", b"\xff\xd8fakejpeg", "image/jpeg")})
    assert "selfie পাঠানো হয়েছে" in r.text
    assert seen == [(staff["phone"], "web:", 10)]
    assert client.post("/checkin/location", json={"token": "forged", "latitude": 1, "longitude": 1}).status_code == 400
    assert TestClient(app).post("/me/attendance", data={"action": "checkin"}, follow_redirects=False).headers["location"] == "/me/login"
    admin.post("/web-checkin/toggle", data={"on": 0})
    assert "বন্ধ" in client.post("/checkin/location", json={"token": token, "latitude": 25.1, "longitude": 89.8}).json()["message"]


def test_portal_shows_only_own_records(staff):
    admin = _admin()
    client = _login(staff, _pin(admin, staff["id"]))
    today = services.now_local().date()
    with get_db() as c:
        c.execute("INSERT INTO employees(staff_id,name,is_active) VALUES(?,?,?)", (staff["staff_id"] + "X", "Someone Else", True))
        other = c.execute("SELECT id FROM employees WHERE staff_id=?", (staff["staff_id"] + "X",)).fetchone()["id"]
        c.execute("INSERT INTO attendance(employee_id,work_date,check_in,late_minutes) VALUES(?,?,?,?)",
                  (staff["id"], today.isoformat(), services.now_local().isoformat(timespec="seconds"), 12))
        c.execute("INSERT INTO custom_duties(employee_id,duty_date,start_time,end_time,note) VALUES(?,?,?,?,?)",
                  (staff["id"], (today + timedelta(days=2)).isoformat(), "10:00", "16:00", "Stock count"))
        c.execute("INSERT INTO leave_requests(employee_id,leave_type,start_date,end_date,reason) VALUES(?,?,?,?,?)",
                  (other, "Sick", today.isoformat(), today.isoformat(), "other person's leave"))
    page = client.get("/me").text
    assert "Duty চলছে" in page and "12 মিনিট দেরি" in page
    assert "Stock count" in page and "10:00 AM" in page
    assert "Someone Else" not in page and "এখনো কোনো ছুটির আবেদন নেই" in page
    assert client.get("/web-checkin").status_code == 401                                   # an employee is not HR


def test_leave_and_correction_requests(staff):
    client = _login(staff, _pin(_admin(), staff["id"]))
    today = services.now_local().date()
    start = (today + timedelta(days=3)).isoformat()
    r = client.post("/me/leave", data={"leave_type": "Casual", "start_date": start, "end_date": start, "reason": "Family"})
    assert "HR-এর কাছে পাঠানো হয়েছে" in r.text and "অপেক্ষায়" in r.text
    assert "আগেই একটি ছুটির আবেদন" in client.post("/me/leave", data={"leave_type": "Casual", "start_date": start, "end_date": start, "reason": "Again"}).text
    assert "শেষ তারিখ" in client.post("/me/leave", data={"leave_type": "Sick", "start_date": start, "end_date": today.isoformat(), "reason": "x"}).text
    day = (today - timedelta(days=1)).isoformat()
    r = client.post("/me/correction", data={"work_date": day, "check_out": "18:05", "reason": "Forgot to check out"})
    assert "অনুরোধ HR-এর কাছে পাঠানো হয়েছে" in r.text
    assert "আগেই HR-এর কাছে আছে" in client.post("/me/correction", data={"work_date": day, "check_out": "18:00", "reason": "x"}).text
    assert "গত ৩০ দিনের" in client.post("/me/correction", data={"work_date": (today + timedelta(days=1)).isoformat(), "check_in": "09:00", "reason": "x"}).text
    with get_db() as c:
        row = c.execute("SELECT requested_check_out,requested_by,status FROM attendance_corrections WHERE employee_id=?", (staff["id"],)).fetchone()
        leave = c.execute("SELECT requested_by FROM leave_requests WHERE employee_id=?", (staff["id"],)).fetchone()
    assert dict(row) == {"requested_check_out": "18:05", "requested_by": "employee:portal", "status": "pending"}
    assert leave["requested_by"] == "employee:portal"


def test_salary_needs_pin_again_and_only_final_months(staff):
    admin = _admin()
    pin = _pin(admin, staff["id"])
    client = _login(staff, pin)
    with get_db() as c:
        for month, status, net in (("2026-08", "paid", 15000), ("2026-09", "draft", 99999)):
            c.execute("INSERT INTO payroll_records(employee_id,salary_month,fixed_salary,net_salary,payment_status) VALUES(?,?,?,?,?)",
                      (staff["id"], month, net, net, status))
        paid = c.execute("SELECT id FROM payroll_records WHERE employee_id=? AND salary_month='2026-08'", (staff["id"],)).fetchone()["id"]
        draft = c.execute("SELECT id FROM payroll_records WHERE employee_id=? AND salary_month='2026-09'", (staff["id"],)).fetchone()["id"]
    page = client.get("/me").text
    assert "15,000" not in page and "বেতন দেখতে আবার PIN দিন" in page
    assert client.get(f"/me/payslip/{paid}.pdf", follow_redirects=False).status_code == 303
    assert "PIN সঠিক নয়" in client.post("/me/salary/unlock", data={"pin": "0000" if pin != "0000" else "1111"}).text
    page = client.post("/me/salary/unlock", data={"pin": pin}).text
    assert "৳15,000" in page and "99,999" not in page
    pdf = client.get(f"/me/payslip/{paid}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.get(f"/me/payslip/{draft}.pdf").status_code == 404
    other = _login(staff, pin)                                                            # another person's payslip is refused
    with get_db() as c:
        c.execute("INSERT INTO employees(staff_id,name,is_active) VALUES(?,?,?)", (staff["staff_id"] + "Y", "Other", True))
        other_id = c.execute("SELECT id FROM employees WHERE staff_id=?", (staff["staff_id"] + "Y",)).fetchone()["id"]
        c.execute("INSERT INTO payroll_records(employee_id,salary_month,fixed_salary,net_salary,payment_status) VALUES(?,?,?,?,?)",
                  (other_id, "2026-08", 1, 1, "paid"))
        theirs = c.execute("SELECT id FROM payroll_records WHERE employee_id=?", (other_id,)).fetchone()["id"]
    other.post("/me/salary/unlock", data={"pin": pin})
    assert other.get(f"/me/payslip/{theirs}.pdf").status_code == 404


def test_changing_the_pin_signs_out_other_phones(staff):
    admin = _admin()
    pin = _pin(admin, staff["id"])
    phone_a, phone_b = _login(staff, pin), _login(staff, pin)
    assert "এই PIN খুব সহজ" in phone_a.post("/me/pin", data={"current": pin, "new": "1111", "confirm": "1111"}).text
    assert "মেলেনি" in phone_a.post("/me/pin", data={"current": pin, "new": "4826", "confirm": "4827"}).text
    assert "PIN বদলানো হয়েছে" in phone_a.post("/me/pin", data={"current": pin, "new": "4826", "confirm": "4826"}).text
    assert phone_a.get("/me", follow_redirects=False).status_code == 200
    assert phone_b.get("/me", follow_redirects=False).headers["location"] == "/me/login"
    _login(staff, "4826")
    _pin(admin, staff["id"])                                                               # HR reset ends phone A too
    assert phone_a.get("/me", follow_redirects=False).headers["location"] == "/me/login"


def test_permissions_and_pwa():
    viewer = TestClient(app)
    session = {"role": "viewer", "user_name": "V", "hr_id": 987654}
    viewer.cookies.set("session", TimestampSigner(os.environ["SESSION_SECRET"]).sign(
        base64.b64encode(json.dumps(session).encode())).decode())
    assert viewer.get("/web-checkin").status_code == 403
    assert viewer.post("/web-checkin/toggle", data={"on": 1}).status_code == 403
    assert viewer.post("/web-checkin/pin/1").status_code == 403
    assert viewer.post("/web-checkin/pins/missing").status_code == 403
    assert TestClient(app).get("/web-checkin").status_code == 401
    assert TestClient(app).get("/me", follow_redirects=False).headers["location"] == "/me/login"
    assert json.loads(TestClient(app).get("/me/manifest.webmanifest").content)["start_url"] == "/me"
    assert TestClient(app).get("/me/icon.svg").content.startswith(b"<svg")
