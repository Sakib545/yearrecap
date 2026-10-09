"""Presentation layer for BURAQ Smart Attendance.

Everything visual lives here: the icon sprite, the navigation model and the
Jinja2 page renderer. Route handlers keep returning body markup exactly as
before; this module wraps it in the application shell.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Cache-busting token. Changes on every deploy, so a new stylesheet is never
# served from a stale browser cache.
ASSET_VERSION = str(int(time.time()))

# --------------------------------------------------------------------------
# Icons — 24x24 stroke paths, rendered once as a sprite and referenced by <use>
# --------------------------------------------------------------------------

ICON_PATHS: dict[str, str] = {
    "home": '<path d="M3 10.6 12 3.2l9 7.4"/><path d="M5.6 9.3V20a1 1 0 0 0 1 1h10.8a1 1 0 0 0 1-1V9.3"/>',
    "user": '<circle cx="12" cy="8" r="3.4"/><path d="M4.8 20c0-3.5 3.2-5.5 7.2-5.5s7.2 2 7.2 5.5"/>',
    "users": '<circle cx="9.2" cy="8" r="3.2"/><path d="M2.8 20c0-3.3 2.9-5.2 6.4-5.2s6.4 1.9 6.4 5.2"/><path d="M16.6 5.3a3 3 0 0 1 0 5.5"/><path d="M17.8 14.6c2.2.6 3.6 2.2 3.6 4.4"/>',
    "clock": '<circle cx="12" cy="12" r="8.6"/><path d="M12 6.8v5.5l3.4 2"/>',
    "calendar": '<rect x="3.4" y="5" width="17.2" height="15.6" rx="2.4"/><path d="M3.4 10h17.2M8.2 3.2v3.6M15.8 3.2v3.6"/>',
    "calendar-check": '<rect x="3.4" y="5" width="17.2" height="15.6" rx="2.4"/><path d="M3.4 10h17.2M8.2 3.2v3.6M15.8 3.2v3.6"/><path d="M9 14.9 11.2 17 15.4 12.8"/>',
    "calendar-minus": '<rect x="3.4" y="5" width="17.2" height="15.6" rx="2.4"/><path d="M3.4 10h17.2M8.2 3.2v3.6M15.8 3.2v3.6"/><path d="M9.2 15.4h5.6"/>',
    "banknote": '<rect x="2.6" y="6" width="18.8" height="12" rx="2.4"/><circle cx="12" cy="12" r="2.7"/><path d="M6.2 9.6v4.8M17.8 9.6v4.8"/>',
    "trending-up": '<path d="M3.2 16.8 9.6 10.4l3.8 3.8L20.8 6.8"/><path d="M15.4 6.8h5.4v5.4"/>',
    "file-text": '<path d="M13.6 3.2H7.4a2 2 0 0 0-2 2v13.6a2 2 0 0 0 2 2h9.2a2 2 0 0 0 2-2V8.4z"/><path d="M13.6 3.2v5.2h5"/><path d="M8.6 13h6.8M8.6 16.4h4.6"/>',
    "chart-bar": '<path d="M3.4 20.6h17.2"/><path d="M7 20.2v-8.6M12 20.2V5.4M17 20.2v-5.6"/>',
    "shield": '<path d="M12 3 19.4 6v5.6c0 4.3-3 7.7-7.4 9.4-4.4-1.7-7.4-5.1-7.4-9.4V6z"/>',
    "sliders": '<path d="M4 7.2h16M4 12h16M4 16.8h16"/><circle cx="9" cy="7.2" r="2.2"/><circle cx="15" cy="12" r="2.2"/><circle cx="8" cy="16.8" r="2.2"/>',
    "logout": '<path d="M14.6 4.8h3.6a1.8 1.8 0 0 1 1.8 1.8v10.8a1.8 1.8 0 0 1-1.8 1.8h-3.6"/><path d="M9.6 8.4 6 12l3.6 3.6"/><path d="M6 12h9.4"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "panel": '<rect x="3.4" y="4.4" width="17.2" height="15.2" rx="2.4"/><path d="M9.4 4.4v15.2"/>',
    "contrast": '<circle cx="12" cy="12" r="8.6"/><path d="M12 3.4a8.6 8.6 0 0 1 0 17.2z" fill="currentColor" stroke="none"/>',
    "search": '<circle cx="11" cy="11" r="6.6"/><path d="M15.8 15.8 20.6 20.6"/>',
    "plus": '<path d="M12 5.2v13.6M5.2 12h13.6"/>',
    "check": '<path d="M5 12.6 9.8 17.4 19 7.4"/>',
    "x": '<path d="M6.4 6.4 17.6 17.6M17.6 6.4 6.4 17.6"/>',
    "download": '<path d="M12 3.6v10.8M7.6 10.4 12 14.8l4.4-4.4"/><path d="M4.6 19.4h14.8"/>',
    "upload": '<path d="M12 15.4V4.6M7.6 9 12 4.6 16.4 9"/><path d="M4.6 19.4h14.8"/>',
    "lock": '<rect x="4.6" y="10" width="14.8" height="10.2" rx="2.4"/><path d="M8.2 10V7.6a3.8 3.8 0 0 1 7.6 0V10"/>',
    "building": '<rect x="4.6" y="3.6" width="14.8" height="17" rx="2"/><path d="M9 8h2M13 8h2M9 12h2M13 12h2"/><path d="M10.4 20.6v-4h3.2v4"/>',
    "refresh": '<path d="M20.2 12a8.2 8.2 0 1 1-2.5-5.9"/><path d="M20.2 4.4v4.6h-4.6"/>',
    "receipt": '<path d="M6.2 3.6h11.6v17l-2.3-1.5-2.3 1.5-2.3-1.5-2.4 1.5-2.3-1.5z"/><path d="M9.2 8.6h5.6M9.2 12.2h5.6"/>',
    "chevron-right": '<path d="M9.6 5.6 16 12l-6.4 6.4"/>',
}


def icon(name: str) -> Markup:
    """Return a sprite reference for the named icon."""
    if name not in ICON_PATHS:
        name = "chevron-right"
    return Markup(f'<svg class="ic" aria-hidden="true"><use href="#i-{name}"/></svg>')


def sprite() -> Markup:
    symbols = "".join(
        f'<symbol id="i-{name}" viewBox="0 0 24 24">{path}</symbol>'
        for name, path in ICON_PATHS.items()
    )
    return Markup(
        f'<svg xmlns="http://www.w3.org/2000/svg" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true">{symbols}</svg>'
    )


# --------------------------------------------------------------------------
# Legacy glyphs -> icons
#
# Route handlers still emit decorative characters such as "♙" inside their
# markup. Rather than editing 90-odd handlers, they are swapped for real icons
# at render time. Characters that carry meaning as text (৳, ★, •, →) are left
# alone.
# --------------------------------------------------------------------------

GLYPH_ICONS: dict[str, str] = {
    "⌂": "home",
    "♙": "users",
    "◷": "clock",
    "▣": "calendar-check",
    "▢": "calendar-minus",
    "⌁": "trending-up",
    "▤": "file-text",
    "♧": "shield",
    "◎": "user",
    "⚙": "sliders",
    "☂": "calendar-minus",
    "◉": "shield",
    "☰": "menu",
    "◐": "contrast",
    "◔": "chart-bar",
    "⌕": "search",
    "＋": "plus",
    "🔎": "search",
    "👤": "user",
    "🔒": "lock",
    "📊": "chart-bar",
    "🗓": "calendar",
    "📥": "download",
    "✅": "check",
    "🧾": "receipt",
    "🏢": "building",
    "🔄": "refresh",
    "🏖": "calendar-minus",
}

_GLYPH_RE = re.compile("[" + "".join(GLYPH_ICONS) + "]\ufe0f?")


def iconify(html: str) -> str:
    return _GLYPH_RE.sub(lambda m: str(icon(GLYPH_ICONS[m.group()[0]])), html)


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------

# (key, label, url, icon, permission-test)
NAV_BLUEPRINT: list[tuple[str, list[tuple[str, str, str, str, str]]]] = [
    ("Daily work", [
        ("dashboard", "Dashboard", "/dashboard", "home", "dashboard_view"),
        ("attendance", "Attendance", "/attendance", "clock", "attendance"),
        ("duty", "Duty", "/duty", "calendar-check", "duty_view"),
        ("webcheckin", "Employee portal", "/web-checkin", "lock", "attendance_edit"),
    ]),
    ("People", [
        ("employees", "Employees", "/employees", "users", "employees"),
        ("leave", "Leave", "/hr-operations", "calendar-minus", "leave_view"),
        ("payroll", "Payroll", "/payroll", "banknote", "payroll_view"),
        ("salarysheet", "Salary sheet upload", "/payroll/sheet", "receipt", "payroll_manage"),
        ("performance", "Performance", "/performance", "trending-up", "performance_view"),
        ("browsing", "Browsing time", "/browsing", "chart-bar", "browsing_view"),
        ("ai", "AI insights", "/ai", "panel", "ai_view"),
    ]),
    ("Administration", [
        ("duplicates", "Selfie review", "/duplicates", "search", "approvals_view"),
        ("face-security", "Face Security", "/face-security", "shield", "face_security_view"),
        ("reports", "Reports", "/reports", "file-text", "reports_view"),
        ("users", "User accounts", "/hr-accounts", "shield", "user_accounts_view"),
        ("settings", "Settings", "/settings", "sliders", "settings_view"),
    ]),
]

# Some entries need more than a single permission flag.
_COMPOUND = {
    "attendance": ("reports_view", "leave_view", "attendance_edit"),
    "employees": ("employees_view", "performance_view"),
}

# Route "active" keys that belong to a parent nav entry.
ACTIVE_ALIASES = {
    "pending": "duty",
    "operations": "leave",
    "hr": "users",
    "audit": "users",
    "account": "",
}


def build_nav(perm: Callable[[str], bool], badges: dict[str, int] | None = None) -> list[dict]:
    badges = badges or {}
    groups = []
    for label, entries in NAV_BLUEPRINT:
        items = []
        for key, text, url, ic, flag in entries:
            allowed = any(perm(p) for p in _COMPOUND[flag]) if flag in _COMPOUND else perm(flag)
            if allowed:
                items.append({"key": key, "label": text, "url": url, "icon": ic,
                              "badge": int(badges.get(key) or 0)})
        groups.append({"label": label, "entries": items})
    return groups


def build_tabbar(groups: list[dict]) -> list[dict]:
    flat = [item for group in groups for item in group["entries"]]
    return flat[:4]


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.globals["icon"] = icon


def initials_of(name: str) -> str:
    parts = [p for p in str(name).split() if p]
    return ("".join(p[0] for p in parts[:2]) or "U").upper()


def render_page(
    *,
    title: str,
    body: str,
    chrome: bool = False,
    nav_groups: list[dict] | None = None,
    active: str = "",
    user_name: str = "",
    role_label: str = "",
    today_line: str = "",
) -> str:
    nav_groups = nav_groups or []
    return env.get_template("base.html").render(
        title=title,
        body=Markup(iconify(body)),
        sprite=sprite(),
        chrome=chrome,
        nav_groups=nav_groups,
        tabbar=build_tabbar(nav_groups),
        active=ACTIVE_ALIASES.get(active, active),
        user_name=user_name,
        role_label=role_label,
        initials=initials_of(user_name),
        today_line=today_line,
        asset_v=ASSET_VERSION,
    )
