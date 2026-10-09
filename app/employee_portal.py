"""আমার BURAQ: the employee's own page, opened from the printed office QR.

An employee signs in with Staff ID and PIN and sees only their own records:
today's attendance, this month at a glance, the next week's duty, leave
requests, payslips and their own duty-time website use. They can apply for
leave, report a wrong attendance record, change their PIN and, while HR has
Web Check In turned on, Check In / Check Out with location and selfie.

Nothing here can change attendance or pay directly: leave and corrections go
to HR, and check-ins run the WhatsApp steps that end in HR approval.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta
from html import escape

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import web_checkin as wc
from app.database import get_db
from app.time_format import format_time_12h

logger = logging.getLogger(__name__)
router = APIRouter()

SESSION_DAYS = 30
PAY_UNLOCK_SECONDS = 10 * 60
OPEN_SHIFT_HOURS = 16

WEEKDAYS = ["সোমবার", "মঙ্গলবার", "বুধবার", "বৃহস্পতিবার", "শুক্রবার", "শনিবার", "রবিবার"]
WEEKDAYS_SHORT = ["সোম", "মঙ্গল", "বুধ", "বৃহ", "শুক্র", "শনি", "রবি"]
MONTHS = ["জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন", "জুলাই", "আগস্ট",
          "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]
LEAVE_LABELS = {"Casual": "নৈমিত্তিক (Casual)", "Sick": "অসুস্থতা (Sick)", "Annual": "বার্ষিক (Annual)", "Unpaid": "বেতন ছাড়া (Unpaid)"}
STATUS_WORDS = {"pending": "অপেক্ষায়", "approved": "মঞ্জুর", "rejected": "নামঞ্জুর"}


def _today() -> date:
    from app.services import now_local
    return now_local().date()


def _bn_date(day: date, weekday: bool = True) -> str:
    text_ = f"{day.day} {MONTHS[day.month - 1]}"
    return f"{WEEKDAYS[day.weekday()]}, {text_}" if weekday else text_


def _minutes(total: int) -> str:
    total = int(total or 0)
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} ঘণ্টা {minutes} মিনিট"
    return f"{hours} ঘণ্টা" if hours else f"{minutes} মিনিট"


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None


def _recent(value, now: datetime, hours: int) -> bool:
    """created_at is UTC text on SQLite and an aware timestamp on Postgres."""
    from datetime import timezone
    made = _parse(value)
    if not made:
        return False
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    return now - made < timedelta(hours=hours)


# ------------------------------------------------------------------ session

def current_employee(request: Request):
    data = request.session.get("emp")
    if not isinstance(data, dict):
        return None
    if time.time() - float(data.get("at", 0)) > SESSION_DAYS * 86400:
        request.session.pop("emp", None)
        return None
    with get_db() as c:
        employee = c.execute(
            "SELECT e.*,p.updated_at AS pin_version FROM employees e JOIN web_checkin_pins p ON p.employee_id=e.id "
            "WHERE e.id=? AND e.is_active", (int(data.get("id", 0)),)).fetchone()
    if not employee or int(employee["pin_version"]) != int(data.get("v", -1)):
        request.session.pop("emp", None)          # a new PIN or a deactivated employee ends the session
        return None
    return employee


def _sign_in(request: Request, employee_id: int, version: int) -> None:
    request.session.pop("emp_pay", None)
    request.session["emp"] = {"id": int(employee_id), "v": int(version), "at": int(time.time())}


def _flash(request: Request, kind: str, message: str) -> None:
    request.session["emp_flash"] = [kind, message]


def _take_flash(request: Request) -> str:
    item = request.session.pop("emp_flash", None)
    if not item:
        return ""
    kind, message = item
    return f"<div class='note {escape(kind)}' role='status'>{escape(message)}</div>"


def _pay_unlocked(request: Request) -> bool:
    return time.time() - float(request.session.get("emp_pay", 0)) < PAY_UNLOCK_SECONDS


# -------------------------------------------------------------------- shell

STYLE = """<style>
:root{--forest:#0E3B2E;--forest-2:#1A5644;--mint:#5EEAAA;--mint-soft:#D3F6E5;--paper:#F6F5F1;--sheet:#fff;
--line:#DDE3DF;--ink:#0E3B2E;--muted:#5A6B64;--amber:#9A6212;--amber-soft:#FBEBCF;--rust:#B42318;--rust-soft:#FBE4E1;
--sky:#255E9E;--sky-soft:#E1ECF8}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:12px}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 'Hind Siliguri',system-ui,sans-serif;
font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;padding-bottom:84px}
a{color:inherit}h1,h2,h3{font-weight:600;line-height:1.25;margin:0}
.wrap{max-width:560px;margin:0 auto;padding:0 16px}
.today{background:var(--forest);color:#EFF5F1;padding:0 0 26px;border-radius:0 0 30px 30px}
.bar{display:flex;align-items:center;justify-content:space-between;padding:14px 0 6px;font-size:15px}
.mark{font-weight:700;letter-spacing:.01em}.mark b{color:var(--mint);font-weight:700}
.who{display:flex;align-items:center;gap:10px;color:#C6DAD1}
.who form{margin:0}.link{background:none;border:0;color:#C6DAD1;font:inherit;text-decoration:underline;cursor:pointer;padding:6px 0}
.date{margin-top:18px;color:#B9D3C8;font-size:16px}
.clock{font-size:62px;font-weight:600;line-height:1;margin:6px 0 8px;letter-spacing:-.01em;color:#fff}
.clock small{font-size:22px;font-weight:500;margin-left:6px;color:#B9D3C8}
.state{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:16px}
.tag{display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;font-size:14.5px;font-weight:500;background:rgba(255,255,255,.1)}
.tag.on{background:var(--mint);color:var(--forest)}.tag.late{background:#F4C978;color:#3B2606}.tag.wait{background:#E8EEF7;color:#173B63}
.tag.bad{background:#F6C9C2;color:#5E120A}
.dot{width:8px;height:8px;border-radius:50%;background:var(--forest)}
@media (prefers-reduced-motion:no-preference){.tag.on .dot{animation:beat 2s ease-in-out infinite}}
@keyframes beat{50%{opacity:.25}}
.line{position:relative;height:10px;margin:26px 0 6px;border-radius:6px;background:rgba(255,255,255,.12)}
.line i{position:absolute;top:0;bottom:0;border-radius:6px;background:var(--mint)}
.line b{position:absolute;top:-5px;width:3px;height:20px;border-radius:2px;background:#fff}
.ends{display:flex;justify-content:space-between;font-size:14px;color:#B9D3C8}
.act{margin-top:22px;display:grid;gap:10px}
.go{display:block;width:100%;text-align:center;font:inherit;font-size:19px;font-weight:600;padding:15px;border-radius:16px;border:0;
background:var(--mint);color:var(--forest);cursor:pointer;text-decoration:none}
.go.ghost{background:transparent;color:#fff;box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.35)}
.hint{font-size:15px;color:#B9D3C8;margin-top:12px}
.note{margin:16px 0 0;padding:12px 14px;border-radius:14px;font-size:16px;white-space:pre-line;background:var(--sheet);color:var(--ink);border:1px solid var(--line)}
.note.good{background:var(--mint-soft);border-color:#A6E8C8}.note.bad{background:var(--rust-soft);border-color:#F2BDB5;color:#6E160C}
.today .note{border:0}
section{padding:34px 0 0}section>h2{font-size:22px}.lede{color:var(--muted);font-size:15.5px;margin:2px 0 14px}
.sheet{background:var(--sheet);border:1px solid var(--line);border-radius:18px}
.rows>div,.rows>a{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:13px 16px;border-top:1px solid var(--line);text-decoration:none}
.rows>:first-child{border-top:0}.rows .main{min-width:0}.rows .sub{color:var(--muted);font-size:14.5px}
.rows .num{font-weight:600;white-space:nowrap}.special{color:var(--sky);font-size:14px;font-weight:500}
.pill{font-size:14px;padding:2px 10px;border-radius:999px;white-space:nowrap;background:var(--paper)}
.pill.approved{background:var(--mint-soft)}.pill.rejected{background:var(--rust-soft);color:#6E160C}.pill.pending{background:var(--amber-soft);color:#4A2F06}
.month-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.month-head nav{display:flex;gap:14px;font-size:15px}.month-head nav a{color:var(--forest-2)}
.grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:12px}
.grid .wd{font-size:12.5px;color:var(--muted);text-align:center;padding-bottom:2px}
.day{position:relative;aspect-ratio:1;border-radius:11px;display:flex;align-items:flex-start;justify-content:flex-start;padding:5px 7px;
font-size:14px;font-weight:500;color:var(--muted)}
.day.present{background:var(--mint-soft);color:var(--forest)}.day.late{background:var(--amber-soft);color:#4A2F06}
.day.absent{background:var(--rust-soft);color:#6E160C}.day.leave{background:var(--sky-soft);color:#173B63}
.day.duty{background:var(--sheet);box-shadow:inset 0 0 0 1px var(--line);color:var(--ink)}
.day.open::after{content:"";position:absolute;right:7px;bottom:7px;width:7px;height:7px;border-radius:50%;background:var(--rust)}
.day.now{box-shadow:inset 0 0 0 2px var(--forest)}
.key{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:12px;font-size:14px;color:var(--muted)}
.key span{display:inline-flex;align-items:center;gap:6px}.key i{width:12px;height:12px;border-radius:4px;display:inline-block}
.facts{display:grid;grid-template-columns:1fr 1fr;margin-top:16px}
.facts div{padding:12px 16px;border-top:1px solid var(--line)}.facts div:nth-child(odd){border-right:1px solid var(--line)}
.facts div:nth-child(-n+2){border-top:0}.facts b{display:block;font-size:24px;font-weight:600;line-height:1.2}.facts span{font-size:14.5px;color:var(--muted)}
details{margin-top:12px}summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:8px;font-weight:600;
padding:11px 16px;border-radius:14px;background:var(--forest);color:#fff}
summary::-webkit-details-marker{display:none}details[open] summary{background:var(--forest-2)}
.form{padding:16px;margin-top:10px}label{display:block;font-weight:500;font-size:15.5px;margin:12px 0 5px}label:first-child{margin-top:0}
input,select,textarea{width:100%;font:inherit;font-size:17px;padding:11px 12px;border:1.5px solid #C8D2CC;border-radius:12px;background:#fff;color:var(--ink)}
textarea{min-height:76px;resize:vertical}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btn{display:inline-block;font:inherit;font-weight:600;font-size:17px;padding:12px 18px;border-radius:14px;border:0;background:var(--forest);color:#fff;cursor:pointer;text-decoration:none;margin-top:16px}
.btn.wide{width:100%;text-align:center}.small{font-size:14.5px;color:var(--muted);margin-top:8px}
.sites>div{display:grid;grid-template-columns:1fr auto;gap:2px 12px;padding:10px 16px;border-top:1px solid var(--line)}
.sites>div:first-child{border-top:0}.sites .meter{grid-column:1/-1;height:6px;border-radius:4px;background:var(--paper);overflow:hidden}
.sites .meter i{display:block;height:100%;background:var(--forest-2);border-radius:4px}
.tabs{position:fixed;left:0;right:0;bottom:0;background:rgba(255,255,255,.96);backdrop-filter:blur(8px);border-top:1px solid var(--line);
padding:6px 8px calc(6px + env(safe-area-inset-bottom));z-index:5}
.tabs div{max-width:560px;margin:0 auto;display:grid;grid-template-columns:repeat(4,1fr)}
.tabs a{display:flex;flex-direction:column;align-items:center;gap:1px;padding:6px 0;font-size:13.5px;text-decoration:none;color:var(--muted);border-radius:12px}
.tabs a:active{background:var(--paper)}.tabs svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
:focus-visible{outline:3px solid #2E8B6A;outline-offset:2px}
.empty{padding:16px;color:var(--muted)}
.steps{display:grid;gap:12px;margin-top:18px}.step{padding:16px;border-radius:18px;background:rgba(255,255,255,.08)}
.step h3{font-size:18px;display:flex;gap:10px;align-items:center}.step h3 span{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;
background:rgba(255,255,255,.14);font-size:15px}.step.done h3 span{background:var(--mint);color:var(--forest)}
.step[aria-disabled=true]{opacity:.45}.step p{margin:6px 0 0;color:#C6DAD1;font-size:15.5px}
.step input[type=file]{margin-top:12px;background:rgba(255,255,255,.95)}
.login{min-height:100vh;padding-bottom:0}.login .today{padding-bottom:70px}.login h1{font-size:38px;margin-top:40px;color:#fff}
.login .lede{color:#B9D3C8;font-size:17px}.login .card{margin-top:-46px;padding:20px}
.pin{letter-spacing:.35em;font-size:24px}
</style>"""

ICONS = {
    "today": "<svg viewBox='0 0 24 24'><circle cx='12' cy='12' r='8.5'/><path d='M12 7.5V12l3 2'/></svg>",
    "month": "<svg viewBox='0 0 24 24'><rect x='3.5' y='5' width='17' height='15' rx='2.5'/><path d='M3.5 10h17M8 3v4M16 3v4'/></svg>",
    "leave": "<svg viewBox='0 0 24 24'><path d='M5 20c0-6 3-11 14-15-1 9-5 14-11 14'/><path d='M5 20l7-7'/></svg>",
    "salary": "<svg viewBox='0 0 24 24'><rect x='3' y='6' width='18' height='12' rx='2.5'/><circle cx='12' cy='12' r='2.5'/></svg>",
}


def _shell(body: str, title: str = "আমার BURAQ", status: int = 200, tabs: bool = True, page_class: str = "") -> HTMLResponse:
    nav = ""
    if tabs:
        items = [("today", "আজ"), ("month", "মাস"), ("leave", "ছুটি"), ("salary", "বেতন")]
        nav = "<nav class='tabs' aria-label='Sections'><div>" + "".join(
            f"<a href='/me#{key}'>{ICONS[key]}<span>{label}</span></a>" for key, label in items) + "</div></nav>"
    return HTMLResponse(
        "<!doctype html><html lang='bn'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<meta name='robots' content='noindex'><meta name='theme-color' content='#0E3B2E'>"
        f"<title>{escape(title)}</title><link rel='manifest' href='/me/manifest.webmanifest'>"
        "<link rel='icon' href='/me/icon.svg' type='image/svg+xml'><link rel='apple-touch-icon' href='/me/icon.svg'>"
        "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&display=swap'>"
        f"{STYLE}</head><body class='{page_class}'>{body}{nav}</body></html>",
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _bar(employee=None) -> str:
    who = ""
    if employee is not None:
        who = (f"<div class='who'><span>{escape(employee['staff_id'])}</span>"
               "<form method='post' action='/me/logout'><button class='link'>বের হন</button></form></div>")
    return f"<div class='bar'><div class='mark'>আমার <b>BURAQ</b></div>{who}</div>"


# -------------------------------------------------------------------- login

def _login_page(message: str = "", status: int = 200, staff_id: str = "") -> HTMLResponse:
    note = f"<div class='note bad' role='alert'>{escape(message)}</div>" if message else ""
    return _shell(f"""<header class='today'><div class='wrap'>{_bar()}
      <h1>আপনার attendance, duty, ছুটি আর বেতন</h1>
      <p class='lede'>Staff ID আর PIN দিয়ে ঢুকুন। এখানে শুধু আপনার নিজের হিসাব দেখা যায়।</p></div></header>
      <main class='wrap'><form class='sheet card' method='post' action='/me/login'>
        <label for='staff'>Staff ID</label>
        <input id='staff' name='staff_id' value='{escape(staff_id)}' autocomplete='username' autocapitalize='characters' required maxlength='40'>
        <label for='pin'>PIN</label>
        <input id='pin' class='pin' name='pin' type='password' inputmode='numeric' pattern='[0-9]{{4,6}}' autocomplete='current-password' required maxlength='6'>
        {note}<button class='btn wide'>ঢুকুন</button>
        <p class='small'>PIN নেই বা ভুলে গেছেন? HR-এর কাছ থেকে নতুন PIN নিন।</p></form></main>""",
                  title="আমার BURAQ: ঢুকুন", status=status, tabs=False, page_class="login")


@router.get("/me/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_employee(request):
        return RedirectResponse("/me", 303)
    return _login_page()


@router.post("/me/login", response_class=HTMLResponse)
def login(request: Request, staff_id: str = Form(...), pin: str = Form(...)):
    staff_id = staff_id.strip()[:40]
    with get_db() as c:
        employee = c.execute("SELECT id FROM employees WHERE UPPER(staff_id)=UPPER(?) AND is_active", (staff_id,)).fetchone()
    if not employee:
        return _login_page("Staff ID বা PIN সঠিক নয়।", 401, staff_id)
    result = wc.check_pin(int(employee["id"]), pin.strip())
    if result == "none":
        return _login_page("আপনার PIN এখনো দেওয়া হয়নি। HR-এর কাছ থেকে PIN নিন।", 401, staff_id)
    if result == "locked":
        return _login_page("অনেকবার ভুল PIN দেওয়া হয়েছে। ১৫ মিনিট পরে আবার চেষ্টা করুন।", 429, staff_id)
    if result != "ok":
        return _login_page("Staff ID বা PIN সঠিক নয়।", 401, staff_id)
    _sign_in(request, int(employee["id"]), wc._pin_version(int(employee["id"])))
    logger.info("Employee portal sign-in employee_id=%s", employee["id"])
    return RedirectResponse("/me", 303)


@router.post("/me/logout")
def logout(request: Request):
    for key in ("emp", "emp_pay", "emp_flash"):
        request.session.pop(key, None)
    return RedirectResponse("/me/login", 303)


# ---------------------------------------------------------------- the page

def _duty_lookup(c, employee_id: int, first: date, last: date):
    weekly = {int(r["weekday"]): r for r in c.execute(
        "SELECT * FROM duty_schedules WHERE employee_id=? AND is_active", (employee_id,)).fetchall()}
    custom = {r["duty_date"]: r for r in c.execute(
        "SELECT * FROM custom_duties WHERE employee_id=? AND duty_date>=? AND duty_date<=? AND is_active",
        (employee_id, first.isoformat(), last.isoformat())).fetchall()}

    def duty(day: date):
        return custom.get(day.isoformat()) or weekly.get(day.weekday())
    return duty, custom


def _today_block(c, employee, today: date, duty) -> str:
    from app.services import now_local
    now = now_local()
    employee_id = int(employee["id"])
    rows = c.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date IN (?,?) ORDER BY work_date DESC",
                     (employee_id, today.isoformat(), (today - timedelta(days=1)).isoformat())).fetchall()
    record = next((r for r in rows if r["work_date"] == today.isoformat()), None)
    yesterday = next((r for r in rows if r["work_date"] != today.isoformat()), None)
    if not record and yesterday and yesterday["check_in"] and not yesterday["check_out"]:
        started = _parse(yesterday["check_in"])
        if started and started.tzinfo and (now - started) < timedelta(hours=OPEN_SHIFT_HOURS):
            record = yesterday                                   # a night duty still running
    proof = c.execute("SELECT action,review_status,created_at FROM attendance_fingerprints WHERE employee_id=? "
                      "ORDER BY id DESC LIMIT 1", (employee_id,)).fetchone()

    duty_today = duty(today)
    tags, clock, caption = [], "", ""
    check_in = _parse(record["check_in"]) if record else None
    check_out = _parse(record["check_out"]) if record else None
    if check_in and not check_out:
        clock, caption = format_time_12h(record["check_in"]), "Check In"
        tags.append("<span class='tag on'><span class='dot'></span>Duty চলছে</span>")
        action = "checkout"
    elif check_in and check_out:
        worked = int((check_out - check_in).total_seconds() // 60) if check_out > check_in else 0
        clock, caption = format_time_12h(record["check_out"]), "Check Out"
        tags.append(f"<span class='tag'>আজ কাজ {escape(_minutes(worked))}</span>")
        action = ""
    else:
        clock = now.strftime("%I:%M").lstrip("0")
        caption = now.strftime("%p")
        tags.append("<span class='tag'>এখনো Check In হয়নি</span>")
        action = "checkin"
    if record and int(record["late_minutes"] or 0) > 0:
        tags.append(f"<span class='tag late'>{escape(_minutes(record['late_minutes']))} দেরি</span>")
    if proof and proof["review_status"] == "pending":
        tags.append("<span class='tag wait'>Selfie HR-এর approval-এর অপেক্ষায়</span>")
    elif proof and proof["review_status"] == "rejected" and _recent(proof["created_at"], now, 12):
        tags.append("<span class='tag bad'>শেষ selfie HR reject করেছে</span>")

    timeline = ""
    if duty_today:
        day = date.fromisoformat(record["work_date"]) if record else today
        start = datetime.combine(day, datetime.strptime(duty_today["start_time"][:5], "%H:%M").time(), tzinfo=now.tzinfo)
        end = datetime.combine(day, datetime.strptime(duty_today["end_time"][:5], "%H:%M").time(), tzinfo=now.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        span = (end - start).total_seconds()

        def at(moment: datetime) -> float:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=now.tzinfo)
            return max(0.0, min(100.0, (moment - start).total_seconds() / span * 100))
        fill = ""
        if check_in:
            left, right = at(check_in), at(check_out or now)
            fill = f"<i style='left:{left:.1f}%;width:{max(right - left, 1.2):.1f}%'></i>"
        marker = "" if check_out else f"<b style='left:calc({at(now):.1f}% - 1px)' title='এখন'></b>"
        timeline = (f"<div class='line' role='img' aria-label='আজকের duty {escape(format_time_12h(duty_today['start_time']))} থেকে "
                    f"{escape(format_time_12h(duty_today['end_time']))}'>{fill}{marker}</div>"
                    f"<div class='ends'><span>Duty {escape(format_time_12h(duty_today['start_time']))}</span>"
                    f"<span>{escape(format_time_12h(duty_today['end_time']))}</span></div>")
    else:
        timeline = "<div class='hint'>আজ আপনার কোনো duty নেই।</div>"

    if action:
        label = "Check In" if action == "checkin" else "Check Out"
        if wc.enabled():
            buttons = (f"<form class='act' method='post' action='/me/attendance'><input type='hidden' name='action' value='{action}'>"
                       f"<button class='go'>{label} দিন</button></form>"
                       "<div class='hint'>Location আর একটি selfie লাগবে। তারপর HR approve করবে।</div>")
        else:
            buttons = f"<div class='hint'>{label} দিন WhatsApp-এ, আগের মতোই। WhatsApp কাজ না করলে HR এখানে {label} চালু করে দেবে।</div>"
    else:
        buttons = ""
    if caption in ("AM", "PM"):
        clock, caption = f"{clock} {caption}", "এখনকার সময়"
    else:
        caption = f"{caption}-এর সময়"
    digits, _, suffix = clock.partition(" ")
    return (f"<div class='date'>{escape(_bn_date(today))}</div>"
            f"<div class='clock'>{escape(digits)}<small>{escape(suffix)}</small></div>"
            f"<div class='date' style='margin:0 0 12px'>{escape(caption)}</div>"
            f"<div class='state'>{''.join(tags)}</div>{timeline}{buttons}")


def _month_block(c, employee, month_start: date, today: date) -> str:
    employee_id = int(employee["id"])
    last = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
    duty, _ = _duty_lookup(c, employee_id, month_start, last)
    attendance = {r["work_date"]: r for r in c.execute(
        "SELECT work_date,check_in,check_out,late_minutes FROM attendance WHERE employee_id=? AND work_date>=? AND work_date<=?",
        (employee_id, month_start.isoformat(), last.isoformat())).fetchall()}
    leaves = c.execute("SELECT start_date,end_date FROM leave_requests WHERE employee_id=? AND status='approved' "
                       "AND start_date<=? AND end_date>=?", (employee_id, last.isoformat(), month_start.isoformat())).fetchall()
    on_leave = set()
    for leave in leaves:
        day = max(date.fromisoformat(leave["start_date"]), month_start)
        while day <= min(date.fromisoformat(leave["end_date"]), last):
            on_leave.add(day.isoformat())
            day += timedelta(days=1)

    present = late_days = late_total = open_days = absent = leave_days = 0
    cells = [f"<div class='wd'>{name}</div>" for name in WEEKDAYS_SHORT]
    cells += ["<div></div>"] * month_start.weekday()
    day = month_start
    while day <= last:
        key = day.isoformat()
        row = attendance.get(key)
        classes, label = [], ""
        if row and row["check_in"]:
            present += 1
            late = int(row["late_minutes"] or 0)
            if late > 0:
                late_days += 1
                late_total += late
                classes.append("late")
                label = f"দেরি {late} মিনিট"
            else:
                classes.append("present")
                label = "উপস্থিত"
            if not row["check_out"] and day < today:
                open_days += 1
                classes.append("open")
                label += ", Check Out নেই"
        elif key in on_leave:
            leave_days += 1
            classes.append("leave")
            label = "ছুটি"
        elif duty(day) and day < today:
            absent += 1
            classes.append("absent")
            label = "অনুপস্থিত"
        elif duty(day):
            classes.append("duty")
            label = "duty আছে"
        else:
            label = "duty নেই"
        if day == today:
            classes.append("now")
        cells.append(f"<div class='day {' '.join(classes)}' title='{escape(label)}' aria-label='{day.day}: {escape(label)}'>{day.day}</div>")
        day += timedelta(days=1)

    prev_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = (last + timedelta(days=1))
    nav = f"<a href='/me?month={prev_month:%Y-%m}#month'>আগের মাস</a>"
    if next_month <= today.replace(day=1):
        nav += f"<a href='/me?month={next_month:%Y-%m}#month'>পরের মাস</a>"
    facts = [(present, "দিন উপস্থিত"), (late_days, f"দিন দেরি, মোট {_minutes(late_total)}" if late_days else "দিন দেরি"),
             (absent, "দিন অনুপস্থিত"), (open_days, "দিন Check Out নেই")]
    if leave_days:
        facts[2] = (absent, f"দিন অনুপস্থিত, {leave_days} দিন ছুটি")
    facts_html = "".join(f"<div><b>{value}</b><span>{escape(label)}</span></div>" for value, label in facts)
    key = ("<div class='key'><span><i style='background:var(--mint-soft)'></i>উপস্থিত</span>"
           "<span><i style='background:var(--amber-soft)'></i>দেরি</span><span><i style='background:var(--rust-soft)'></i>অনুপস্থিত</span>"
           "<span><i style='background:var(--sky-soft)'></i>ছুটি</span>"
           "<span><i style='background:var(--rust);border-radius:50%;width:8px;height:8px'></i>Check Out নেই</span></div>")
    fix = ""
    if open_days or absent:
        fix = "<p class='small'>কোনো দিনের হিসাব ভুল মনে হলে নিচে <a href='#fix'>ভুল জানান</a>।</p>"
    return (f"<div class='month-head'><h2>{MONTHS[month_start.month - 1]} {month_start.year}</h2><nav>{nav}</nav></div>"
            f"<div class='grid'>{''.join(cells)}</div>{key}<div class='sheet facts'>{facts_html}</div>{fix}")


def _week_block(c, employee, today: date) -> str:
    duty, custom = _duty_lookup(c, int(employee["id"]), today, today + timedelta(days=6))
    rows = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        item = duty(day)
        name = "আজ" if offset == 0 else "আগামীকাল" if offset == 1 else WEEKDAYS[day.weekday()]
        if item:
            special = "<div class='special'>বিশেষ duty" + (f": {escape(item['note'])}" if item["note"] else "") + "</div>" \
                if day.isoformat() in custom and "note" in item.keys() else ""
            rows.append(f"<div><div class='main'><b>{name}</b><div class='sub'>{escape(_bn_date(day, False))}"
                        f"{', ' + escape(item['office_name']) if item['office_name'] and item['office_name'] != 'BURAQ Office' else ''}</div>{special}</div>"
                        f"<div class='num'>{escape(format_time_12h(item['start_time']))} – {escape(format_time_12h(item['end_time']))}</div></div>")
        else:
            rows.append(f"<div><div class='main'><b>{name}</b><div class='sub'>{escape(_bn_date(day, False))}</div></div>"
                        "<div class='sub'>duty নেই</div></div>")
    return "<h2>আগামী ৭ দিনের duty</h2><p class='lede'>HR duty বদলালে এখানেও বদলে যাবে।</p>" \
           f"<div class='sheet rows'>{''.join(rows)}</div>"


def _leave_block(c, employee, today: date) -> str:
    rows = c.execute("SELECT leave_type,start_date,end_date,status,reason FROM leave_requests WHERE employee_id=? "
                     "ORDER BY id DESC LIMIT 8", (int(employee["id"]),)).fetchall()
    items = "".join(
        f"<div><div class='main'><b>{escape(LEAVE_LABELS.get(r['leave_type'], r['leave_type']).split(' (')[0])} ছুটি</b>"
        f"<div class='sub'>{escape(_range(r['start_date'], r['end_date']))}</div></div>"
        f"<span class='pill {escape(r['status'])}'>{escape(STATUS_WORDS.get(r['status'], r['status']))}</span></div>"
        for r in rows) or "<div class='empty'>এখনো কোনো ছুটির আবেদন নেই।</div>"
    options = "".join(f"<option value='{key}'>{escape(label)}</option>" for key, label in LEAVE_LABELS.items())
    return (f"<h2>ছুটি</h2><p class='lede'>আবেদন HR-এর কাছে যায়। মঞ্জুর হলে এখানে দেখবেন।</p>"
            f"<div class='sheet rows'>{items}</div>"
            "<details><summary>ছুটির আবেদন করুন</summary><form class='sheet form' method='post' action='/me/leave'>"
            f"<label for='lt'>ধরন</label><select id='lt' name='leave_type'>{options}</select>"
            f"<div class='two'><div><label for='ls'>শুরু</label><input id='ls' type='date' name='start_date' required min='{today - timedelta(days=30)}'></div>"
            f"<div><label for='le'>শেষ</label><input id='le' type='date' name='end_date' required min='{today - timedelta(days=30)}'></div></div>"
            "<label for='lr'>কারণ</label><textarea id='lr' name='reason' required maxlength='500'></textarea>"
            "<button class='btn'>আবেদন পাঠান</button></form></details>")


def _range(start: str, end: str) -> str:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = (last - first).days + 1
    if first == last:
        return f"{_bn_date(first, False)}, ১ দিন"
    return f"{_bn_date(first, False)} থেকে {_bn_date(last, False)}, {days} দিন"


def _salary_block(request: Request, c, employee) -> str:
    head = "<h2>বেতন</h2><p class='lede'>HR চূড়ান্ত করা মাসগুলোর হিসাব আর payslip।</p>"
    if not _pay_unlocked(request):
        return head + ("<form class='sheet form' method='post' action='/me/salary/unlock'>"
                       "<label for='pp'>বেতন দেখতে আবার PIN দিন</label>"
                       "<input id='pp' class='pin' name='pin' type='password' inputmode='numeric' pattern='[0-9]{4,6}' required maxlength='6' autocomplete='current-password'>"
                       "<button class='btn'>বেতন দেখুন</button>"
                       "<p class='small'>পাশে কেউ phone দেখলেও যেন বেতন না দেখে, তাই ১০ মিনিট পর আবার বন্ধ হয়ে যায়।</p></form>")
    rows = c.execute("SELECT id,salary_month,net_salary,payment_status FROM payroll_records WHERE employee_id=? "
                     "AND payment_status IN ('finalized','paid') ORDER BY salary_month DESC LIMIT 12", (int(employee["id"]),)).fetchall()
    items = "".join(
        f"<a href='/me/payslip/{r['id']}.pdf'><div class='main'><b>{escape(_month_name(r['salary_month']))}</b>"
        f"<div class='sub'>{'দেওয়া হয়েছে' if r['payment_status'] == 'paid' else 'চূড়ান্ত, এখনো দেওয়া হয়নি'}. Payslip PDF</div></div>"
        f"<div class='num'>৳{float(r['net_salary'] or 0):,.0f}</div></a>"
        for r in rows) or "<div class='empty'>এখনো কোনো চূড়ান্ত বেতনের হিসাব নেই।</div>"
    return head + f"<div class='sheet rows'>{items}</div><p class='small'>হিসাবে প্রশ্ন থাকলে HR-এর সাথে কথা বলুন।</p>"


def _month_name(value: str) -> str:
    try:
        year, month = (int(part) for part in str(value)[:7].split("-"))
        return f"{MONTHS[month - 1]} {year}"
    except (ValueError, IndexError):
        return str(value)


def _browsing_block(c, employee, today: date) -> str:
    employee_id = int(employee["id"])
    try:
        paired = c.execute("SELECT 1 FROM browsing_devices WHERE employee_id=? AND revoked_at IS NULL LIMIT 1", (employee_id,)).fetchone()
        usage = c.execute("SELECT domain,seconds FROM browsing_usage WHERE employee_id=? AND work_date=? AND domain NOT LIKE '~%' "
                          "ORDER BY seconds DESC", (employee_id, today.isoformat())).fetchall()
        presence = c.execute("SELECT idle_seconds,outside_seconds FROM browsing_presence WHERE employee_id=? AND work_date=?",
                             (employee_id, today.isoformat())).fetchone()
    except Exception:
        return ""
    if not paired and not usage:
        return ""
    total = sum(int(r["seconds"] or 0) for r in usage)
    top = usage[:5]
    biggest = max((int(r["seconds"] or 0) for r in top), default=1) or 1
    sites = "".join(
        f"<div><span>{escape(r['domain'])}</span><span class='num'>{escape(_minutes(int(r['seconds']) // 60))}</span>"
        f"<span class='meter'><i style='width:{int(r['seconds']) / biggest * 100:.0f}%'></i></span></div>" for r in top) \
        or "<div class='empty'>আজ duty-তে এখনো কোনো website-এর সময় নেই।</div>"
    extra = []
    if presence and int(presence["idle_seconds"] or 0) >= 60:
        extra.append(f"কোনো click ছাড়া খোলা: {_minutes(int(presence['idle_seconds']) // 60)}")
    if presence and int(presence["outside_seconds"] or 0) >= 60:
        extra.append(f"Tracker-এর বাইরে: {_minutes(int(presence['outside_seconds']) // 60)}")
    extra_html = "".join(f"<p class='small'>{escape(line)}</p>" for line in extra)
    return (f"<section id='web'><h2>আজ duty-তে website</h2>"
            f"<p class='lede'>মোট {escape(_minutes(total // 60))}। HR যা দেখে আপনিও ঠিক তাই দেখছেন: শুধু website-এর নাম আর সময়।</p>"
            f"<div class='sheet sites'>{sites}</div>{extra_html}</section>")


def _fix_block(today: date) -> str:
    return ("<section id='fix'><h2>ভুল জানান</h2><p class='lede'>Check In / Check Out ভুল বা বাদ পড়লে HR-কে জানান। HR দেখে ঠিক করবে।</p>"
            "<details><summary>হিসাব ঠিক করার অনুরোধ</summary><form class='sheet form' method='post' action='/me/correction'>"
            f"<label for='fd'>তারিখ</label><input id='fd' type='date' name='work_date' required max='{today}' min='{today - timedelta(days=30)}'>"
            "<div class='two'><div><label for='fi'>সঠিক Check In</label><input id='fi' type='time' name='check_in'></div>"
            "<div><label for='fo'>সঠিক Check Out</label><input id='fo' type='time' name='check_out'></div></div>"
            "<label for='fr'>কী হয়েছিল</label><textarea id='fr' name='reason' required maxlength='500'></textarea>"
            "<button class='btn'>HR-কে পাঠান</button></form></details></section>")


def _pin_block() -> str:
    return ("<section id='pin'><h2>PIN বদলান</h2><p class='lede'>নতুন PIN শুধু আপনি জানবেন। বদলালে অন্য সব phone থেকে বের হয়ে যাবে।</p>"
            "<details><summary>নতুন PIN দিন</summary><form class='sheet form' method='post' action='/me/pin'>"
            "<label for='p0'>এখনকার PIN</label><input id='p0' class='pin' name='current' type='password' inputmode='numeric' required maxlength='6' autocomplete='current-password'>"
            "<div class='two'><div><label for='p1'>নতুন PIN</label><input id='p1' class='pin' name='new' type='password' inputmode='numeric' pattern='[0-9]{4,6}' required maxlength='6' autocomplete='new-password'></div>"
            "<div><label for='p2'>আবার</label><input id='p2' class='pin' name='confirm' type='password' inputmode='numeric' pattern='[0-9]{4,6}' required maxlength='6' autocomplete='new-password'></div></div>"
            "<p class='small'>৪ থেকে ৬ সংখ্যা। 1234 বা 1111-এর মতো সহজ PIN হবে না।</p>"
            "<button class='btn'>PIN বদলান</button></form></details></section>")


@router.get("/me", response_class=HTMLResponse)
def portal(request: Request, month: str = ""):
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    today = _today()
    month_start = today.replace(day=1)
    if re.fullmatch(r"\d{4}-\d{2}", month or ""):
        try:
            chosen = date(int(month[:4]), int(month[5:]), 1)
            if date(2020, 1, 1) <= chosen <= month_start:
                month_start = chosen
        except ValueError:
            pass
    with get_db() as c:
        duty, _ = _duty_lookup(c, int(employee["id"]), today - timedelta(days=1), today)
        today_html = _today_block(c, employee, today, duty)
        month_html = _month_block(c, employee, month_start, today)
        week_html = _week_block(c, employee, today)
        leave_html = _leave_block(c, employee, today)
        salary_html = _salary_block(request, c, employee)
        browsing_html = _browsing_block(c, employee, today)
    flash = _take_flash(request)
    first_name = escape(str(employee["name"]).split()[0]) if employee["name"] else ""
    return _shell(f"""<header class='today' id='today'><div class='wrap'>{_bar(employee)}
      <p class='date' style='margin-top:10px;color:#fff;font-size:19px'>সালাম, {first_name}</p>
      {today_html}{flash}</div></header>
      <main class='wrap'>
        <section id='month'>{month_html}</section>
        <section id='week'>{week_html}</section>
        <section id='leave'>{leave_html}</section>
        <section id='salary'>{salary_html}</section>
        {browsing_html}{_fix_block(today)}{_pin_block()}
        <p class='small' style='margin:36px 0 12px'>BURAQ Smart Attendance. এখানে শুধু আপনার নিজের তথ্য দেখা যায়।</p>
      </main>""")


# ------------------------------------------------------------- employee forms

@router.post("/me/leave")
def apply_leave(request: Request, leave_type: str = Form(...), start_date: str = Form(...), end_date: str = Form(...),
                reason: str = Form(...)):
    from app.leave_flow import LEAVE_TYPES, _date_error, _overlap
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    back = RedirectResponse("/me#leave", 303)
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        _flash(request, "bad", "তারিখ সঠিক নয়।")
        return back
    reason = reason.strip()[:500]
    error = None if leave_type in LEAVE_TYPES else "ছুটির ধরন বেছে নিন।"
    error = error or _date_error(start, end) or (None if reason else "কারণ লিখুন।")
    if not error and _overlap(int(employee["id"]), start, end):
        error = "এই তারিখে আগেই একটি ছুটির আবেদন আছে।"
    if error:
        _flash(request, "bad", error.replace("❌", "").strip())
        return back
    with get_db() as c:
        c.execute("INSERT INTO leave_requests(employee_id,leave_type,start_date,end_date,reason,requested_by) VALUES(?,?,?,?,?,?)",
                  (int(employee["id"]), leave_type, start.isoformat(), end.isoformat(), reason, "employee:portal"))
    _flash(request, "good", f"ছুটির আবেদন HR-এর কাছে পাঠানো হয়েছে: {_range(start.isoformat(), end.isoformat())}।")
    return back


@router.post("/me/correction")
def request_correction(request: Request, work_date: str = Form(...), check_in: str = Form(""), check_out: str = Form(""),
                       reason: str = Form(...)):
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    back = RedirectResponse("/me#fix", 303)
    today = _today()
    try:
        day = date.fromisoformat(work_date)
    except ValueError:
        _flash(request, "bad", "তারিখ সঠিক নয়।")
        return back
    times = [t.strip() for t in (check_in, check_out)]
    if any(t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in times):
        _flash(request, "bad", "সময় সঠিক নয়।")
        return back
    if day > today or day < today - timedelta(days=30):
        _flash(request, "bad", "শুধু আজ বা গত ৩০ দিনের হিসাব ঠিক করার অনুরোধ করা যায়।")
        return back
    if not any(times) or not reason.strip():
        _flash(request, "bad", "সঠিক Check In বা Check Out সময় আর কী হয়েছিল তা লিখুন।")
        return back
    with get_db() as c:
        duplicate = c.execute("SELECT 1 FROM attendance_corrections WHERE employee_id=? AND work_date=? AND status='pending'",
                              (int(employee["id"]), day.isoformat())).fetchone()
        if duplicate:
            _flash(request, "bad", "এই দিনের একটি অনুরোধ আগেই HR-এর কাছে আছে।")
            return back
        c.execute("INSERT INTO attendance_corrections(employee_id,work_date,requested_check_in,requested_check_out,reason,requested_by) "
                  "VALUES(?,?,?,?,?,?)", (int(employee["id"]), day.isoformat(), times[0] or None, times[1] or None,
                                          reason.strip()[:500], "employee:portal"))
    _flash(request, "good", f"{_bn_date(day, False)}-এর হিসাব ঠিক করার অনুরোধ HR-এর কাছে পাঠানো হয়েছে।")
    return back


WEAK_PINS = {"1234", "12345", "123456", "4321", "0123", "2580", "1212"}


@router.post("/me/pin")
def change_pin(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    back = RedirectResponse("/me#pin", 303)
    new = new.strip()
    if not re.fullmatch(r"\d{4,6}", new):
        _flash(request, "bad", "নতুন PIN ৪ থেকে ৬ সংখ্যার হতে হবে।")
        return back
    if new != confirm.strip():
        _flash(request, "bad", "দুইবার দেওয়া নতুন PIN মেলেনি।")
        return back
    if new in WEAK_PINS or len(set(new)) == 1:
        _flash(request, "bad", "এই PIN খুব সহজ। অন্য PIN দিন।")
        return back
    result = wc.check_pin(int(employee["id"]), current.strip())
    if result == "locked":
        request.session.pop("emp", None)
        return _login_page("অনেকবার ভুল PIN দেওয়া হয়েছে। ১৫ মিনিট পরে আবার চেষ্টা করুন।", 429, employee["staff_id"])
    if result != "ok":
        _flash(request, "bad", "এখনকার PIN সঠিক নয়।")
        return back
    version = wc.set_pin(int(employee["id"]), new)
    _sign_in(request, int(employee["id"]), version)
    _flash(request, "good", "PIN বদলানো হয়েছে। অন্য phone-এ নতুন PIN দিয়ে ঢুকতে হবে।")
    return back


@router.post("/me/salary/unlock")
def unlock_salary(request: Request, pin: str = Form(...)):
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    result = wc.check_pin(int(employee["id"]), pin.strip())
    if result == "ok":
        request.session["emp_pay"] = int(time.time())
    elif result == "locked":
        request.session.pop("emp", None)
        return _login_page("অনেকবার ভুল PIN দেওয়া হয়েছে। ১৫ মিনিট পরে আবার চেষ্টা করুন।", 429, employee["staff_id"])
    else:
        _flash(request, "bad", "PIN সঠিক নয়।")
    return RedirectResponse("/me#salary", 303)


@router.get("/me/payslip/{payroll_id}.pdf")
def own_payslip(request: Request, payroll_id: int):
    from app.main import payslip_pdf_for
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    if not _pay_unlocked(request):
        return RedirectResponse("/me#salary", 303)
    with get_db() as c:
        row = c.execute("SELECT payment_status FROM payroll_records WHERE id=? AND employee_id=?",
                        (payroll_id, int(employee["id"]))).fetchone()
    if not row or row["payment_status"] not in ("finalized", "paid"):
        return _shell("<main class='wrap'><section><h2>Payslip পাওয়া যায়নি</h2>"
                      "<p class='lede'>এই মাসের হিসাব এখনো চূড়ান্ত হয়নি।</p><a class='btn' href='/me#salary'>ফিরে যান</a></section></main>",
                      status=404)
    pdf, filename = payslip_pdf_for(payroll_id, employee_id=int(employee["id"]))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}", "Cache-Control": "no-store"})


# ------------------------------------------------- Check In / Check Out (web)

def _capture_page(employee, action: str, token: str, message: str = "", located: bool = False, kind: str = "") -> HTMLResponse:
    label = "Check In" if action == "checkin" else "Check Out"
    note = f"<div id='m' class='note {kind}' role='status'>{escape(message)}</div>" if message else \
        "<div id='m' class='note' role='status' hidden></div>"
    return _shell(f"""<header class='today'><div class='wrap'>{_bar(employee)}
      <h1 style='font-size:34px;margin-top:22px;color:#fff'>{label}</h1>
      <p class='date' style='margin-top:4px'>{escape(employee['name'])}, {escape(employee['staff_id'])}</p>{note}
      <div class='steps'>
        <div class='step{' done' if located else ''}' id='s1'><h3><span>১</span>অফিসের location</h3>
          <p>Phone জিজ্ঞেস করলে <b>Allow</b> দিন। অফিসের ভেতরে থাকতে হবে।</p>
          <div class='act' {'hidden' if located else ''}><button class='go' id='loc' type='button'>Location দিন</button></div></div>
        <div class='step' id='s2' aria-disabled='{'false' if located else 'true'}'><h3><span>২</span>Selfie</h3>
          <p>উপরের নির্দেশ মেনে এখনই নতুন selfie তুলুন। Gallery-র ছবি চলবে না।</p>
          <form id='sf' method='post' action='/checkin/selfie' enctype='multipart/form-data' {'' if located else 'hidden'}>
            <input type='hidden' name='token' value='{escape(token)}'>
            <input id='photo' name='photo' type='file' accept='image/*' capture='user' required aria-label='Selfie'>
            <div class='act'><button class='go' id='send'>Selfie পাঠান</button></div></form></div>
      </div>
      <p class='hint'><a href='/me'>বাতিল করে ফিরে যান</a></p></div></header>
    <script>
    (function () {{
      var m = document.getElementById('m'), loc = document.getElementById('loc'), sf = document.getElementById('sf');
      function say(t, cls) {{ m.hidden = false; m.textContent = t; m.className = 'note ' + (cls || ''); }}
      if (loc) loc.onclick = function () {{
        if (!navigator.geolocation) {{ say('এই phone-এ location পাওয়া যাচ্ছে না।', 'bad'); return; }}
        loc.disabled = true; say('Location নেওয়া হচ্ছে…');
        navigator.geolocation.getCurrentPosition(function (p) {{
          fetch('/checkin/location', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{token: {json.dumps(token)}, latitude: p.coords.latitude, longitude: p.coords.longitude}})}})
          .then(function (r) {{ return r.json(); }}).then(function (d) {{
            if (d.ok) {{
              say(d.message, 'good'); loc.parentNode.hidden = true;
              document.getElementById('s1').className = 'step done';
              document.getElementById('s2').setAttribute('aria-disabled', 'false'); sf.hidden = false;
            }} else {{ say(d.message, 'bad'); loc.disabled = false; }}
          }}).catch(function () {{ say('Server-এ পৌঁছানো যাচ্ছে না। আবার চেষ্টা করুন।', 'bad'); loc.disabled = false; }});
        }}, function () {{ say('Location পাওয়া যায়নি। Phone-এর Location চালু করে Allow দিন।', 'bad'); loc.disabled = false; }},
        {{enableHighAccuracy: true, timeout: 20000, maximumAge: 0}});
      }};
      sf.onsubmit = function () {{ document.getElementById('send').disabled = true; say('Selfie যাচাই হচ্ছে, একটু অপেক্ষা করুন…'); }};
    }})();
    </script>""", title=f"{label}: আমার BURAQ", tabs=False)


@router.post("/me/attendance", response_class=HTMLResponse)
def start_attendance(request: Request, action: str = Form(...)):
    from app import services
    employee = current_employee(request)
    if not employee:
        return RedirectResponse("/me/login", 303)
    if action not in ("checkin", "checkout"):
        return RedirectResponse("/me", 303)
    if not wc.enabled():
        _flash(request, "bad", "Web Check In এখন বন্ধ। WhatsApp-এ দিন।")
        return RedirectResponse("/me", 303)
    phone = services.normalize_phone(employee["whatsapp_phone"] or employee["phone"])
    if not phone or employee["registration_status"] != "approved":
        _flash(request, "bad", "আপনার registration সম্পূর্ণ নয়। HR-এর সাথে যোগাযোগ করুন।")
        return RedirectResponse("/me", 303)
    reply = services.begin_attendance_action(phone, action)
    if reply != "__REQUEST_LOCATION__":
        _flash(request, "bad", reply)
        return RedirectResponse("/me", 303)
    token = wc._serializer().dumps({"e": int(employee["id"]), "p": phone, "a": action})
    logger.info("Web check-in started employee_id=%s action=%s", employee["id"], action)
    return _capture_page(employee, action, token)


@router.post("/checkin/location")
async def checkin_location(request: Request):
    from app import services
    try:
        data = await request.json()
        session = wc._session(data.get("token"))
        latitude, longitude = float(data.get("latitude")), float(data.get("longitude"))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Location সঠিক নয়।")
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc) or "Location সঠিক নয়।"}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "message": "অনুরোধ সঠিক নয়।"}, status_code=400)
    reply = services.receive_location(session["p"], latitude, longitude)
    ok = reply.startswith("✅")
    message = reply.replace("selfie পাঠান", "selfie তুলুন").replace("তুলে পাঠান", "তুলুন")
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 422)


@router.post("/checkin/selfie", response_class=HTMLResponse)
async def checkin_selfie(request: Request, token: str = Form(...), photo: UploadFile = File(...)):
    from app import services
    try:
        session = wc._session(token)
    except ValueError as exc:
        _flash(request, "bad", str(exc))
        return RedirectResponse("/me", 303)
    with get_db() as c:
        employee = c.execute("SELECT * FROM employees WHERE id=?", (int(session["e"]),)).fetchone()
    image = await photo.read(wc.MAX_PHOTO_BYTES + 1)
    if not image or len(image) > wc.MAX_PHOTO_BYTES:
        return _capture_page(employee, session["a"], token, "ছবিটি পাওয়া যায়নি বা অনেক বড়। আবার selfie তুলুন।", True, "bad")
    reply = await asyncio.to_thread(services.receive_image, session["p"], f"web:{uuid.uuid4().hex}", image)
    if reply.startswith("⏳") or reply.startswith("✅"):
        label = "Check In" if session["a"] == "checkin" else "Check Out"
        _flash(request, "good", f"{label}-এর selfie পাঠানো হয়েছে। HR approve করলে এখানে দেখা যাবে।")
        return RedirectResponse("/me", 303)
    # The WhatsApp steps either ask for another selfie (a new pose) or end the attempt.
    still_waiting = (services.state(session["p"]) or {}).get("state", "").startswith(("checkin_selfie", "checkout_selfie"))
    if still_waiting:
        return _capture_page(employee, session["a"], token, reply, True, "bad")
    _flash(request, "bad", reply)
    return RedirectResponse("/me", 303)


# ------------------------------------------------ old links, PWA, shortcuts

@router.get("/checkin")
def old_checkin_link():
    return RedirectResponse("/me", 303)


@router.get("/checkin/status")
def old_status_link():
    return RedirectResponse("/me", 303)


@router.get("/me/manifest.webmanifest")
def manifest():
    data = {"name": "আমার BURAQ", "short_name": "BURAQ", "start_url": "/me", "scope": "/me", "display": "standalone",
            "background_color": "#F6F5F1", "theme_color": "#0E3B2E", "lang": "bn",
            "icons": [{"src": "/me/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json")


@router.get("/me/icon.svg")
def icon():
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='#0E3B2E'/>"
           "<circle cx='256' cy='256' r='150' fill='none' stroke='#5EEAAA' stroke-width='36'/>"
           "<path d='M256 168v92l62 40' fill='none' stroke='#F6F5F1' stroke-width='36' stroke-linecap='round' stroke-linejoin='round'/></svg>")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
