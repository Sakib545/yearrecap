"""Web Check In: Check In / Check Out from a phone browser when WhatsApp is down.

HR turns it on and off. A printed office QR opens the employee portal (/me);
the employee signs in with Staff ID and PIN and then goes through exactly the
WhatsApp steps: office location, selfie, Face AI and HR approval. The same
functions run those steps, so every existing rule still applies.

The QR itself is fixed and can be printed once. It is only a shortcut to the
portal: being at the office is proven by GPS, the person by PIN and face.

This module holds the PINs, the check-in session tokens and the HR pages. The
employee-facing pages live in app.employee_portal.
"""
import hashlib
import hmac
import io
import logging
import secrets
import time
from html import escape

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import text

from app.database import get_db
from app.location_links import _secret

logger = logging.getLogger(__name__)
router = APIRouter()

ENABLED_SETTING = "web_checkin_enabled"
SESSION_SECONDS = 12 * 60       # time to finish location + selfie after the PIN
PIN_MAX_FAILURES = 5
PIN_LOCK_SECONDS = 15 * 60
MAX_PHOTO_BYTES = 8 * 1024 * 1024


def apply_web_checkin_migrations(engine, sqlite: bool) -> None:
    big = "INTEGER" if sqlite else "BIGINT"
    with engine.begin() as conn:
        conn.execute(text(
            f"""CREATE TABLE IF NOT EXISTS web_checkin_pins(
                employee_id {big} PRIMARY KEY REFERENCES employees(id),
                pin_hash TEXT NOT NULL, failed INTEGER NOT NULL DEFAULT 0,
                locked_until {big} NOT NULL DEFAULT 0, updated_at {big} NOT NULL)"""))


def enabled() -> bool:
    from app.runtime import get_setting
    return get_setting(ENABLED_SETTING, "0") == "1"


def _base(request: Request) -> str:
    from app.config import settings
    return settings.public_base_url or str(request.base_url).rstrip("/")


# ------------------------------------------------------------------- QR code

def qr_svg(url: str) -> bytes:
    import qrcode
    import qrcode.image.svg
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


# ----------------------------------------------------------------------- PIN

def _pin_hash(employee_id: int, pin: str) -> str:
    salt = hmac.new(_secret(), f"pin:{employee_id}".encode(), hashlib.sha256).hexdigest()
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()


def check_pin(employee_id: int, pin: str) -> str:
    """'ok', 'wrong', 'locked' or 'none' (no PIN issued yet)."""
    now = int(time.time())
    with get_db() as c:
        row = c.execute("SELECT * FROM web_checkin_pins WHERE employee_id=?", (employee_id,)).fetchone()
        if not row:
            return "none"
        if int(row["locked_until"]) > now:
            return "locked"
        if hmac.compare_digest(row["pin_hash"], _pin_hash(employee_id, pin)):
            c.execute("UPDATE web_checkin_pins SET failed=0 WHERE employee_id=?", (employee_id,))
            return "ok"
        failed = int(row["failed"]) + 1
        locked = now + PIN_LOCK_SECONDS if failed >= PIN_MAX_FAILURES else 0
        c.execute("UPDATE web_checkin_pins SET failed=?,locked_until=? WHERE employee_id=?",
                  (0 if locked else failed, locked, employee_id))
        return "locked" if locked else "wrong"


def set_pin(employee_id: int, pin: str, db=None) -> int:
    """Store a new PIN; returns its version (signed-in portal sessions of the old PIN end)."""
    version = max(int(time.time()), _pin_version(employee_id, db) + 1)
    sql = ("INSERT INTO web_checkin_pins(employee_id,pin_hash,failed,locked_until,updated_at) VALUES(?,?,0,0,?) "
           "ON CONFLICT(employee_id) DO UPDATE SET pin_hash=excluded.pin_hash,failed=0,locked_until=0,updated_at=excluded.updated_at")
    args = (employee_id, _pin_hash(employee_id, pin), version)
    if db is not None:
        db.execute(sql, args)
    else:
        with get_db() as c:
            c.execute(sql, args)
    return version


def _pin_version(employee_id: int, db=None) -> int:
    query = "SELECT updated_at FROM web_checkin_pins WHERE employee_id=?"
    if db is not None:
        row = db.execute(query, (employee_id,)).fetchone()
    else:
        with get_db() as c:
            row = c.execute(query, (employee_id,)).fetchone()
    return int(row["updated_at"]) if row else 0


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="web-checkin")


def _session(token: str) -> dict:
    try:
        data = _serializer().loads(str(token or ""), max_age=SESSION_SECONDS)
    except SignatureExpired:
        raise ValueError("সময় শেষ হয়ে গেছে। আবার শুরু করুন।")
    except BadSignature:
        raise ValueError("Link সঠিক নয়। আবার শুরু করুন।")
    if not enabled():
        raise ValueError("Web Check In এখন বন্ধ। WhatsApp-এ Check In করুন।")
    return data


def portal_url(request: Request) -> str:
    return f"{_base(request)}/me"


# --------------------------------------------------------------- HR controls

@router.get("/checkin/qr.svg")
def office_qr_svg(request: Request):
    from app.main import require_permission
    require_permission(request, "attendance_edit")
    return Response(qr_svg(portal_url(request)), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/checkin/print", response_class=HTMLResponse)
def office_qr_poster(request: Request):
    """A one-page poster to print and stick near the office door."""
    from app.main import require_permission
    require_permission(request, "attendance_edit")
    svg = qr_svg(portal_url(request)).decode()
    svg = svg[svg.index("<svg"):]
    return HTMLResponse(f"""<!doctype html><html lang='bn'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>BURAQ office QR</title><link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;600;700&display=swap'>
    <style>@page{{size:A4;margin:0}}*{{box-sizing:border-box}}body{{margin:0;font-family:'Hind Siliguri',sans-serif;color:#0E3B2E;background:#fff}}
    .sheet{{width:210mm;min-height:297mm;margin:0 auto;padding:22mm 20mm;display:flex;flex-direction:column;align-items:center;text-align:center}}
    .mark{{font-weight:700;font-size:20px;letter-spacing:.04em}}h1{{font-size:44px;line-height:1.15;margin:14mm 0 4mm}}
    .lead{{font-size:22px;margin:0;color:#35524A}}.qr{{width:120mm;height:120mm;margin:12mm 0;padding:6mm;border:3px solid #0E3B2E;border-radius:10mm}}
    .qr svg{{width:100%;height:100%}}ol{{text-align:left;font-size:20px;line-height:1.7;margin:0;padding-left:1.2em}}
    .url{{margin-top:auto;font-size:16px;color:#35524A}}.tool{{text-align:center;padding:12px}}@media print{{.tool{{display:none}}}}
    button{{font:inherit;font-size:16px;padding:10px 18px;border-radius:10px;border:0;background:#0E3B2E;color:#fff;cursor:pointer}}</style></head>
    <body><div class='tool'><button onclick='window.print()'>Print</button></div><div class='sheet'><div class='mark'>BURAQ Smart Attendance</div>
    <h1>আমার BURAQ</h1><p class='lead'>Attendance, duty, ছুটি আর বেতনের হিসাব — নিজের phone-এ।</p>
    <div class='qr'>{svg}</div>
    <ol><li>Phone-এর camera দিয়ে QR scan করুন।</li><li>Staff ID আর PIN দিয়ে ঢুকুন।</li>
    <li>WhatsApp কাজ না করলে এখান থেকেই Check In / Check Out দিন।</li></ol>
    <div class='url'>{escape(portal_url(request))}</div></div></body></html>""")


@router.get("/web-checkin", response_class=HTMLResponse)
def web_checkin_admin(request: Request, issued: list | None = None):
    from app.main import require_permission, has_permission, layout
    require_permission(request, "attendance_edit")
    can_pin = has_permission(request, "employees_edit")
    on = enabled()
    with get_db() as c:
        employees = c.execute(
            "SELECT e.id,e.staff_id,e.name,e.registration_status,p.updated_at,p.locked_until FROM employees e "
            "LEFT JOIN web_checkin_pins p ON p.employee_id=e.id WHERE e.is_active ORDER BY e.staff_id").fetchall()
    shown = ""
    if issued:
        items = "".join(f"<tr><td><b>{escape(name)}</b><div class='sub'>{escape(staff)}</div></td>"
                        f"<td><span style='font-size:26px;font-weight:700;letter-spacing:.3em' class='pin'>{escape(pin)}</span></td></tr>"
                        for name, staff, pin in issued)
        shown = ("<div class='card' style='overflow:auto'><h3>New PINs</h3>"
                 "<div class='sub'>Give each person their PIN in person. They are not shown again; each person can change theirs in the portal.</div>"
                 f"<table><tbody>{items}</tbody></table></div><div class='section-gap'></div>")
    now = int(time.time())
    missing = sum(1 for e in employees if not e["updated_at"])
    rows = "".join(
        f"<tr><td><b>{escape(e['name'])}</b><div class='sub'>{escape(e['staff_id'])}</div></td>"
        f"<td>{'PIN set' if e['updated_at'] else '<span class=sub>No PIN</span>'}"
        + (" <span class='status bad'>locked</span>" if e['locked_until'] and int(e['locked_until']) > now else "")
        + ("" if e['registration_status'] == 'approved' else " <span class='status warn'>not registered</span>")
        + "</td><td>"
        + (f"<form method='post' action='/web-checkin/pin/{e['id']}'><button class='btn secondary'>{'New PIN' if e['updated_at'] else 'Create PIN'}</button></form>" if can_pin else "")
        + "</td></tr>" for e in employees)
    switch = (f"<form method='post' action='/web-checkin/toggle'><input type='hidden' name='on' value='{0 if on else 1}'>"
              f"<button class='btn{' danger' if on else ''}'>{'Turn off Web Check In' if on else 'Turn on Web Check In'}</button></form>")
    bulk = (f"<form method='post' action='/web-checkin/pins/missing'><button class='btn'>Create PINs for {missing} without one</button></form>"
            if can_pin and missing else "")
    body = f"""{shown}<div class='hero'><div><h2>Employee portal</h2>
    <div class='sub'>Staff open <b>{escape(portal_url(request))}</b> (or scan the printed QR) and sign in with Staff ID and PIN to see
    their attendance, duty, leave and payslips.</div>
    <div class='sub' style='margin-top:8px'>Web Check In: <b>{'ON: staff can Check In / Check Out from the portal' if on else 'OFF: staff use WhatsApp'}</b>.
    Turn it on when WhatsApp is not working. Same location, selfie, Face AI and HR approval as WhatsApp.</div></div>
    <div class='actions'>{switch}<a class='btn secondary' href='/checkin/print' target='_blank'>Print office QR</a></div></div>
    <div class='card' style='overflow:auto'><div class='card-head'><div><h3>PINs</h3><div class='sub'>Each employee needs a PIN once. Five wrong tries lock it for 15 minutes.</div></div>{bulk}</div>
    <table><thead><tr><th>Employee</th><th>PIN</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>"""
    response = layout("Employee portal", body, request, "webcheckin")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/web-checkin/toggle")
def web_checkin_toggle(request: Request, on: int = Form(...)):
    from app.main import require_permission, audit
    from app.runtime import set_setting
    require_permission(request, "attendance_edit")
    set_setting(ENABLED_SETTING, "1" if on else "0")
    audit(request, "web_checkin_toggle", "setting", ENABLED_SETTING, "on" if on else "off")
    return RedirectResponse("/web-checkin", 303)


def _new_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


@router.post("/web-checkin/pin/{employee_id}", response_class=HTMLResponse)
def web_checkin_new_pin(request: Request, employee_id: int):
    from app.main import require_permission, audit
    require_permission(request, "employees_edit")
    with get_db() as c:
        employee = c.execute("SELECT name,staff_id FROM employees WHERE id=? AND is_active", (employee_id,)).fetchone()
        if not employee:
            raise HTTPException(404, "Employee not found")
        pin = _new_pin()
        set_pin(employee_id, pin, db=c)
        audit(request, "web_checkin_pin", "employee", str(employee_id), "PIN created or replaced", db=c)
    return web_checkin_admin(request, issued=[(employee["name"], employee["staff_id"], pin)])


@router.post("/web-checkin/pins/missing", response_class=HTMLResponse)
def web_checkin_missing_pins(request: Request):
    from app.main import require_permission, audit
    require_permission(request, "employees_edit")
    issued = []
    with get_db() as c:
        rows = c.execute("SELECT e.id,e.name,e.staff_id FROM employees e LEFT JOIN web_checkin_pins p ON p.employee_id=e.id "
                         "WHERE e.is_active AND p.employee_id IS NULL ORDER BY e.staff_id").fetchall()
        for row in rows:
            pin = _new_pin()
            set_pin(int(row["id"]), pin, db=c)
            issued.append((row["name"], row["staff_id"], pin))
        audit(request, "web_checkin_pin", "employee", "bulk", f"Created {len(issued)} PINs", db=c)
    return web_checkin_admin(request, issued=issued)
