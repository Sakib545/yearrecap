"""AI helpers for HR, powered by the Gemini API.

Four things, each small and each safe to be wrong:

* sort browsed websites into work / social / entertainment / ... (an Admin can
  overrule any of them);
* a short weekly note per employee;
* a daily list of unusual attendance or browsing, for a human to look at;
* "ask the dashboard": a typed question becomes one read-only query.

What leaves this server is kept deliberately small. Summaries and flags send
Staff IDs and numbers, never names or phone numbers. "Ask" sends only the
question and the table layout — the query runs here, against a throwaway
in-memory copy that holds no pay, phone, face or login data, and the rows are
shown to the Admin without going back to Gemini.

Nothing here acts on its own: it never changes attendance, pay or messages.
"""
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

CATEGORIES = {
    "work": "Work",
    "social": "Social media",
    "entertainment": "Entertainment",
    "shopping": "Shopping",
    "news": "News",
    "other": "Other",
}
SEVERITIES = ("high", "medium", "low")
CLASSIFY_BATCH = 80
ASK_HISTORY_DAYS = 120
ASK_MAX_ROWS = 200
WORKER_INTERVAL_SECONDS = 30 * 60
DAILY_FLAGS_AFTER_HOUR = 9


class AIUnavailable(Exception):
    """Raised with a message that is safe to show an Admin."""


def api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"


def daily_call_limit() -> int:
    try:
        return max(1, int(os.getenv("AI_DAILY_CALL_LIMIT", "300")))
    except ValueError:
        return 300


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


# ------------------------------------------------------------------- database

def apply_ai_migrations(engine, sqlite: bool) -> None:
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "BIGSERIAL PRIMARY KEY"
    big = "INTEGER" if sqlite else "BIGINT"
    statements = [
        f"""CREATE TABLE IF NOT EXISTS browsing_site_categories(
            domain TEXT PRIMARY KEY, category TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'ai', updated_at {big} NOT NULL DEFAULT 0)""",
        f"""CREATE TABLE IF NOT EXISTS ai_reports(
            id {pk}, kind TEXT NOT NULL, period_key TEXT NOT NULL,
            employee_id {big}, severity TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', created_at {big} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_ai_reports_kind_period ON ai_reports(kind, period_key)",
        f"""CREATE TABLE IF NOT EXISTS ai_usage(
            day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
            input_tokens {big} NOT NULL DEFAULT 0, output_tokens {big} NOT NULL DEFAULT 0)""",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


# --------------------------------------------------------------------- Gemini

def _post(url: str, headers: dict, body: dict) -> dict:
    """The one network call. Tests replace this."""
    response = httpx.post(url, headers=headers, json=body, timeout=60)
    if response.status_code in (401, 403):
        raise AIUnavailable("Gemini rejected the API key. Check GEMINI_API_KEY.")
    if response.status_code == 404:
        raise AIUnavailable(f"Gemini does not know the model '{model_name()}'. Check GEMINI_MODEL.")
    if response.status_code == 429:
        raise AIUnavailable("Gemini quota or rate limit reached. Try again later or check billing.")
    if response.is_error:
        logger.error("Gemini API error %s: %s", response.status_code, response.text[:500])
        raise AIUnavailable(f"Gemini returned an error ({response.status_code}).")
    return response.json()


def ask_gemini(system: str, prompt: str, files: list[tuple[str, bytes]] | None = None, temperature: float = 0.2):
    """Send one prompt (optionally with PDF/image files), return the parsed JSON answer."""
    key = api_key()
    if not key:
        raise AIUnavailable("GEMINI_API_KEY is not set.")
    day = _today()
    with get_db() as c:
        row = c.execute("SELECT calls FROM ai_usage WHERE day=?", (day,)).fetchone()
        if row and int(row["calls"]) >= daily_call_limit():
            raise AIUnavailable("Today's AI call limit is used up (AI_DAILY_CALL_LIMIT).")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            *({"inline_data": {"mime_type": mime, "data": base64.b64encode(content).decode()}} for mime, content in (files or [])),
            {"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name()}:generateContent"
    try:
        data = _post(url, {"x-goog-api-key": key, "Content-Type": "application/json"}, body)
    except httpx.HTTPError as exc:
        raise AIUnavailable("Could not reach Gemini. Check the server's internet connection.") from exc
    usage = data.get("usageMetadata") or {}
    with get_db() as c:
        c.execute(
            "INSERT INTO ai_usage(day,calls,input_tokens,output_tokens) VALUES(?,1,?,?) "
            "ON CONFLICT(day) DO UPDATE SET calls=ai_usage.calls+1,"
            "input_tokens=ai_usage.input_tokens+excluded.input_tokens,"
            "output_tokens=ai_usage.output_tokens+excluded.output_tokens",
            (day, int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0)),
        )
    try:
        parts = data["candidates"][0]["content"]["parts"]
        raw = "".join(str(part.get("text") or "") for part in parts).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        return json.loads(raw)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIUnavailable("Gemini gave an answer that could not be read. Try again.") from exc


# ------------------------------------------------------------ site categories

def category_map(db) -> dict[str, str]:
    return {r["domain"]: r["category"] for r in db.execute("SELECT domain,category FROM browsing_site_categories").fetchall()}


def classify_new_domains() -> int:
    """Categorise websites seen in browsing data that have no category yet."""
    with get_db() as c:
        rows = c.execute(
            "SELECT u.domain,SUM(u.seconds) s FROM browsing_usage u "
            "LEFT JOIN browsing_site_categories k ON k.domain=u.domain "
            "WHERE k.domain IS NULL GROUP BY u.domain ORDER BY s DESC LIMIT ?",
            (CLASSIFY_BATCH,),
        ).fetchall()
    domains = [r["domain"] for r in rows]
    if not domains:
        return 0
    answer = ask_gemini(
        "You sort website domains for a small Bangladeshi retail business that sells through Facebook Pages and "
        "WhatsApp. Categories: work (business tools, email, documents, banking, courier, e-commerce seller panels, "
        "the company's own systems, AI assistants, search engines), social (social networks and chat), "
        "entertainment (video, music, games, sports), shopping (personal shopping), news, other. "
        'Reply with JSON only: {"domain": "category", ...} using exactly the domains given.',
        json.dumps(domains),
    )
    if not isinstance(answer, dict):
        raise AIUnavailable("Gemini gave an answer that could not be read. Try again.")
    now = int(time.time())
    saved = 0
    with get_db() as c:
        for domain in domains:
            category = str(answer.get(domain) or "other").strip().lower()
            if category not in CATEGORIES:
                category = "other"
            c.execute(
                "INSERT INTO browsing_site_categories(domain,category,source,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(domain) DO NOTHING",
                (domain, category, "ai", now),
            )
            saved += 1
    return saved


# ------------------------------------------------------- per-employee figures

def employee_figures(db, start: str, end: str) -> dict[int, dict]:
    """Attendance and browsing totals per active employee for start..end inclusive."""
    people = {
        r["id"]: {"staff_id": r["staff_id"], "name": r["name"], "days_present": 0, "late_days": 0, "late_minutes": 0,
                  "missing_checkout": 0, "browsing_minutes": {}, "top_sites": []}
        for r in db.execute("SELECT id,staff_id,name FROM employees WHERE is_active").fetchall()
    }
    for r in db.execute(
        "SELECT employee_id,check_in,check_out,late_minutes FROM attendance WHERE work_date>=? AND work_date<=?",
        (start, end),
    ).fetchall():
        person = people.get(r["employee_id"])
        if not person or not r["check_in"]:
            continue
        person["days_present"] += 1
        late = int(r["late_minutes"] or 0)
        if late > 0:
            person["late_days"] += 1
            person["late_minutes"] += late
        if not r["check_out"]:
            person["missing_checkout"] += 1
    categories = category_map(db)
    sites: dict[int, dict[str, int]] = {}
    for r in db.execute(
        "SELECT employee_id,domain,SUM(seconds) s FROM browsing_usage WHERE work_date>=? AND work_date<=? "
        "GROUP BY employee_id,domain",
        (start, end),
    ).fetchall():
        person = people.get(r["employee_id"])
        if not person:
            continue
        minutes = int(r["s"] or 0) // 60
        category = categories.get(r["domain"], "other")
        person["browsing_minutes"][category] = person["browsing_minutes"].get(category, 0) + minutes
        sites.setdefault(r["employee_id"], {})[r["domain"]] = minutes
    for employee_id, by_site in sites.items():
        top = sorted(by_site.items(), key=lambda item: item[1], reverse=True)[:5]
        people[employee_id]["top_sites"] = [f"{domain} {minutes}m" for domain, minutes in top if minutes > 0]
    return people


def _for_gemini(figures: dict) -> dict:
    return {key: value for key, value in figures.items() if key != "name"}


def _has_activity(figures: dict) -> bool:
    return bool(figures["days_present"] or figures["browsing_minutes"])


def _store_reports(kind: str, period_key: str, items: list[tuple], marker: str) -> None:
    now = int(time.time())
    with get_db() as c:
        c.execute("DELETE FROM ai_reports WHERE kind IN (?,?) AND period_key=?", (kind, marker, period_key))
        c.execute("INSERT INTO ai_reports(kind,period_key,content,created_at) VALUES(?,?,?,?)", (marker, period_key, "", now))
        for employee_id, severity, content in items:
            c.execute(
                "INSERT INTO ai_reports(kind,period_key,employee_id,severity,content,created_at) VALUES(?,?,?,?,?,?)",
                (kind, period_key, employee_id, severity, content, now),
            )


def generate_weekly(end_date: str = "") -> int:
    """One short note per employee for the 7 days ending end_date (default yesterday)."""
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else _now().date() - timedelta(days=1)
    start = end - timedelta(days=6)
    with get_db() as c:
        people = employee_figures(c, start.isoformat(), end.isoformat())
    active = {eid: p for eid, p in people.items() if _has_activity(p)}
    items: list[tuple] = []
    if active:
        answer = ask_gemini(
            "You write short weekly notes for the HR manager of a small Bangladeshi business. For each employee you get "
            "7 days of figures: days present, late days and minutes, missing check-outs, browsing minutes per category "
            "during duty, and top websites. Write 1-2 plain sentences in Bengali (Bangla script) per employee: what "
            "stands out, stated as fact from the numbers. Do not judge character, do not recommend punishment, and do "
            'not invent anything not in the figures. Reply with JSON only: {"<staff_id>": "<note>", ...}.',
            json.dumps({p["staff_id"]: _for_gemini(p) for p in active.values()}, ensure_ascii=False),
        )
        if not isinstance(answer, dict):
            raise AIUnavailable("Gemini gave an answer that could not be read. Try again.")
        for employee_id, person in active.items():
            note = str(answer.get(person["staff_id"]) or "").strip()[:600]
            if note:
                items.append((employee_id, "", note))
    _store_reports("weekly", end.isoformat(), items, "weekly_run")
    return len(items)


def generate_flags(day: str = "") -> int:
    """Unusual attendance or browsing on one day (default yesterday) against the 14 days before it."""
    target = datetime.strptime(day, "%Y-%m-%d").date() if day else _now().date() - timedelta(days=1)
    with get_db() as c:
        that_day = employee_figures(c, target.isoformat(), target.isoformat())
        before = employee_figures(c, (target - timedelta(days=14)).isoformat(), (target - timedelta(days=1)).isoformat())
    payload = {}
    for employee_id, person in that_day.items():
        if not _has_activity(person):
            continue
        payload[person["staff_id"]] = {"day": _for_gemini(person), "previous_14_days_total": _for_gemini(before[employee_id])}
    items: list[tuple] = []
    if payload:
        answer = ask_gemini(
            "You help the HR manager of a small Bangladeshi business notice unusual days. For each employee you get one "
            "day's figures and the totals of the 14 days before. List only what is clearly unusual for that person: "
            "much later than usual, a missing check-out, or far more non-work browsing than usual. Most employees "
            "should produce nothing. Each note is one plain sentence in Bengali (Bangla script) stating the fact and "
            "the numbers; no judgement, no punishment advice, nothing invented. Reply with JSON only: "
            '{"flags": [{"staff_id": "...", "severity": "low|medium|high", "note": "..."}]}.',
            json.dumps(payload, ensure_ascii=False),
        )
        flags = answer.get("flags") if isinstance(answer, dict) else None
        if not isinstance(flags, list):
            raise AIUnavailable("Gemini gave an answer that could not be read. Try again.")
        by_staff = {p["staff_id"]: eid for eid, p in that_day.items()}
        for flag in flags[:60]:
            if not isinstance(flag, dict):
                continue
            employee_id = by_staff.get(str(flag.get("staff_id") or ""))
            note = str(flag.get("note") or "").strip()[:400]
            severity = str(flag.get("severity") or "low").lower()
            if employee_id and note:
                items.append((employee_id, severity if severity in SEVERITIES else "low", note))
    _store_reports("flag", target.isoformat(), items, "flag_run")
    return len(items)


# ------------------------------------------------------------ ask the dashboard

ASK_SCHEMA = """Tables (SQLite). Dates are text 'YYYY-MM-DD'.
employees(id, staff_id, name, department, designation, shift, is_active)  -- is_active is 1 or 0
attendance(employee_id, work_date, check_in, check_out, shift, late_minutes, early_leave_minutes, status)
  -- one row per employee per day worked; check_in/check_out are ISO date-times or NULL; shift is 'first' or 'second'
leave_requests(employee_id, leave_type, start_date, end_date, status)  -- status: pending, approved, rejected
browsing(employee_id, work_date, domain, category, seconds)
  -- duty-time website use; category: work, social, entertainment, shopping, news, other"""

_ALLOWED_SQLITE_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _snapshot() -> sqlite3.Connection:
    """A private in-memory copy holding only what questions may be asked about."""
    since = (_now().date() - timedelta(days=ASK_HISTORY_DAYS)).isoformat()
    mem = sqlite3.connect(":memory:")
    mem.executescript(
        "CREATE TABLE employees(id,staff_id,name,department,designation,shift,is_active);"
        "CREATE TABLE attendance(employee_id,work_date,check_in,check_out,shift,late_minutes,early_leave_minutes,status);"
        "CREATE TABLE leave_requests(employee_id,leave_type,start_date,end_date,status);"
        "CREATE TABLE browsing(employee_id,work_date,domain,category,seconds);"
    )
    with get_db() as c:
        mem.executemany("INSERT INTO employees VALUES(?,?,?,?,?,?,?)", [
            (r["id"], r["staff_id"], r["name"], r["department"], r["designation"], r["shift"], 1 if r["is_active"] else 0)
            for r in c.execute("SELECT id,staff_id,name,department,designation,shift,is_active FROM employees").fetchall()])
        mem.executemany("INSERT INTO attendance VALUES(?,?,?,?,?,?,?,?)", [
            (r["employee_id"], r["work_date"], str(r["check_in"]) if r["check_in"] else None,
             str(r["check_out"]) if r["check_out"] else None, r["attendance_shift"], r["late_minutes"],
             r["early_leave_minutes"], r["status"])
            for r in c.execute(
                "SELECT employee_id,work_date,check_in,check_out,attendance_shift,late_minutes,early_leave_minutes,status "
                "FROM attendance WHERE work_date>=?", (since,)).fetchall()])
        mem.executemany("INSERT INTO leave_requests VALUES(?,?,?,?,?)", [
            (r["employee_id"], r["leave_type"], r["start_date"], r["end_date"], r["status"])
            for r in c.execute(
                "SELECT employee_id,leave_type,start_date,end_date,status FROM leave_requests WHERE end_date>=?",
                (since,)).fetchall()])
        categories = category_map(c)
        mem.executemany("INSERT INTO browsing VALUES(?,?,?,?,?)", [
            (r["employee_id"], r["work_date"], r["domain"], categories.get(r["domain"], "other"), int(r["seconds"]))
            for r in c.execute(
                "SELECT employee_id,work_date,domain,seconds FROM browsing_usage WHERE work_date>=?", (since,)).fetchall()])
    mem.commit()
    return mem


def run_readonly(mem: sqlite3.Connection, sql: str):
    """Run one SELECT. SQLite itself refuses anything that is not a read."""
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement or not re.match(r"(?is)^\s*(select|with)\b", statement):
        raise AIUnavailable("The question could not be turned into a safe query. Try wording it differently.")
    mem.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in _ALLOWED_SQLITE_ACTIONS else sqlite3.SQLITE_DENY)
    deadline = time.time() + 5
    mem.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    try:
        cursor = mem.execute(statement)
        columns = [d[0] for d in cursor.description or []]
        return columns, cursor.fetchmany(ASK_MAX_ROWS)
    except sqlite3.Error as exc:
        raise AIUnavailable("The question could not be answered from the data. Try wording it differently.") from exc


def answer_question(question: str) -> dict:
    question = " ".join(str(question or "").split())[:400]
    if not question:
        raise AIUnavailable("Type a question first.")
    answer = ask_gemini(
        "You turn an HR manager's question (Bengali, Banglish or English) into ONE SQLite SELECT statement.\n"
        + ASK_SCHEMA + f"\nToday is {_today()}. Only the last {ASK_HISTORY_DAYS} days exist. Join employees to show "
        "staff_id and name. Match names with LIKE and wildcards. Give columns clear English aliases. Add ORDER BY "
        "and LIMIT 50 unless the question needs every row. Salary, pay, phone numbers and face data are not available. "
        'Reply with JSON only: {"sql": "<statement>", "title": "<short title in the question\'s language>"} or, if it '
        'cannot be answered from these tables, {"sql": "", "title": "", "message": "<why, in the question\'s language>"}.',
        question,
    )
    if not isinstance(answer, dict):
        raise AIUnavailable("Gemini gave an answer that could not be read. Try again.")
    sql = str(answer.get("sql") or "").strip()
    if not sql:
        raise AIUnavailable(str(answer.get("message") or "This cannot be answered from attendance, leave and browsing data.")[:300])
    mem = _snapshot()
    try:
        columns, rows = run_readonly(mem, sql)
    finally:
        mem.close()
    return {"question": question, "title": str(answer.get("title") or "")[:120], "sql": sql, "columns": columns, "rows": rows}


# --------------------------------------------------------------------- worker

def _has_run(db, marker: str, period_key: str) -> bool:
    return bool(db.execute("SELECT 1 FROM ai_reports WHERE kind=? AND period_key=? LIMIT 1", (marker, period_key)).fetchone())


def run_cycle() -> None:
    """Background upkeep. Each step is independent; one failing never blocks the rest."""
    if not api_key():
        return
    now = _now()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    try:
        classify_new_domains()
    except AIUnavailable as exc:
        logger.warning("AI site categories skipped: %s", exc)
    if now.hour < DAILY_FLAGS_AFTER_HOUR:
        return
    try:
        with get_db() as c:
            flags_done = _has_run(c, "flag_run", yesterday)
            last_weekly = c.execute("SELECT MAX(period_key) k FROM ai_reports WHERE kind='weekly_run'").fetchone()["k"]
        if not flags_done:
            generate_flags(yesterday)
        if not last_weekly or last_weekly <= (now.date() - timedelta(days=8)).isoformat():
            generate_weekly(yesterday)
    except AIUnavailable as exc:
        logger.warning("AI reports skipped: %s", exc)


async def ai_worker():
    await asyncio.sleep(90)
    while True:
        try:
            await asyncio.to_thread(run_cycle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI insights cycle failed")
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)


# ------------------------------------------------------------------ dashboard

def _usage_line(db) -> str:
    month = _today()[:7]
    today = db.execute("SELECT calls FROM ai_usage WHERE day=?", (_today(),)).fetchone()
    total = db.execute(
        "SELECT COALESCE(SUM(calls),0) calls,COALESCE(SUM(input_tokens),0) i,COALESCE(SUM(output_tokens),0) o "
        "FROM ai_usage WHERE day LIKE ?", (month + "-%",)).fetchone()
    try:
        price_in = float(os.getenv("GEMINI_PRICE_INPUT_PER_M", "0.25"))
        price_out = float(os.getenv("GEMINI_PRICE_OUTPUT_PER_M", "1.50"))
    except ValueError:
        price_in, price_out = 0.25, 1.50
    cost = int(total["i"]) / 1e6 * price_in + int(total["o"]) / 1e6 * price_out
    return (f"Today {int(today['calls']) if today else 0}/{daily_call_limit()} calls • This month {int(total['calls'])} calls, "
            f"about ${cost:.2f} (estimate from token counts)")


def _page(request: Request, result: dict | None = None, error: str = "", notice: str = ""):
    from app.main import has_permission, layout
    manage = has_permission(request, "ai_manage")
    configured = bool(api_key())
    with get_db() as c:
        usage = _usage_line(c)
        names = {r["id"]: (r["name"], r["staff_id"]) for r in c.execute("SELECT id,name,staff_id FROM employees").fetchall()}
        flags = c.execute(
            "SELECT period_key,employee_id,severity,content FROM ai_reports WHERE kind='flag' AND period_key>=? "
            "ORDER BY period_key DESC,id", ((_now().date() - timedelta(days=7)).isoformat(),)).fetchall()
        last_week = c.execute("SELECT MAX(period_key) k FROM ai_reports WHERE kind='weekly_run'").fetchone()["k"]
        weekly = c.execute(
            "SELECT employee_id,content FROM ai_reports WHERE kind='weekly' AND period_key=? ORDER BY id",
            (last_week or "",)).fetchall()
    top = ""
    if not configured:
        top += ("<div class='notice' style='background:#fef3c7;color:#92400e'>Gemini is not connected yet. Add "
                "<b>GEMINI_API_KEY</b> in Railway Variables (from Google AI Studio, on a paid billing plan), then redeploy.</div>")
    if error:
        top += f"<div class='notice' style='background:#fee2e2;color:#991b1b'>{escape(error)}</div>"
    if notice:
        top += f"<div class='notice'>{escape(notice)}</div>"
    answer = ""
    if result:
        head = "".join(f"<th>{escape(str(col))}</th>" for col in result["columns"])
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{escape('' if value is None else str(value))}</td>" for value in row) + "</tr>"
            for row in result["rows"]
        ) or f"<tr><td colspan='{max(1, len(result['columns']))}'>No matching records.</td></tr>"
        more = f"<div class='sub'>Showing the first {ASK_MAX_ROWS} rows.</div>" if len(result["rows"]) >= ASK_MAX_ROWS else ""
        answer = (
            f"<div class='card' style='overflow:auto'><div class='eyebrow'>{escape(result['question'])}</div>"
            f"<h3>{escape(result['title'] or 'Answer')}</h3><table><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table>{more}"
            f"<details style='margin-top:12px'><summary>How this was worked out</summary><pre style='white-space:pre-wrap'>"
            f"{escape(result['sql'])}</pre></details></div><div class='section-gap'></div>"
        )

    def person(employee_id) -> str:
        name, staff_id = names.get(employee_id, ("Unknown", ""))
        return f"<b>{escape(name)}</b><div class='sub'>{escape(staff_id)}</div>"

    tone = {"high": "bad", "medium": "warn", "low": "ok"}
    flag_rows = "".join(
        f"<tr><td>{escape(f['period_key'])}</td><td>{person(f['employee_id'])}</td>"
        f"<td><span class='status {tone.get(f['severity'], 'ok')}'>{escape(f['severity'] or 'low')}</span></td>"
        f"<td>{escape(f['content'])}</td></tr>" for f in flags
    ) or "<tr><td colspan='4'>Nothing unusual in the last 7 days.</td></tr>"
    weekly_rows = "".join(
        f"<tr><td>{person(w['employee_id'])}</td><td>{escape(w['content'])}</td></tr>" for w in weekly
    ) or "<tr><td colspan='2'>No weekly summary yet.</td></tr>"
    command_link = "<a class='btn' href='/ai/command'>Manage by typing</a>" if manage else ""
    run_buttons = ""
    if manage and configured:
        run_buttons = "".join(
            f"<form method='post' action='/ai/run/{kind}'><button class='btn secondary'>{label}</button></form>"
            for kind, label in (("flags", "Check yesterday now"), ("weekly", "Write weekly summary now"), ("sites", "Sort new websites now"))
        )
    body = f"""{top}<div class='hero'><div><div class='eyebrow'>Gemini • {escape(model_name())}</div><h2>AI Insights</h2>
    <div class='sub'>Suggestions for a person to review. Nothing here changes attendance or pay.</div>
    <div class='sub'>{escape(usage)}</div></div><div class='actions'>{command_link}{run_buttons}</div></div>
    <div class='card'><h3>Ask about attendance, leave or browsing</h3>
    <form method='post' action='/ai/ask'><input name='question' maxlength='400' required
    placeholder='যেমন: এই মাসে কে সবচেয়ে বেশি late? / Who had no check-out last week?'{'' if configured else ' disabled'}>
    <button class='btn'{'' if configured else ' disabled'}>Ask</button></form>
    <div class='sub'>Last {ASK_HISTORY_DAYS} days. Salary, phone numbers and face data cannot be asked about.</div></div>
    <div class='section-gap'></div>{answer}
    <div class='card' style='overflow:auto'><h3>Unusual days</h3><div class='sub'>Checked once a day against each person's own last 14 days.</div>
    <table><thead><tr><th>Date</th><th>Employee</th><th>Level</th><th>What stood out</th></tr></thead><tbody>{flag_rows}</tbody></table></div>
    <div class='section-gap'></div>
    <div class='card' style='overflow:auto'><h3>Weekly summary</h3><div class='sub'>{('7 days ending ' + escape(last_week)) if last_week else 'Written once a week.'}</div>
    <table><thead><tr><th>Employee</th><th>Summary</th></tr></thead><tbody>{weekly_rows}</tbody></table></div>"""
    return layout("AI Insights", body, request, "ai")


@router.get("/ai", response_class=HTMLResponse)
def ai_page(request: Request, notice: str = "", error: str = ""):
    from app.main import require_permission
    require_permission(request, "ai_view")
    return _page(request, notice=notice[:200], error=error[:300])


@router.post("/ai/ask", response_class=HTMLResponse)
def ai_ask(request: Request, question: str = Form(...)):
    from app.main import require_permission, audit
    require_permission(request, "ai_view")
    try:
        result = answer_question(question)
    except AIUnavailable as exc:
        return _page(request, error=str(exc))
    audit(request, "ai_question", "ai", "", result["question"][:200])
    return _page(request, result=result)


@router.post("/ai/run/{kind}")
def ai_run(request: Request, kind: str):
    from app.main import require_permission, audit
    from urllib.parse import quote_plus
    require_permission(request, "ai_manage")
    jobs = {"flags": (generate_flags, "unusual days found"), "weekly": (generate_weekly, "weekly notes written"),
            "sites": (classify_new_domains, "websites sorted")}
    if kind not in jobs:
        return RedirectResponse("/ai", 303)
    job, label = jobs[kind]
    try:
        count = job()
    except AIUnavailable as exc:
        return RedirectResponse("/ai?error=" + quote_plus(str(exc)), 303)
    audit(request, "ai_run", "ai", kind, f"{count} {label}")
    return RedirectResponse("/ai?notice=" + quote_plus(f"{count} {label}."), 303)


@router.post("/browsing/categories")
def set_site_category(request: Request, domain: str = Form(...), category: str = Form(...), back: str = Form("/browsing")):
    """An Admin's choice always wins over the AI's and is never overwritten by it."""
    from app.main import require_permission, audit
    from app.browsing import clean_domain
    require_permission(request, "browsing_manage")
    domain = clean_domain(domain)
    if domain and category in CATEGORIES:
        with get_db() as c:
            c.execute(
                "INSERT INTO browsing_site_categories(domain,category,source,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET category=excluded.category,source='manual',updated_at=excluded.updated_at",
                (domain, category, "manual", int(time.time())),
            )
            audit(request, "browsing_category", "domain", domain, f"Category set to {category}", db=c)
    return RedirectResponse(back if back.startswith("/browsing") else "/browsing", 303)
