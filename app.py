"""Exam Materials Inventory — Flask backend.

Key guarantees
* Stock only changes inside database transactions.
* A checkout deducts with `UPDATE ... WHERE quantity >= requested`, so stock can never go
  negative, even with simultaneous checkouts. The whole cart is one transaction.
* Every checkout carries a one-time token (unique in the DB), so double-clicks, retries and
  page refreshes can never deduct twice.
* Reversals/corrections are guarded by the checkout's status row, so stock is restored once.
* Every stock change is written to the `movements` audit table.
* Permissions are enforced here on the server, not just hidden in the UI.
"""
import csv
import io
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import mailer
import qr
from db import DB, FOR_UPDATE, IS_PG, IntegrityError, init_schema, utcnow

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Normally the web files live in ./static. If they were uploaded flat next to app.py
# (easy to do via GitHub's web uploader), serve them from the top folder instead.
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.isfile(os.path.join(STATIC_DIR, "shop.html")):
    STATIC_DIR = BASE_DIR
WEB_EXTS = (".js", ".css", ".html")
TYPES = {"record_form": "Record Form", "response_booklet": "Response Booklet", "test_kit": "Test Kit"}
RETURNABLE = "test_kit"  # items of this type come back; their stock is added back on return
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?)?$")

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _bootstrap():
    d = DB()
    try:
        init_schema(d)
        key = os.environ.get("SECRET_KEY", "").strip()
        if not key:  # generated once, stored in the DB so every worker/restart shares it
            d.execute(
                "INSERT INTO settings(key,value) VALUES('secret_key',?) ON CONFLICT(key) DO NOTHING",
                (secrets.token_hex(32),),
            )
            key = d.execute("SELECT value FROM settings WHERE key='secret_key'").one()["value"]
        return key
    finally:
        d.close()


app.config.update(
    SECRET_KEY=_bootstrap(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("INSECURE_COOKIES") != "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=256 * 1024,
    MAIL_SYNC=False,
)

# --------------------------------------------------------------------------- plumbing


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify(error=e.message, **e.extra), e.status


@app.errorhandler(404)
def _404(e):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found."), 404
    return Response("Not found", 404)


@app.errorhandler(413)
def _413(e):
    return jsonify(error="Request too large."), 413


@app.errorhandler(Exception)
def _500(e):
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error")
    msg = "The database is busy — please try again." if "locked" in str(e).lower() else "Something went wrong. Please try again."
    return jsonify(error=msg), 503 if "locked" in str(e).lower() else 500


def get_db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    d = g.pop("db", None)
    if d:
        d.close()


@contextmanager
def tx(d):
    d.begin()
    try:
        yield
        d.commit()
    except BaseException:
        d.rollback()
        raise


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def _csrf_guard():
    if request.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        tok = session.get("csrf", "")
        sent = request.headers.get("X-CSRF-Token", "")
        if not tok or not secrets.compare_digest(tok, sent):
            raise ApiError("Your session expired. Please refresh the page and try again.", 403)


def body():
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        raise ApiError("Invalid request.")
    return b


def s(v, name, maxlen=200, required=False):
    v = "" if v is None else str(v).strip()
    if required and not v:
        raise ApiError(f"{name} is required.")
    if len(v) > maxlen:
        raise ApiError(f"{name} is too long (max {maxlen} characters).")
    return v


def n(v, name, lo=0, hi=1_000_000):
    try:
        if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
            raise ValueError
        x = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a whole number.")
    if x < lo or x > hi:
        raise ApiError(f"{name} must be between {lo} and {hi}.")
    return x


# simple in-memory rate limiting (per process)
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rl_blocked(key, limit, window):
    now = time.time()
    with _hits_lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.popleft()
        return len(q) >= limit


def rl_add(key):
    with _hits_lock:
        _hits[key].append(time.time())


def client_ip():
    return request.remote_addr or "?"


# --------------------------------------------------------------------------- settings


def get_setting(d, key, default=""):
    r = d.execute("SELECT value FROM settings WHERE key=?", (key,)).one()
    return r["value"] if r else default


def set_setting(d, key, value):
    d.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def parse_recipients(text):
    out, seen = [], set()
    for part in re.split(r"[,;\s]+", text or ""):
        p = part.strip().lower()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def base_url(d):
    url = get_setting(d, "public_url") or os.environ.get("PUBLIC_URL", "").strip()
    if not url:
        try:
            url = request.url_root
        except RuntimeError:
            url = get_setting(d, "detected_url")
    return url.rstrip("/")


# --------------------------------------------------------------------------- users / auth


def current_user():
    if "user" in g:
        return g.user
    u = None
    uid = session.get("uid")
    if uid:
        u = get_db().execute("SELECT id,email,name,role,active FROM users WHERE id=?", (uid,)).one()
        if u and not u["active"]:
            u = None
    g.user = u
    return u


def public_user(u):
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]} if u else None


def owner_required(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if not u:
            raise ApiError("Please sign in.", 401)
        if u["role"] != "owner":
            raise ApiError("Owner access required.", 403)
        return fn(*a, **k)

    return w


def has_owner(d):
    return bool(d.execute("SELECT 1 AS x FROM users WHERE role='owner' LIMIT 1").one())


def new_csrf():
    session["csrf"] = secrets.token_urlsafe(24)
    session.permanent = True
    return session["csrf"]


@app.get("/api/session")
def api_session():
    d = get_db()
    if "csrf" not in session:
        new_csrf()
    needs_setup = not has_owner(d)
    return jsonify(
        csrf=session["csrf"],
        user=public_user(current_user()),
        needs_setup=needs_setup,
        setup_key_configured=bool(os.environ.get("SETUP_KEY", "").strip()),
        allow_guest=get_setting(d, "allow_guest", "1") == "1",
    )


def check_password_rules(pw, minlen):
    if len(pw or "") < minlen:
        raise ApiError(f"Password must be at least {minlen} characters.")
    if len(pw) > 200:
        raise ApiError("Password is too long.")


@app.post("/api/setup")
def api_setup():
    d = get_db()
    b = body()
    key = os.environ.get("SETUP_KEY", "").strip()
    if not key:
        raise ApiError("Setup is locked: set a SETUP_KEY environment variable on the server first (see README).", 403)
    if rl_blocked("setup:" + client_ip(), 10, 900):
        raise ApiError("Too many attempts. Try again later.", 429)
    if not secrets.compare_digest(str(b.get("setup_key", "")), key):
        rl_add("setup:" + client_ip())
        raise ApiError("That setup key is not correct.", 403)
    name = s(b.get("name"), "Name", 100, True)
    email = s(b.get("email"), "Email", 200, True).lower()
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    pw = str(b.get("password", ""))
    check_password_rules(pw, 10)
    try:
        with tx(d):
            if has_owner(d):
                raise ApiError("An owner account already exists. Sign in instead.", 409)
            d.execute(
                "INSERT INTO users(email,name,password_hash,role,active,created_at) VALUES(?,?,?,'owner',1,?)",
                (email, name, generate_password_hash(pw), utcnow()),
            )
    except IntegrityError:
        raise ApiError("That email is already registered.", 409)
    u = d.execute("SELECT id,email,name,role FROM users WHERE email=?", (email,)).one()
    session.clear()
    session["uid"] = u["id"]
    return jsonify(ok=True, user=public_user(u), csrf=new_csrf())


@app.post("/api/login")
def api_login():
    d = get_db()
    b = body()
    email = s(b.get("email"), "Email", 200).lower()
    k1, k2 = "login-ip:" + client_ip(), "login-em:" + email
    if rl_blocked(k1, 60, 900) or rl_blocked(k2, 8, 900):
        raise ApiError("Too many failed sign-in attempts. Please wait 15 minutes and try again.", 429)
    u = d.execute("SELECT * FROM users WHERE email=?", (email,)).one()
    ok = bool(u) and u["active"] and check_password_hash(u["password_hash"], str(b.get("password", "")))
    if not ok:
        rl_add(k1)
        rl_add(k2)
        raise ApiError("Email or password is incorrect.", 401)
    session.clear()
    session["uid"] = u["id"]
    return jsonify(ok=True, user=public_user(u), csrf=new_csrf())


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True, csrf=new_csrf())


@app.post("/api/register")
def api_register():
    d = get_db()
    b = body()
    if rl_blocked("reg:" + client_ip(), 10, 3600):
        raise ApiError("Too many sign-ups from this network. Try again later.", 429)
    name = s(b.get("name"), "Name", 100, True)
    email = s(b.get("email"), "Email", 200, True).lower()
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    pw = str(b.get("password", ""))
    check_password_rules(pw, 8)
    rl_add("reg:" + client_ip())
    try:
        r = d.execute(
            "INSERT INTO users(email,name,password_hash,role,active,created_at) VALUES(?,?,?,'user',1,?) RETURNING id",
            (email, name, generate_password_hash(pw), utcnow()),
        ).one()
    except IntegrityError:
        raise ApiError("That email already has an account. Try signing in.", 409)
    session.clear()
    session["uid"] = r["id"]
    return jsonify(ok=True, user={"id": r["id"], "name": name, "email": email, "role": "user"}, csrf=new_csrf())


@app.post("/api/account/password")
def api_change_password():
    u = current_user()
    if not u:
        raise ApiError("Please sign in.", 401)
    b = body()
    d = get_db()
    row = d.execute("SELECT password_hash, role FROM users WHERE id=?", (u["id"],)).one()
    if not check_password_hash(row["password_hash"], str(b.get("current", ""))):
        raise ApiError("Your current password is not correct.", 403)
    pw = str(b.get("new", ""))
    check_password_rules(pw, 10 if row["role"] == "owner" else 8)
    d.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), u["id"]))
    return jsonify(ok=True)


# --------------------------------------------------------------------------- inventory helpers

RET = "RETURNING id, exam, edition, material_type, form_name, quantity, low_threshold, alert_active, archived"
ITEM_COLS = (
    "id, exam, edition, material_type, form_name, quantity, low_threshold, location, notes, "
    "archived, alert_active, created_at, updated_at"
)


def status_of(q, thr):
    return "out" if q <= 0 else ("low" if q <= thr else "in")


def label_of(r):
    t = TYPES.get(r["material_type"], r["material_type"])
    s_ = f"{r['exam']}"
    if r.get("edition"):
        s_ += f" ({r['edition']})"
    s_ += f" — {t}"
    if r.get("form_name"):
        s_ += f": {r['form_name']}"
    return s_


def item_json(r, admin=False):
    o = {
        "id": r["id"],
        "exam": r["exam"],
        "edition": r["edition"],
        "material_type": r["material_type"],
        "type_name": TYPES.get(r["material_type"], r["material_type"]),
        "form_name": r["form_name"],
        "quantity": r["quantity"],
        "location": r["location"],
        "status": status_of(r["quantity"], r["low_threshold"]),
        "label": label_of(r),
    }
    if admin:
        o.update(
            low_threshold=r["low_threshold"],
            notes=r["notes"],
            archived=bool(r["archived"]),
            alert_active=bool(r["alert_active"]),
            updated_at=r["updated_at"],
            last_alert_status=r.get("last_alert_status"),
            last_alert_at=r.get("last_alert_at"),
        )
    return o


def add_movement(d, item_id, prev, new, kind, reason="", checkout_id=None, actor_name="", actor_id=None):
    d.execute(
        "INSERT INTO movements(item_id,delta,prev_qty,new_qty,kind,reason,checkout_id,actor_name,actor_user_id,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (item_id, new - prev, prev, new, kind, reason, checkout_id, actor_name, actor_id, utcnow()),
    )


def apply_alert(d, row, trigger, alerts, notify=True):
    """Low-stock state machine. Alert once when quantity first reaches/falls below the
    threshold; reset only when it is back above the threshold."""
    qty, thr = row["quantity"], row["low_threshold"]
    if qty <= thr:
        if row["archived"]:
            return
        claimed = d.execute(
            "UPDATE items SET alert_active=1 WHERE id=? AND alert_active=0 RETURNING id", (row["id"],)
        ).one()
        if claimed and notify:
            a = d.execute(
                "INSERT INTO alerts(item_id,label,quantity,threshold,trigger,status,created_at)"
                " VALUES(?,?,?,?,?,'pending',?) RETURNING id",
                (row["id"], label_of(row), qty, thr, trigger, utcnow()),
            ).one()
            alerts.append(a["id"])
    else:
        d.execute("UPDATE items SET alert_active=0 WHERE id=? AND alert_active=1", (row["id"],))


def dispatch_alerts(alert_ids):
    if not alert_ids:
        return
    if app.config.get("MAIL_SYNC"):
        send_alerts_job(alert_ids)
    else:
        threading.Thread(target=send_alerts_job, args=(alert_ids,), daemon=True).start()


def send_alerts_job(alert_ids):
    """Sends ONE email covering all the alert rows, then records the outcome on each row."""
    d = DB()
    try:
        ph = ",".join("?" * len(alert_ids))
        rows = d.execute(f"SELECT * FROM alerts WHERE id IN ({ph}) ORDER BY id", tuple(alert_ids)).all()
        if not rows:
            return
        recips = parse_recipients(get_setting(d, "alert_recipients"))
        status, err = "sent", ""
        if not recips:
            status, err = "not_configured", "No alert recipients are set (Settings → Alert emails)."
        elif not mailer.configured():
            status, err = "not_configured", "Email service is not configured on the server (see README)."
        else:
            base = base_url(d)
            lines = [
                f"• {r['label']}: {r['quantity']} left (alert level {r['threshold']})"
                + (" — OUT OF STOCK" if r["quantity"] <= 0 else "")
                for r in rows
            ]
            subject = (
                f"Low stock: {rows[0]['label']}" if len(rows) == 1 else f"Low stock: {len(rows)} items need reordering"
            )
            text = (
                "These exam materials have reached their low-stock level:\n\n"
                + "\n".join(lines)
                + (f"\n\nOpen the dashboard: {base}/admin" if base else "")
                + "\n\nYou will not get another alert for an item until it has been restocked above its level."
            )
            ok, err = mailer.send(recips, subject, text)
            status = "sent" if ok else "failed"
        d.execute(
            f"UPDATE alerts SET status=?, recipients=?, error=? WHERE id IN ({ph})",
            (status, ", ".join(recips), err, *alert_ids),
        )
    finally:
        d.close()


def parse_item(b, creating):
    exam = s(b.get("exam"), "Exam name", 120, True)
    mt = b.get("material_type")
    if mt not in TYPES:
        raise ApiError("Choose a material type: record form, response booklet, or test kit.")
    out = {
        "exam": exam,
        "edition": s(b.get("edition"), "Edition/version", 80),
        "material_type": mt,
        "form_name": s(b.get("form_name"), "Form/booklet name", 120),
        "low_threshold": n(b.get("low_threshold", 5), "Low-stock level", 0, 1_000_000),
        "location": s(b.get("location"), "Storage location", 120),
        "notes": s(b.get("notes"), "Notes", 500),
    }
    if creating:
        out["quantity"] = n(b.get("quantity", 0), "Starting quantity", 0, 1_000_000)
    return out


def dup_item(d, it, exclude_id=0):
    return d.execute(
        "SELECT id FROM items WHERE lower(exam)=? AND lower(edition)=? AND material_type=? AND lower(form_name)=? AND id<>?",
        (it["exam"].lower(), it["edition"].lower(), it["material_type"], it["form_name"].lower(), exclude_id),
    ).one()


def insert_item(d, it, actor):
    now = utcnow()
    row = d.execute(
        "INSERT INTO items(exam,edition,material_type,form_name,quantity,low_threshold,location,notes,archived,alert_active,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,0,0,?,?) " + RET,
        (it["exam"], it["edition"], it["material_type"], it["form_name"], it["quantity"], it["low_threshold"],
         it["location"], it["notes"], now, now),
    ).one()
    if it["quantity"] > 0:
        add_movement(d, row["id"], 0, it["quantity"], "initial", "Item created", None, actor["name"], actor["id"])
    apply_alert(d, row, "item created", [], notify=False)  # flagged on dashboard, no email for brand-new items
    return row["id"]


# --------------------------------------------------------------------------- public: browse + checkout


@app.get("/api/items")
def api_items():
    rows = get_db().execute(
        f"SELECT {ITEM_COLS} FROM items WHERE archived=0 ORDER BY lower(exam), lower(edition), material_type, lower(form_name)"
    ).all()
    return jsonify(items=[item_json(r) for r in rows])


def receipt_for(d, token):
    c = d.execute(
        "SELECT id, token, name, email, status, edited, created_at, reversed_at FROM checkouts WHERE token=?", (token,)
    ).one()
    if not c:
        return None
    lines = d.execute(
        "SELECT id, item_id, exam, edition, material_type, form_name, qty, returned_qty FROM checkout_lines WHERE checkout_id=? ORDER BY id",
        (c["id"],),
    ).all()
    c["lines"] = [
        {**l, "label": label_of(l), "type_name": TYPES.get(l["material_type"], l["material_type"])}
        for l in lines
        if l["qty"] > 0
    ]
    c["total_units"] = sum(l["qty"] for l in c["lines"])
    return c


@app.get("/api/receipt/<token>")
def api_receipt(token):
    if not TOKEN_RE.match(token):
        raise ApiError("Not found.", 404)
    r = receipt_for(get_db(), token)
    if not r:
        raise ApiError("Receipt not found.", 404)
    r.pop("email", None)
    return jsonify(receipt=r)


@app.post("/api/checkout")
def api_checkout():
    d = get_db()
    b = body()
    user = current_user()
    # Generous on purpose: a whole school often shares one public IP address.
    if rl_blocked("co:" + client_ip(), int(os.environ.get("CHECKOUT_LIMIT_PER_MIN", "150")), 60):
        raise ApiError("Too many requests. Please wait a moment.", 429)
    rl_add("co:" + client_ip())
    if not user and get_setting(d, "allow_guest", "1") != "1":
        raise ApiError("Please sign in to check out.", 401)
    token = str(b.get("token", ""))
    if not TOKEN_RE.match(token):
        raise ApiError("Invalid checkout request. Please refresh the page.")
    name = s(b.get("name") or (user["name"] if user else ""), "Name", 100, True)
    email = s(b.get("email") or (user["email"] if user else ""), "Email", 200, True).lower()
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    raw = b.get("lines")
    if not isinstance(raw, list) or not raw:
        raise ApiError("Your cart is empty.")
    if len(raw) > 60:
        raise ApiError("Too many different items in one cart.")
    lines = {}
    for ln in raw:
        if not isinstance(ln, dict):
            raise ApiError("Invalid cart.")
        iid = n(ln.get("item_id"), "Item", 1, 10**12)
        qty = n(ln.get("qty"), "Quantity", 1, 10_000)  # rejects zero / negative
        lines[iid] = lines.get(iid, 0) + qty

    alerts = []
    now = utcnow()
    try:
        with tx(d):
            if d.execute("SELECT 1 AS x FROM checkouts WHERE token=?", (token,)).one():
                return jsonify(ok=True, duplicate=True, receipt=_pub_receipt(receipt_for(d, token)))
            cid = d.execute(
                "INSERT INTO checkouts(token,user_id,name,email,status,created_at) VALUES(?,?,?,?,'completed',?) RETURNING id",
                (token, user["id"] if user else None, name, email, now),
            ).one()["id"]
            problems = []
            for item_id in sorted(lines):  # consistent lock order avoids deadlocks
                qty = lines[item_id]
                row = d.execute(
                    "UPDATE items SET quantity=quantity-?, updated_at=? WHERE id=? AND archived=0 AND quantity>=? " + RET,
                    (qty, now, item_id, qty),
                ).one()
                if not row:
                    cur = d.execute(
                        "SELECT id, exam, edition, material_type, form_name, quantity, archived FROM items WHERE id=?",
                        (item_id,),
                    ).one()
                    if not cur or cur["archived"]:
                        problems.append({"item_id": item_id, "label": label_of(cur) if cur else f"Item #{item_id}",
                                         "requested": qty, "available": 0, "reason": "unavailable"})
                    else:
                        problems.append({"item_id": item_id, "label": label_of(cur), "requested": qty,
                                         "available": cur["quantity"],
                                         "reason": "out_of_stock" if cur["quantity"] <= 0 else "not_enough"})
                    continue
                d.execute(
                    "INSERT INTO checkout_lines(checkout_id,item_id,exam,edition,material_type,form_name,qty,original_qty)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (cid, item_id, row["exam"], row["edition"], row["material_type"], row["form_name"], qty, qty),
                )
                add_movement(d, item_id, row["quantity"] + qty, row["quantity"], "checkout",
                             "", cid, name, user["id"] if user else None)
                apply_alert(d, row, "checkout", alerts)
            if problems:
                raise ApiError(
                    "Some items are no longer available in the quantity you asked for. Nothing was taken — please update your cart.",
                    409, problems=problems)
    except IntegrityError:
        # Another request with the same token won the race: return its result, deduct nothing.
        existing = receipt_for(DB_fresh(d), token)
        if existing:
            return jsonify(ok=True, duplicate=True, receipt=_pub_receipt(existing))
        raise
    dispatch_alerts(alerts)
    return jsonify(ok=True, duplicate=False, receipt=_pub_receipt(receipt_for(d, token)))


def DB_fresh(d):
    return d  # connection is autocommit and the failed transaction was rolled back


def _pub_receipt(r):
    r = dict(r)
    r.pop("email", None)
    return r


# --------------------------------------------------------------------------- owner: items


@app.get("/api/admin/items")
@owner_required
def admin_items():
    rows = get_db().execute(
        f"SELECT {ITEM_COLS},"
        " (SELECT status FROM alerts a WHERE a.item_id=items.id ORDER BY a.id DESC LIMIT 1) AS last_alert_status,"
        " (SELECT created_at FROM alerts a WHERE a.item_id=items.id ORDER BY a.id DESC LIMIT 1) AS last_alert_at"
        " FROM items ORDER BY archived, lower(exam), lower(edition), material_type, lower(form_name)"
    ).all()
    return jsonify(items=[item_json(r, True) for r in rows])


def _get_item_admin(d, item_id):
    r = d.execute(f"SELECT {ITEM_COLS} FROM items WHERE id=?", (item_id,)).one()
    if not r:
        raise ApiError("Item not found.", 404)
    return item_json(r, True)


@app.post("/api/admin/items")
@owner_required
def admin_create_item():
    d = get_db()
    it = parse_item(body(), True)
    u = current_user()
    with tx(d):
        if dup_item(d, it):
            raise ApiError("That exam / edition / type / form already exists. Edit the existing item instead.", 409)
        iid = insert_item(d, it, u)
    return jsonify(item=_get_item_admin(d, iid)), 201


@app.put("/api/admin/items/<int:item_id>")
@owner_required
def admin_edit_item(item_id):
    d = get_db()
    it = parse_item(body(), False)
    alerts = []
    with tx(d):
        if not d.execute("SELECT 1 AS x FROM items WHERE id=?", (item_id,)).one():
            raise ApiError("Item not found.", 404)
        if dup_item(d, it, item_id):
            raise ApiError("Another item already has that exam / edition / type / form.", 409)
        row = d.execute(
            "UPDATE items SET exam=?, edition=?, material_type=?, form_name=?, low_threshold=?, location=?, notes=?, updated_at=?"
            " WHERE id=? " + RET,
            (it["exam"], it["edition"], it["material_type"], it["form_name"], it["low_threshold"], it["location"],
             it["notes"], utcnow(), item_id),
        ).one()
        apply_alert(d, row, "threshold change", alerts)
    dispatch_alerts(alerts)
    return jsonify(item=_get_item_admin(d, item_id))


@app.post("/api/admin/items/<int:item_id>/archive")
@owner_required
def admin_archive_item(item_id):
    d = get_db()
    flag = 1 if body().get("archived", True) else 0
    with tx(d):
        row = d.execute("UPDATE items SET archived=?, updated_at=? WHERE id=? " + RET, (flag, utcnow(), item_id)).one()
        if not row:
            raise ApiError("Item not found.", 404)
        if flag == 0:
            apply_alert(d, row, "restored", [], notify=False)
    return jsonify(item=_get_item_admin(d, item_id))


@app.post("/api/admin/items/<int:item_id>/adjust")
@owner_required
def admin_adjust(item_id):
    d = get_db()
    b = body()
    u = current_user()
    mode = b.get("mode")
    if mode not in ("add", "remove", "set"):
        raise ApiError("Choose add, remove, or set exact count.")
    reason = s(b.get("reason"), "A reason", 300, True)
    amount = n(b.get("amount"), "Amount", 0 if mode == "set" else 1, 1_000_000)
    alerts = []
    with tx(d):
        cur = d.execute("SELECT quantity FROM items WHERE id=?" + FOR_UPDATE, (item_id,)).one()
        if not cur:
            raise ApiError("Item not found.", 404)
        prev = cur["quantity"]
        if mode == "add":
            new = prev + amount
        elif mode == "remove":
            if amount > prev:
                raise ApiError(f"Only {prev} on hand — you can't remove {amount}.", 409)
            new = prev - amount
        else:
            new = amount
        row = d.execute("UPDATE items SET quantity=?, updated_at=? WHERE id=? " + RET, (new, utcnow(), item_id)).one()
        kind = {"add": "restock", "remove": "removed", "set": "count"}[mode]
        add_movement(d, item_id, prev, new, kind, reason, None, u["name"], u["id"])
        apply_alert(d, row, "manual adjustment", alerts)
    dispatch_alerts(alerts)
    return jsonify(item=_get_item_admin(d, item_id))


# --------------------------------------------------------------------------- owner: checkouts


def _date_arg(name):
    v = (request.args.get(name) or "").strip()
    if v and not DATE_RE.match(v):
        raise ApiError(f"Invalid {name} date.")
    return v


def checkout_where():
    where, params = [], []
    q = (request.args.get("q") or "").strip().lower()
    if q:
        where.append("(lower(c.name) LIKE ? OR lower(c.email) LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    frm, to = _date_arg("from"), _date_arg("to")
    if frm:
        where.append("c.created_at >= ?")
        params.append(frm)
    if to:
        where.append("c.created_at <= ?")
        params.append(to)
    if request.args.get("status") in ("completed", "reversed"):
        where.append("c.status=?")
        params.append(request.args["status"])
    exam = (request.args.get("exam") or "").strip().lower()
    if exam:
        where.append("EXISTS (SELECT 1 FROM checkout_lines l WHERE l.checkout_id=c.id AND lower(l.exam) LIKE ?)")
        params.append(f"%{exam}%")
    item = (request.args.get("item") or "").strip().lower()
    if item:
        where.append(
            "EXISTS (SELECT 1 FROM checkout_lines l WHERE l.checkout_id=c.id AND "
            "(lower(l.form_name) LIKE ? OR lower(l.edition) LIKE ? OR lower(l.material_type) LIKE ?))"
        )
        params += [f"%{item}%"] * 3
    return (" AND ".join(where) or "1=1"), params


def load_checkouts(d, limit):
    w, p = checkout_where()
    cos = d.execute(
        "SELECT c.id, c.token, c.name, c.email, c.status, c.edited, c.created_at, c.reversed_at, c.reversed_by, c.reverse_reason"
        f" FROM checkouts c WHERE {w} ORDER BY c.id DESC LIMIT ?",
        (*p, limit),
    ).all()
    if not cos:
        return []
    ids = [c["id"] for c in cos]
    ph = ",".join("?" * len(ids))
    lines = d.execute(
        f"SELECT id, checkout_id, item_id, exam, edition, material_type, form_name, qty, original_qty, returned_qty FROM checkout_lines WHERE checkout_id IN ({ph}) ORDER BY id",
        tuple(ids),
    ).all()
    by = defaultdict(list)
    for l in lines:
        l["label"] = label_of(l)
        l["type_name"] = TYPES.get(l["material_type"], l["material_type"])
        by[l["checkout_id"]].append(l)
    for c in cos:
        c["lines"] = by[c["id"]]
        c["total_units"] = sum(l["qty"] for l in c["lines"]) if c["status"] == "completed" else 0
    return cos


@app.get("/api/admin/checkouts")
@owner_required
def admin_checkouts():
    limit = n(request.args.get("limit", 200), "Limit", 1, 1000)
    return jsonify(checkouts=load_checkouts(get_db(), limit))


@app.post("/api/admin/checkouts/<int:cid>/reverse")
@owner_required
def admin_reverse(cid):
    d = get_db()
    u = current_user()
    reason = s(body().get("reason"), "A reason", 300, True)
    alerts = []
    with tx(d):
        # The status guard is what makes "restore exactly once" true, even for double clicks.
        c = d.execute(
            "UPDATE checkouts SET status='reversed', reversed_at=?, reversed_by=?, reverse_reason=?"
            " WHERE id=? AND status='completed' RETURNING id, name",
            (utcnow(), u["name"], reason, cid),
        ).one()
        if not c:
            raise ApiError("This checkout was not found or has already been reversed.", 409)
        lines = d.execute(
            # Only what is still out: kits already returned were put back on the shelf by the return.
            "SELECT item_id, qty - returned_qty AS qty FROM checkout_lines WHERE checkout_id=? AND qty-returned_qty>0 ORDER BY item_id", (cid,)
        ).all()
        for l in lines:
            row = d.execute(
                "UPDATE items SET quantity=quantity+?, updated_at=? WHERE id=? " + RET, (l["qty"], utcnow(), l["item_id"])
            ).one()
            add_movement(d, l["item_id"], row["quantity"] - l["qty"], row["quantity"], "checkout_reversal",
                         reason, cid, u["name"], u["id"])
            apply_alert(d, row, "checkout reversal", alerts)
    dispatch_alerts(alerts)
    return jsonify(ok=True, checkout=load_one(d, cid))


def load_one(d, cid):
    w = d.execute("SELECT token FROM checkouts WHERE id=?", (cid,)).one()
    return receipt_for(d, w["token"])


@app.post("/api/admin/checkouts/<int:cid>/correct")
@owner_required
def admin_correct(cid):
    d = get_db()
    u = current_user()
    b = body()
    reason = s(b.get("reason"), "A reason", 300, True)
    changes = b.get("lines")
    if not isinstance(changes, list) or not changes:
        raise ApiError("Nothing to change.")
    alerts = []
    with tx(d):
        c = d.execute("UPDATE checkouts SET edited=1 WHERE id=? AND status='completed' RETURNING id", (cid,)).one()
        if not c:
            raise ApiError("This checkout was not found or has already been reversed.", 409)
        lines = {l["id"]: l for l in d.execute(
            "SELECT id, item_id, exam, edition, material_type, form_name, qty, returned_qty FROM checkout_lines WHERE checkout_id=?", (cid,)
        ).all()}
        want = {}
        for ch in changes:
            lid = n(ch.get("line_id"), "Line", 1, 10**12)
            if lid not in lines:
                raise ApiError("That line does not belong to this checkout.")
            want[lid] = n(ch.get("qty"), "Quantity", 0, 10_000)
            if want[lid] < lines[lid]["returned_qty"]:
                raise ApiError(
                    f"{lines[lid]['returned_qty']} of “{label_of(lines[lid])}” were already returned, so the quantity taken can't be less than that."
                )
        if sum(want.get(i, l["qty"]) for i, l in lines.items()) <= 0:
            raise ApiError("To return everything, use “Reverse checkout” instead.")
        problems = []
        for lid in sorted(lines, key=lambda i: (lines[i]["item_id"], i)):
            l = lines[lid]
            newq = want.get(lid, l["qty"])
            delta = newq - l["qty"]  # >0 means they took more, <0 means some come back
            if delta == 0:
                continue
            if delta > 0:
                row = d.execute(
                    "UPDATE items SET quantity=quantity-?, updated_at=? WHERE id=? AND archived=0 AND quantity>=? " + RET,
                    (delta, utcnow(), l["item_id"], delta),
                ).one()
                if not row:
                    cur = d.execute("SELECT quantity FROM items WHERE id=?", (l["item_id"],)).one()
                    problems.append({"item_id": l["item_id"], "label": label_of(l), "requested": delta,
                                     "available": cur["quantity"] if cur else 0, "reason": "not_enough"})
                    continue
            else:
                row = d.execute(
                    "UPDATE items SET quantity=quantity+?, updated_at=? WHERE id=? " + RET, (-delta, utcnow(), l["item_id"])
                ).one()
            d.execute("UPDATE checkout_lines SET qty=? WHERE id=?", (newq, lid))
            add_movement(d, l["item_id"], row["quantity"] + delta, row["quantity"], "checkout_correction",
                         reason, cid, u["name"], u["id"])
            apply_alert(d, row, "checkout correction", alerts)
        if problems:
            raise ApiError("Not enough stock to increase that checkout.", 409, problems=problems)
    dispatch_alerts(alerts)
    return jsonify(ok=True, checkout=load_one(d, cid))


# --------------------------------------------------------------------------- test kits: returns


def do_return(d, line_id, qty, actor_name, actor_id, reason, alerts, email=None):
    """Put `qty` of a checked-out test kit back into stock. Must run inside tx().

    Safe against double returns: the line is only updated while `qty - returned_qty >= qty`,
    and the checkout row is locked first so a simultaneous reversal can't also restore it."""
    sql = ("SELECT l.id, l.checkout_id, l.item_id FROM checkout_lines l JOIN checkouts c ON c.id=l.checkout_id"
           " WHERE l.id=? AND l.material_type=?")
    params = [line_id, RETURNABLE]
    if email is not None:
        sql += " AND c.email=?"
        params.append(email)
    ln = d.execute(sql, tuple(params)).one()
    if not ln:
        raise ApiError("That kit wasn't found for this email address.", 404)
    if not d.execute("UPDATE checkouts SET edited=edited WHERE id=? AND status='completed' RETURNING id",
                     (ln["checkout_id"],)).one():
        raise ApiError("That checkout was cancelled by an administrator, so there is nothing to return.", 409)
    upd = d.execute(
        "UPDATE checkout_lines SET returned_qty=returned_qty+? WHERE id=? AND qty-returned_qty>=? RETURNING id, qty, returned_qty",
        (qty, line_id, qty),
    ).one()
    if not upd:
        cur = d.execute("SELECT qty-returned_qty AS out FROM checkout_lines WHERE id=?", (line_id,)).one()
        out = cur["out"] if cur else 0
        raise ApiError("That kit has already been returned." if out <= 0 else f"Only {out} of that kit {'is' if out == 1 else 'are'} still out.", 409)
    row = d.execute("UPDATE items SET quantity=quantity+?, updated_at=? WHERE id=? " + RET, (qty, utcnow(), ln["item_id"])).one()
    add_movement(d, ln["item_id"], row["quantity"] - qty, row["quantity"], "return", reason, ln["checkout_id"],
                 actor_name, actor_id)
    apply_alert(d, row, "kit returned", alerts)  # re-arms the low-stock alert if we're back above the level
    return row


def loans_for(d, email=None):
    sql = ("SELECT l.id AS line_id, l.exam, l.edition, l.material_type, l.form_name, l.qty, l.returned_qty,"
           " l.qty-l.returned_qty AS outstanding, c.id AS checkout_id, c.name, c.email, c.created_at"
           " FROM checkout_lines l JOIN checkouts c ON c.id=l.checkout_id"
           " WHERE c.status='completed' AND l.material_type=? AND l.qty-l.returned_qty>0")
    params = [RETURNABLE]
    if email is not None:
        sql += " AND c.email=?"
        params.append(email)
    rows = d.execute(sql + " ORDER BY c.created_at, l.id", tuple(params)).all()
    for r in rows:
        r["label"] = label_of(r)
    return rows


@app.post("/api/loans/lookup")
def api_loans_lookup():
    d = get_db()
    b = body()
    user = current_user()
    if rl_blocked("lookup:" + client_ip(), 40, 900):
        raise ApiError("Too many lookups. Please wait a few minutes.", 429)
    rl_add("lookup:" + client_ip())
    email = s(b.get("email") or (user["email"] if user else ""), "Email", 200, True).lower()
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    loans = [{k: r[k] for k in ("line_id", "label", "outstanding", "created_at", "name")} for r in loans_for(d, email)]
    return jsonify(loans=loans)


@app.post("/api/return")
def api_return():
    d = get_db()
    b = body()
    user = current_user()
    if rl_blocked("ret:" + client_ip(), 60, 60):
        raise ApiError("Too many requests. Please wait a moment.", 429)
    rl_add("ret:" + client_ip())
    token = str(b.get("token", ""))
    if not TOKEN_RE.match(token):
        raise ApiError("Invalid request. Please refresh the page.")
    email = s(b.get("email") or (user["email"] if user else ""), "Email", 200, True).lower()
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    raw = b.get("lines")
    if not isinstance(raw, list) or not raw or len(raw) > 60:
        raise ApiError("Choose at least one kit to return.")
    wanted = {}
    for ln in raw:
        if not isinstance(ln, dict):
            raise ApiError("Invalid request.")
        lid = n(ln.get("line_id"), "Kit", 1, 10**12)
        wanted[lid] = wanted.get(lid, 0) + n(ln.get("qty"), "Quantity", 1, 10_000)
    who = user["name"] if user and user["email"] == email else email
    alerts, done = [], []
    try:
        with tx(d):
            d.execute("INSERT INTO returns(token,email,created_at) VALUES(?,?,?)", (token, email, utcnow()))
            for lid in sorted(wanted):
                row = do_return(d, lid, wanted[lid], who, user["id"] if user and user["email"] == email else None,
                                "Returned by borrower", alerts, email=email)
                done.append({"line_id": lid, "qty": wanted[lid], "now_in_stock": row["quantity"],
                             "label": label_of(row)})
    except IntegrityError:
        return jsonify(ok=True, duplicate=True, returned=[])  # same tap sent twice: already recorded
    dispatch_alerts(alerts)
    return jsonify(ok=True, duplicate=False, returned=done)


@app.get("/api/admin/loans")
@owner_required
def admin_loans():
    return jsonify(loans=loans_for(get_db()))


@app.post("/api/admin/loans/<int:line_id>/return")
@owner_required
def admin_return(line_id):
    d = get_db()
    u = current_user()
    b = body()
    qty = n(b.get("qty"), "Quantity", 1, 10_000)
    reason = s(b.get("reason"), "A reason", 300, True)
    alerts = []
    with tx(d):
        do_return(d, line_id, qty, u["name"], u["id"], reason, alerts)
    dispatch_alerts(alerts)
    return jsonify(ok=True)


# --------------------------------------------------------------------------- owner: audit, dashboard


def movement_query(d, limit):
    where, params = [], []
    item = request.args.get("item_id")
    if item:
        where.append("m.item_id=?")
        params.append(n(item, "Item", 1, 10**12))
    if request.args.get("kind"):
        where.append("m.kind=?")
        params.append(request.args["kind"])
    frm, to = _date_arg("from"), _date_arg("to")
    if frm:
        where.append("m.created_at >= ?")
        params.append(frm)
    if to:
        where.append("m.created_at <= ?")
        params.append(to)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        where.append("(lower(m.reason) LIKE ? OR lower(m.actor_name) LIKE ? OR lower(i.exam) LIKE ?)")
        params += [f"%{q}%"] * 3
    rows = d.execute(
        "SELECT m.id, m.item_id, m.delta, m.prev_qty, m.new_qty, m.kind, m.reason, m.checkout_id, m.actor_name, m.created_at,"
        " i.exam, i.edition, i.material_type, i.form_name FROM movements m JOIN items i ON i.id=m.item_id"
        f" WHERE {' AND '.join(where) or '1=1'} ORDER BY m.id DESC LIMIT ?",
        (*params, limit),
    ).all()
    for r in rows:
        r["label"] = label_of(r)
    return rows


@app.get("/api/admin/movements")
@owner_required
def admin_movements():
    return jsonify(movements=movement_query(get_db(), n(request.args.get("limit", 300), "Limit", 1, 2000)))


@app.get("/api/admin/dashboard")
@owner_required
def admin_dashboard():
    d = get_db()
    set_setting(d, "detected_url", request.url_root.rstrip("/"))
    items = [item_json(r, True) for r in d.execute(
        f"SELECT {ITEM_COLS},"
        " (SELECT status FROM alerts a WHERE a.item_id=items.id ORDER BY a.id DESC LIMIT 1) AS last_alert_status,"
        " (SELECT created_at FROM alerts a WHERE a.item_id=items.id ORDER BY a.id DESC LIMIT 1) AS last_alert_at"
        " FROM items WHERE archived=0"
    ).all()]
    counts = {"in": 0, "low": 0, "out": 0}
    for i in items:
        counts[i["status"]] += 1
    needs = sorted((i for i in items if i["status"] != "in"), key=lambda i: (i["quantity"] - i["low_threshold"], i["exam"]))
    recips = parse_recipients(get_setting(d, "alert_recipients"))
    total_items = d.execute("SELECT COUNT(*) AS c FROM items").one()["c"]
    return jsonify(
        counts=counts,
        kits_out=loans_for(d),
        total=len(items),
        needs_reordering=needs,
        checklist={
            "owner": True,
            "inventory": total_items > 0,
            "recipients": bool(recips),
            "email_service": mailer.configured(),
            "qr": get_setting(d, "qr_seen") == "1",
        },
    )


# --------------------------------------------------------------------------- owner: settings, alerts, QR, team


@app.get("/api/admin/settings")
@owner_required
def admin_get_settings():
    d = get_db()
    alerts = d.execute(
        "SELECT id, label, quantity, threshold, trigger, status, recipients, error, created_at FROM alerts ORDER BY id DESC LIMIT 25"
    ).all()
    return jsonify(
        alert_recipients=parse_recipients(get_setting(d, "alert_recipients")),
        allow_guest=get_setting(d, "allow_guest", "1") == "1",
        public_url=get_setting(d, "public_url"),
        effective_url=base_url(d),
        email=mailer.describe(),
        alerts=alerts,
    )


@app.put("/api/admin/settings")
@owner_required
def admin_put_settings():
    d = get_db()
    b = body()
    if "alert_recipients" in b:
        raw = b["alert_recipients"]
        recips = parse_recipients(", ".join(raw) if isinstance(raw, list) else str(raw))
        bad = [r for r in recips if not EMAIL_RE.match(r)]
        if bad:
            raise ApiError("These don't look like email addresses: " + ", ".join(bad[:5]))
        if len(recips) > 20:
            raise ApiError("Please use 20 or fewer alert recipients.")
        set_setting(d, "alert_recipients", ", ".join(recips))
    if "allow_guest" in b:
        set_setting(d, "allow_guest", "1" if b["allow_guest"] else "0")
    if "public_url" in b:
        url = s(b["public_url"], "Public URL", 200).rstrip("/")
        if url and not re.match(r"^https?://[^\s/]+", url):
            raise ApiError("The public URL must start with https:// (or http://).")
        set_setting(d, "public_url", url)
    return admin_get_settings()


@app.post("/api/admin/test-email")
@owner_required
def admin_test_email():
    d = get_db()
    recips = parse_recipients(get_setting(d, "alert_recipients"))
    if not recips:
        raise ApiError("Add at least one alert email address first.")
    if not mailer.configured():
        raise ApiError("The server has no email service configured yet — see the README (Step 4).")
    ok, err = mailer.send(recips, "Test alert from Exam Inventory",
                          "This is a test. If you can read this, low-stock alerts will reach you.")
    if not ok:
        raise ApiError("The email service rejected the message: " + err, 502)
    return jsonify(ok=True, sent_to=recips)


@app.post("/api/admin/resend-low-stock")
@owner_required
def admin_resend_low():
    d = get_db()
    rows = d.execute(
        "SELECT id, exam, edition, material_type, form_name, quantity, low_threshold FROM items"
        " WHERE archived=0 AND quantity <= low_threshold ORDER BY quantity"
    ).all()
    if not rows:
        raise ApiError("Nothing is low on stock right now.")
    ids = []
    with tx(d):
        for r in rows:
            ids.append(d.execute(
                "INSERT INTO alerts(item_id,label,quantity,threshold,trigger,status,created_at) VALUES(?,?,?,?,?,'pending',?) RETURNING id",
                (r["id"], label_of(r), r["quantity"], r["low_threshold"], "manual summary", utcnow()),
            ).one()["id"])
    send_alerts_job(ids)
    st = d.execute("SELECT status, error FROM alerts WHERE id=?", (ids[0],)).one()
    if st["status"] != "sent":
        raise ApiError(st["error"] or "Could not send.", 502)
    return jsonify(ok=True, count=len(ids))


@app.post("/api/admin/sample-data")
@owner_required
def admin_sample():
    d = get_db()
    u = current_user()
    S = [
        ("WISC-V", "5th Edition", "record_form", "Record Form 1", 40, 10, "Cabinet A, shelf 1"),
        ("WISC-V", "5th Edition", "response_booklet", "Response Booklet 1", 25, 10, "Cabinet A, shelf 1"),
        ("WISC-V", "5th Edition", "response_booklet", "Response Booklet 2", 25, 10, "Cabinet A, shelf 1"),
        ("WIAT-4", "4th Edition", "record_form", "Record Form", 30, 8, "Cabinet A, shelf 2"),
        ("WIAT-4", "4th Edition", "response_booklet", "Response Booklet", 12, 8, "Cabinet A, shelf 2"),
        ("WJ IV Tests of Achievement", "Form A", "record_form", "Test Record", 35, 10, "Cabinet B"),
        ("BASC-3", "3rd Edition", "record_form", "Teacher Rating Scales – Child", 6, 10, "Cabinet B"),
        ("KTEA-3", "Form A", "record_form", "Record Form A", 20, 6, "Cabinet C"),
        ("KTEA-3", "Form A", "response_booklet", "Student Response Booklet 1", 0, 5, "Cabinet C"),
    ]
    added = 0
    with tx(d):
        for exam, ed, mt, fn, q, thr, loc in S:
            it = dict(exam=exam, edition=ed, material_type=mt, form_name=fn, quantity=q, low_threshold=thr,
                      location=loc, notes="Sample item — safe to archive")
            if not dup_item(d, it):
                insert_item(d, it, u)
                added += 1
    return jsonify(ok=True, added=added)


@app.get("/api/admin/qr.<fmt>")
@owner_required
def admin_qr(fmt):
    if fmt not in ("svg", "png"):
        raise ApiError("Not found.", 404)
    d = get_db()
    url = base_url(d) + "/"
    try:
        matrix = qr.make(url)
    except ValueError as e:
        raise ApiError(str(e), 400)
    data = qr.to_svg(matrix) if fmt == "svg" else qr.to_png(matrix)
    if request.args.get("download"):
        set_setting(d, "qr_seen", "1")
    resp = Response(data, mimetype="image/svg+xml" if fmt == "svg" else "image/png")
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = f"attachment; filename=exam-materials-checkout-qr.{fmt}"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/admin/users")
@owner_required
def admin_users():
    rows = get_db().execute("SELECT id,email,name,role,active,created_at FROM users ORDER BY role, lower(name)").all()
    for r in rows:
        r["active"] = bool(r["active"])
    return jsonify(users=rows)


@app.post("/api/admin/users")
@owner_required
def admin_create_user():
    d = get_db()
    b = body()
    name = s(b.get("name"), "Name", 100, True)
    email = s(b.get("email"), "Email", 200, True).lower()
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    role = b.get("role", "user")
    if role not in ("owner", "user"):
        raise ApiError("Role must be owner or user.")
    pw = str(b.get("password", ""))
    check_password_rules(pw, 10 if role == "owner" else 8)
    try:
        d.execute("INSERT INTO users(email,name,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)",
                  (email, name, generate_password_hash(pw), role, utcnow()))
    except IntegrityError:
        raise ApiError("That email already has an account.", 409)
    return jsonify(ok=True), 201


@app.post("/api/admin/users/<int:uid>/active")
@owner_required
def admin_user_active(uid):
    d = get_db()
    u = current_user()
    flag = 1 if body().get("active") else 0
    if uid == u["id"] and not flag:
        raise ApiError("You can't deactivate your own account.")
    d.execute("UPDATE users SET active=? WHERE id=?", (flag, uid))
    return jsonify(ok=True)


# --------------------------------------------------------------------------- owner: CSV export


def csv_safe(v):
    """Stop spreadsheet formula injection from user-supplied text (names, emails)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def csv_response(filename, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(x) for x in r])
    resp = Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


@app.get("/api/admin/export/inventory.csv")
@owner_required
def export_inventory():
    rows = get_db().execute(
        f"SELECT {ITEM_COLS} FROM items ORDER BY lower(exam), lower(edition), material_type, lower(form_name)"
    ).all()
    return csv_response(
        "inventory.csv",
        ["Exam", "Edition/Version", "Material type", "Form/Booklet name", "Quantity", "Low-stock level", "Status",
         "Storage location", "Notes", "Archived"],
        [[r["exam"], r["edition"], TYPES[r["material_type"]], r["form_name"], r["quantity"], r["low_threshold"],
          {"in": "In Stock", "low": "Low Stock", "out": "Out of Stock"}[status_of(r["quantity"], r["low_threshold"])],
          r["location"], r["notes"], "yes" if r["archived"] else "no"] for r in rows],
    )


@app.get("/api/admin/export/transactions.csv")
@owner_required
def export_transactions():
    cos = load_checkouts(get_db(), 100000)
    rows = []
    for c in cos:
        for l in c["lines"]:
            rows.append([c["id"], c["created_at"], c["name"], c["email"], l["exam"], l["edition"], l["type_name"],
                         l["form_name"], l["qty"], l["original_qty"], c["status"],
                         "yes" if c["edited"] else "no", c["reversed_at"] or "", c["reverse_reason"] or "",
                         l["returned_qty"] if l["material_type"] == RETURNABLE else ""])
    return csv_response(
        "checkout-history.csv",
        ["Checkout #", "Date/time (UTC)", "Name", "Email", "Exam", "Edition/Version", "Material type", "Form/Booklet",
         "Quantity", "Originally taken", "Status", "Corrected", "Reversed at (UTC)", "Reversal reason",
         "Returned (test kits)"],
        rows,
    )


@app.get("/api/admin/export/audit.csv")
@owner_required
def export_audit():
    rows = movement_query(get_db(), 1_000_000)
    return csv_response(
        "stock-audit-log.csv",
        ["Date/time (UTC)", "Exam", "Edition/Version", "Material type", "Form/Booklet", "Change", "Previous qty",
         "New qty", "Type", "Reason", "Checkout #", "Person"],
        [[r["created_at"], r["exam"], r["edition"], TYPES[r["material_type"]], r["form_name"], r["delta"],
          r["prev_qty"], r["new_qty"], r["kind"], r["reason"], r["checkout_id"] or "", r["actor_name"]] for r in rows],
    )


# --------------------------------------------------------------------------- pages


def _page(name):
    resp = send_from_directory(STATIC_DIR, name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def page_shop():
    return _page("shop.html")


@app.get("/admin")
def page_admin():
    return _page("admin.html")


@app.get("/setup")
def page_setup():
    return _page("setup.html")


@app.get("/static/<path:p>")
def static_files(p):
    if not p.lower().endswith(WEB_EXTS):  # never serve source code or data files
        return Response("Not found", 404)
    resp = send_from_directory(STATIC_DIR, p)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/healthz")
def healthz():
    get_db().execute("SELECT 1 AS ok")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.config["SESSION_COOKIE_SECURE"] = False  # local development over http
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True)
