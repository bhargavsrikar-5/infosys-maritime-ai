"""Authentication + user administration for FreightQuote AI (Milestone 1 login, rebuilt as FastAPI routes).

* users live in their own SQLite file (auth_users.db) so they never mix with the freight data
* passwords and security answers are bcrypt hashes; JWTs are signed with JWT_SECRET (Colab Secret)
* every request re-reads the user row: a locked / deleted user or a changed role takes effect immediately
* the same error text is returned for an unknown account and a wrong password
"""
import os, re, time, string, secrets, sqlite3, datetime, threading, html
from contextlib import contextmanager
import bcrypt, jwt
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
import config, rbac

AUTH_DB = os.path.join(config.DATA_DIR, "auth_users.db")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "8"))
OTP_MINUTES = 5
MAX_ATTEMPTS = 5                 # wrong passwords before the account locks
BASE_LOCK_SECONDS, MAX_LOCK_SECONDS, STALE_SECONDS = 60, 3600, 86400
SECURITY_QUESTIONS = ["What was the name of your first school?", "What was the name of your first pet?", "What is your mother's maiden name?",
                      "In which city were you born?", "What is the name of your favourite teacher?"]
GENERIC_LOGIN = "Invalid username/email or password."
LOCKED_MSG = "This account is locked after too many wrong attempts. Reset your password or ask an administrator to unlock it."


def _jwt_secret():
    v = (os.environ.get("JWT_SECRET") or "").strip()
    if v: return v
    p = os.path.join(config.DATA_DIR, ".jwt_secret")             # no Colab Secret: a random key kept beside the data (never in git)
    try:
        if os.path.exists(p): return open(p).read().strip()
        v = secrets.token_urlsafe(48); fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, v.encode()); os.close(fd); return v
    except Exception: return secrets.token_urlsafe(48)
JWT_SECRET = _jwt_secret()
JWT_FROM_SECRET = bool((os.environ.get("JWT_SECRET") or "").strip())

_lock = threading.RLock()


@contextmanager
def _db():
    with _lock:
        c = sqlite3.connect(AUTH_DB, timeout=30); c.row_factory = sqlite3.Row; c.execute("PRAGMA journal_mode=WAL;"); c.execute("PRAGMA busy_timeout=30000;")
        try: yield c; c.commit()
        finally: c.close()


def now_s(): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ hashing
def hash_text(t): return bcrypt.hashpw(t.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")
def check_text(t, h):
    try: return bool(h) and bcrypt.checkpw(t.encode("utf-8")[:72], h.encode("utf-8"))
    except ValueError: return False
_DUMMY = hash_text("dummy-password-for-constant-time")


# ------------------------------------------------------------------ validation (Milestone 1 rules)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
COMMON = {"password@123", "welcome@123", "admin@123", "qwerty@123", "password123!", "admin@12345"}

def is_valid_email(e):
    """2+ letters before @, 2+ letters between @ and the last dot, 2+ letters after it."""
    if not EMAIL_RE.match(e or ""): return False
    local, domain = e.split("@"); host, tld = domain.rsplit(".", 1); letters = lambda s: sum(ch.isalpha() for ch in s)
    return letters(local) >= 2 and letters(host) >= 2 and len(tld) >= 2

def is_strong_password(pw, *names):
    pw = pw or ""
    if len(pw) < 8: return False, "Password must be at least 8 characters long."
    if len(pw) > 64: return False, "Password must be at most 64 characters long."
    if " " in pw: return False, "Password must not contain spaces."
    if not re.search(r"[A-Z]", pw): return False, "Add at least one uppercase letter."
    if not re.search(r"[a-z]", pw): return False, "Add at least one lowercase letter."
    if not re.search(r"\d", pw): return False, "Add at least one number."
    if not re.search(r"[^A-Za-z0-9]", pw): return False, "Add at least one special character (e.g. @ # $ !)."
    for n in names:
        part = (n or "").split("@")[0].lower()
        if len(part) >= 3 and part in pw.lower(): return False, "Password must not contain your username or email name."
    if pw.lower() in COMMON: return False, "This password is too common."
    return True, "Strong"

def gen_password():
    while True:
        p = "".join(secrets.choice(string.ascii_letters + string.digits + "@#$%!") for _ in range(12))
        if is_strong_password(p)[0]: return p


# ------------------------------------------------------------------ database
def init_db():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'Customer', locked INTEGER NOT NULL DEFAULT 0, lock_reason TEXT DEFAULT '', failed_attempts INTEGER NOT NULL DEFAULT 0,
            customer_id TEXT, security_question TEXT, security_answer_hash TEXT, token_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_login TEXT, prev_login TEXT, avatar TEXT DEFAULT '', last_seen REAL DEFAULT 0, must_change INTEGER NOT NULL DEFAULT 0, pw_kind TEXT DEFAULT 'permanent');
        CREATE TABLE IF NOT EXISTS lockouts(key TEXT PRIMARY KEY, fails INTEGER NOT NULL, locked_until REAL NOT NULL DEFAULT 0, last_fail REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS otp_codes(email TEXT PRIMARY KEY COLLATE NOCASE, otp_hash TEXT NOT NULL, expires REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS revoked_tokens(jti TEXT PRIMARY KEY, exp REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS activity_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS ix_activity_id ON activity_log(id);""")
        have = {r[1] for r in c.execute("PRAGMA table_info(users)")}          # older databases: add the new columns once
        for col, ddl in [("avatar", "TEXT DEFAULT ''"), ("last_seen", "REAL DEFAULT 0"), ("must_change", "INTEGER NOT NULL DEFAULT 0"), ("pw_kind", "TEXT DEFAULT 'permanent'")]:
            if col not in have: c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")


def log_activity(actor, action, target="", detail=""):
    try:
        with _db() as c: c.execute("INSERT INTO activity_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)", (now_s(), str(actor or "system"), action, str(target or ""), str(detail or "")[:400]))
    except Exception: pass


def public_user(u):
    """Never contains the password hash, the security answer or the token version."""
    if not u: return None
    return {"id": u["id"], "email": u["email"], "username": u["username"], "role": u["role"], "locked": bool(u["locked"]), "customer_id": u.get("customer_id"),
            "created_at": u["created_at"], "last_login": u.get("last_login"), "prev_login": u.get("prev_login"),
            "avatar": u.get("avatar") or "", "must_change": bool(u.get("must_change")), "pw_kind": u.get("pw_kind") or "permanent"}


def _row(r): return dict(r) if r else None
def find_user(ident):
    ident = (ident or "").strip()
    if not ident: return None
    with _db() as c: return _row(c.execute("SELECT * FROM users WHERE username=? OR email=?", (ident, ident)).fetchone())
def get_user(uid):
    with _db() as c: return _row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
def count_admins():
    with _db() as c: return c.execute("SELECT COUNT(*) FROM users WHERE role='Admin'").fetchone()[0]


# ------------------------------------------------------------------ presence: Active (signed in and recently seen) / Inactive
ACTIVE_SECONDS = 180             # a signed-in user is "Active" while the app has been seen within the last 3 minutes (the page pings every minute)
_seen = {}
def touch(uid):
    """Record that this user is using the app. Throttled to one write per 20 s per user."""
    t = time.time()
    if t - _seen.get(uid, 0) < 20: return
    _seen[uid] = t
    try:
        with _db() as c: c.execute("UPDATE users SET last_seen=? WHERE id=?", (t, uid))
    except Exception: pass
def clear_seen(uid):
    _seen.pop(uid, None)
    with _db() as c: c.execute("UPDATE users SET last_seen=0 WHERE id=?", (uid,))
def is_active(u): return (not u.get("locked")) and (time.time() - float(u.get("last_seen") or 0)) <= ACTIVE_SECONDS
def _ts(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if epoch else None


# ------------------------------------------------------------------ profile picture (avatar)
AVATAR_PRESETS = 12              # preset:0 ... preset:11 are drawn by the front end; an uploaded photo is a small data URL
AVATAR_MAX_CHARS = 90000
def _image_ok(raw):
    return raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n" or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
def set_avatar(u, value):
    import base64, binascii
    v = (value or "").strip()
    if v:
        if v.startswith("preset:"):
            if not (v[7:].isdigit() and int(v[7:]) < AVATAR_PRESETS): return _bad("Unknown avatar.")
        else:
            m = re.match(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$", v)
            if not m: return _bad("Use a JPG, PNG or WebP picture.")
            if len(v) > AVATAR_MAX_CHARS: return _bad("That picture is too large. Choose a smaller one.")
            try: raw = base64.b64decode(m.group(2), validate=True)
            except (binascii.Error, ValueError): return _bad("That picture could not be read.")
            if not _image_ok(raw): return _bad("That file is not a valid picture.")
    with _db() as c: c.execute("UPDATE users SET avatar=? WHERE id=?", (v, u["id"]))
    log_activity(u["username"], "avatar_changed", u["email"], "removed" if not v else ("preset" if v.startswith("preset:") else "photo"))
    return {"ok": True, "msg": "Profile picture updated." if v else "Profile picture removed.", "user": public_user(get_user(u["id"]))}


# ------------------------------------------------------------------ customer accounts (a Customer user is tied to a customers row)
def _freight_conn():
    c = sqlite3.connect(config.DB_PATH, timeout=30); c.execute("PRAGMA busy_timeout=30000;"); return c

def ensure_customer(customer_id=None, name="New customer"):
    """Return a valid customer_id. Creates a customers row when needed (self-signup / admin-created Customer)."""
    try:
        with _freight_conn() as c:
            if customer_id:
                if c.execute("SELECT 1 FROM customers WHERE customer_id=?", (customer_id,)).fetchone(): return customer_id
            else:
                n = max([int(m.group(1)) for (x,) in c.execute("SELECT customer_id FROM customers") if (m := re.match(r"CUST-(\d+)$", x or ""))] or [0]) + 1; customer_id = f"CUST-{n:03d}"
            c.execute("INSERT OR IGNORE INTO customers(customer_id,name,industry,priority_tier,credit_risk) VALUES(?,?,?,?,?)", (customer_id, name, "Self-registered", "Silver", 0.2))
        return customer_id
    except Exception: return customer_id


# ------------------------------------------------------------------ tokens
def make_token(u):
    n = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode({"sub": str(u["id"]), "tv": u["token_version"], "jti": secrets.token_hex(8), "type": "session", "iat": n, "exp": n + datetime.timedelta(hours=SESSION_HOURS)}, JWT_SECRET, algorithm="HS256")

def user_from_token(token):
    """-> (user, error). Re-reads the row on every call so lock / delete / role change / password change act immediately."""
    if not token: return None, "Please sign in."
    try: p = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError: return None, "Your session expired. Please sign in again."
    except jwt.InvalidTokenError: return None, "Invalid session. Please sign in again."
    if p.get("type") != "session": return None, "Invalid session."
    with _db() as c:
        if c.execute("SELECT 1 FROM revoked_tokens WHERE jti=?", (p.get("jti"),)).fetchone(): return None, "You have signed out."
        r = c.execute("SELECT * FROM users WHERE id=?", (p.get("sub"),)).fetchone()
    if not r: return None, "This account no longer exists."
    u = dict(r)
    if u["locked"]: return None, "Your account is locked. Contact an administrator."
    if u["token_version"] != p.get("tv"): return None, "Your password changed. Please sign in again."
    touch(u["id"]); return u, None

def bearer(request):
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else ""

def user_from_request(request):
    return user_from_token(bearer(request))


# ------------------------------------------------------------------ timed lockouts (OTP, security answer, unknown accounts)
def _fails(row, now): return 0 if (not row or now - row["last_fail"] > STALE_SECONDS) else row["fails"]
def failed_attempt(key):
    key, now = key.lower(), time.time()
    with _db() as c:
        row = c.execute("SELECT fails,locked_until,last_fail FROM lockouts WHERE key=?", (key,)).fetchone(); fails = _fails(row, now) + 1; lock = 0; until = 0
        if fails >= MAX_ATTEMPTS: lock = min(BASE_LOCK_SECONDS * (fails - MAX_ATTEMPTS + 1), MAX_LOCK_SECONDS); until = now + lock
        c.execute("INSERT OR REPLACE INTO lockouts(key,fails,locked_until,last_fail) VALUES(?,?,?,?)", (key, fails, until, now))
    return fails, lock
def attempts_left(key):
    with _db() as c: row = c.execute("SELECT fails,locked_until,last_fail FROM lockouts WHERE key=?", (key.lower(),)).fetchone()
    return max(0, MAX_ATTEMPTS - _fails(row, time.time()))
def lockout_remaining(key):
    with _db() as c: row = c.execute("SELECT locked_until FROM lockouts WHERE key=?", (key.lower(),)).fetchone()
    return 0 if not row else max(0, int(row["locked_until"] - time.time()) + (1 if row["locked_until"] > time.time() else 0))
def clear_attempts(key):
    with _db() as c: c.execute("DELETE FROM lockouts WHERE key=?", (key.lower(),))


# ------------------------------------------------------------------ e-mail (OTP)
def email_ready():
    import mailer; return mailer.configured()

def send_otp_email(to, otp):
    import mailer
    text = f"Your FreightQuote AI verification code is {otp}. It expires in {OTP_MINUTES} minutes. If you did not request it, ignore this e-mail."
    page = (f'<html><body style="font-family:Arial,sans-serif;background:#FBF4EC;padding:20px"><div style="max-width:480px;margin:0 auto;background:#fff;border:1.5px solid #E3D2C2;border-radius:16px;padding:30px;text-align:center">'
            f'<div style="font-size:20px;font-weight:bold;color:#26304D;margin-bottom:12px">Password reset verification</div>'
            f'<div style="color:#4A5578;font-size:15px;line-height:1.5">Use this code to reset the password for <b>{html.escape(to)}</b>:</div>'
            f'<div style="display:inline-block;margin:18px 0;padding:14px 22px;background:#E4EAF5;color:#26304D;font-size:28px;font-weight:bold;letter-spacing:6px;border:1.5px solid #54668E;border-radius:12px">{otp}</div>'
            f'<div style="color:#4A5578;font-size:14px">This code expires in <b>{OTP_MINUTES} minutes</b>.</div>'
            f'<div style="color:#667090;font-size:12px;margin-top:22px;border-top:1px solid #E3D2C2;padding-top:14px">If you did not request this, you can safely ignore this e-mail.</div></div></body></html>')
    return mailer.send(to, "Your FreightQuote AI verification code", text, html=page)


# ------------------------------------------------------------------ core operations
def _bad(msg, code=400, **kw): return JSONResponse({"ok": False, "msg": msg, **kw}, status_code=code)

def signup(d):
    username, email = (d.get("username") or "").strip(), (d.get("email") or "").strip().lower(); pw, cpw = d.get("password") or "", d.get("confirm") or ""
    q, ans = d.get("question") or "", (d.get("answer") or "").strip()
    if not (username and email and pw and cpw and ans): return _bad("All fields are mandatory.")
    if not USERNAME_RE.match(username): return _bad("Username must be 3-30 characters: letters, numbers or underscore only.")
    if not is_valid_email(email): return _bad("Enter a valid email (at least 2 letters before @, between @ and the dot, and after the dot).")
    ok, m = is_strong_password(pw, username, email)
    if not ok: return _bad(m)
    if pw != cpw: return _bad("Password and Confirm Password do not match.")
    if q not in SECURITY_QUESTIONS: return _bad("Choose one of the security questions.")
    if len(ans) < 2: return _bad("Security answer is too short.")
    cust = ensure_customer(None, username)
    try:
        with _db() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone(): return _bad("Username already taken. Please choose another one.")
            if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): return _bad("This email is already registered.")
            c.execute("INSERT INTO users(email,username,password_hash,role,customer_id,security_question,security_answer_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (email, username, hash_text(pw), rbac.PUBLIC_SIGNUP_ROLE, cust, q, hash_text(ans.lower()), now_s()))
    except sqlite3.IntegrityError: return _bad("Username or email already exists.")
    log_activity(username, "signup", email, f"role {rbac.PUBLIC_SIGNUP_ROLE}")
    return {"ok": True, "msg": "Account created. You can sign in now."}

def login(d):
    ident, pw = (d.get("identifier") or "").strip(), d.get("password") or ""
    if not ident or not pw: return _bad("Please fill in all fields.")
    key = "login:" + ident.lower(); u = find_user(ident)
    if u is None:                                                      # unknown account: same text, same timing, same lock rule as a real one
        check_text(pw, _DUMMY)
        if lockout_remaining(key): return _bad(LOCKED_MSG, 423)
        fails, lock = failed_attempt(key)
        return _bad(LOCKED_MSG, 423) if lock else _bad(f"{GENERIC_LOGIN} Attempts left: {attempts_left(key)}.", 401)
    if u["locked"]:
        check_text(pw, _DUMMY); log_activity(u["username"], "login_blocked", u["email"], "account locked"); return _bad(LOCKED_MSG, 423)
    if not check_text(pw, u["password_hash"]):
        n = u["failed_attempts"] + 1
        with _db() as c:
            if n >= MAX_ATTEMPTS: c.execute("UPDATE users SET locked=1, lock_reason='attempts', failed_attempts=0 WHERE id=?", (u["id"],))
            else: c.execute("UPDATE users SET failed_attempts=? WHERE id=?", (n, u["id"]))
        if n >= MAX_ATTEMPTS: log_activity(u["username"], "account_locked", u["email"], f"{MAX_ATTEMPTS} wrong passwords"); return _bad(LOCKED_MSG, 423)
        log_activity(u["username"], "login_failed", u["email"], f"attempt {n}")
        return _bad(f"{GENERIC_LOGIN} Attempts left: {MAX_ATTEMPTS - n}.", 401)
    with _db() as c: c.execute("UPDATE users SET failed_attempts=0, prev_login=last_login, last_login=?, last_seen=? WHERE id=?", (now_s(), time.time(), u["id"])); _seen[u["id"]] = time.time()
    u = get_user(u["id"]); log_activity(u["username"], "login", u["email"], u["role"])
    return {"ok": True, "token": make_token(u), "user": public_user(u)}

def logout(token):
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=["HS256"]);
        with _db() as c: c.execute("INSERT OR IGNORE INTO revoked_tokens(jti,exp) VALUES(?,?)", (p.get("jti"), float(p.get("exp", 0)))); c.execute("DELETE FROM revoked_tokens WHERE exp<?", (time.time(),))
        clear_seen(int(p.get("sub")))                                   # signing out makes the user Inactive straight away
    except Exception: pass

def question_for(username):
    u = find_user(username)
    if u and u.get("security_question"): return u["security_question"]
    import zlib; return SECURITY_QUESTIONS[zlib.crc32((username or "").lower().encode()) % len(SECURITY_QUESTIONS)]   # unknown user: a stable fake question

def forgot(d):
    """Send an e-mail OTP. The answer never reveals whether the address is registered."""
    email = (d.get("email") or "").strip().lower()
    if not email: return _bad("Please enter your email.")
    if not is_valid_email(email): return _bad("Enter a valid email address.")
    if not email_ready(): return _bad("E-mail sending is not configured. Add EMAIL_ADDRESS and EMAIL_PASSWORD in Colab Secrets, or use the security question.")
    key = "otp:" + email; left = lockout_remaining(key)
    if left: return _bad(f"Too many attempts. Try again in {left}s.", 429, wait=left)
    u = find_user(email)
    if u and u["email"].lower() == email:
        otp = f"{secrets.randbelow(900000) + 100000}"
        with _db() as c: c.execute("INSERT OR REPLACE INTO otp_codes(email,otp_hash,expires,tries) VALUES(?,?,?,0)", (email, hash_text(otp), time.time() + OTP_MINUTES * 60))
        ok, m = send_otp_email(email, otp); log_activity(u["username"], "otp_sent" if ok else "otp_failed", email, "" if ok else m[:120])
    return {"ok": True, "msg": f"If this email is registered, a 6-digit code was sent. It is valid for {OTP_MINUTES} minutes."}

def _apply_new_password(u, pw):
    with _db() as c:
        c.execute("UPDATE users SET password_hash=?, must_change=0, pw_kind='permanent', token_version=token_version+1, failed_attempts=0, locked=CASE WHEN lock_reason='attempts' THEN 0 ELSE locked END, lock_reason=CASE WHEN lock_reason='attempts' THEN '' ELSE lock_reason END WHERE id=?", (hash_text(pw), u["id"]))
    clear_attempts("login:" + u["email"].lower()); clear_attempts("login:" + u["username"].lower())

def reset(d):
    method = d.get("method") or "otp"; pw, cpw = d.get("password") or "", d.get("confirm") or ""
    if not pw or not cpw: return _bad("All fields are mandatory.")
    if method == "question":
        uname = (d.get("username") or "").strip(); ans = (d.get("answer") or "").strip().lower(); key = "sq:" + uname.lower(); left = lockout_remaining(key)
        if not uname or not ans: return _bad("All fields are mandatory.")
        if left: return _bad(f"Too many wrong answers. Try again in {left}s.", 429, wait=left)
        u = find_user(uname)
        if not (u and u["username"].lower() == uname.lower() and u.get("security_answer_hash") and check_text(ans, u["security_answer_hash"])):
            if u is None: check_text(ans, _DUMMY)
            fails, lock = failed_attempt(key); return _bad(f"Incorrect security answer. Attempts left: {attempts_left(key)}." if not lock else f"Too many wrong answers. Locked for {lock // 60} min.", 400)
        ok, m = is_strong_password(pw, u["username"], u["email"])
        if not ok: return _bad(m)
        if pw != cpw: return _bad("Password and Confirm Password do not match.")
        _apply_new_password(u, pw); clear_attempts(key); log_activity(u["username"], "password_reset", u["email"], "security question")
        return {"ok": True, "msg": "Password reset successful. You can sign in now."}
    email = (d.get("email") or "").strip().lower(); code = (d.get("code") or "").strip(); key = "otp:" + email; left = lockout_remaining(key)
    if not (email and code): return _bad("All fields are mandatory.")
    if not (code.isdigit() and len(code) == 6): return _bad("Please enter the 6-digit code.")
    if left: return _bad(f"Too many wrong codes. Try again in {left}s.", 429, wait=left)
    with _db() as c: r = c.execute("SELECT * FROM otp_codes WHERE email=?", (email,)).fetchone()
    u = find_user(email)
    if not (r and u and u["email"].lower() == email) or time.time() > r["expires"] or r["tries"] >= MAX_ATTEMPTS:
        failed_attempt(key); return _bad("This code is invalid or expired. Request a new one.")
    if not check_text(code, r["otp_hash"]):
        with _db() as c: c.execute("UPDATE otp_codes SET tries=tries+1 WHERE email=?", (email,))
        failed_attempt(key); return _bad("Incorrect code.")
    ok, m = is_strong_password(pw, u["username"], email)
    if not ok: return _bad(m)
    if pw != cpw: return _bad("Password and Confirm Password do not match.")
    _apply_new_password(u, pw); clear_attempts(key)
    with _db() as c: c.execute("DELETE FROM otp_codes WHERE email=?", (email,))
    log_activity(u["username"], "password_reset", email, "e-mail OTP")
    return {"ok": True, "msg": "Password reset successful. You can sign in now."}


# ------------------------------------------------------------------ profile
def change_username(u, new):
    new = (new or "").strip()
    if not USERNAME_RE.match(new): return _bad("Username must be 3-30 characters: letters, numbers or underscore only.")
    if new.lower() == u["username"].lower() and new == u["username"]: return _bad("That is already your username.")
    with _db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=? AND id<>?", (new, u["id"])).fetchone(): return _bad("Username already taken. Please choose another one.")
        c.execute("UPDATE users SET username=? WHERE id=?", (new, u["id"]))
    log_activity(new, "username_changed", u["email"], f"{u['username']} -> {new}")
    return {"ok": True, "msg": "Username updated.", "user": public_user(get_user(u["id"]))}

def change_password(u, cur, new, confirm):
    if not (cur and new and confirm): return _bad("All fields are mandatory.")
    if not check_text(cur, u["password_hash"]): return _bad("Current password is incorrect.", 400)
    ok, m = is_strong_password(new, u["username"], u["email"])
    if not ok: return _bad(m)
    if new != confirm: return _bad("Password and Confirm Password do not match.")
    if check_text(new, u["password_hash"]): return _bad("The new password must be different from the current one.")
    with _db() as c: c.execute("UPDATE users SET password_hash=?, must_change=0, pw_kind='permanent', token_version=token_version+1 WHERE id=?", (hash_text(new), u["id"]))
    log_activity(u["username"], "password_changed", u["email"], "replaced the temporary password" if u.get("must_change") else "")
    nu = get_user(u["id"])
    return {"ok": True, "msg": "Password changed. Other devices were signed out.", "token": make_token(nu), "user": public_user(nu)}


# ------------------------------------------------------------------ admin operations (callers must have passed need(ctx, "users"))
def list_users():
    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id")]
    return [{**public_user(r), "status": "Locked" if r["locked"] else "Enabled", "lock_reason": r.get("lock_reason") or "",
             "presence": "Active" if is_active(r) else "Inactive", "last_seen_at": _ts(r.get("last_seen"))} for r in rows]

def recent_activity(n=50):
    with _db() as c: return [dict(r) for r in c.execute("SELECT id,ts,actor,action,target,detail FROM activity_log ORDER BY id DESC LIMIT ?", (n,))]

def overview():
    us = list_users(); per = {r: sum(1 for u in us if u["role"] == r) for r in rbac.ROLES}
    return {"ok": True, "kpis": {"total": len(us), "per_role": per, "locked": sum(1 for u in us if u["locked"]), "active": sum(1 for u in us if u["presence"] == "Active"), "inactive": sum(1 for u in us if u["presence"] == "Inactive")},
            "users": us, "roles": rbac.ROLES, "matrix": rbac.permission_rows(), "log": recent_activity(50)}

def admin_add_user(actor, d):
    email, username, pw, role = (d.get("email") or "").strip().lower(), (d.get("username") or "").strip(), d.get("password") or "", d.get("role") or ""
    kind = "permanent" if (d.get("password_type") or "temporary") == "permanent" else "temporary"      # temporary: must be changed at first sign-in; permanent: the user keeps it (and may change it any time)
    if not (email and username and pw and role): return _bad("Email, username, password and role are all required.")
    if role not in rbac.ROLES: return _bad("Unknown role.")
    if not USERNAME_RE.match(username): return _bad("Username must be 3-30 characters: letters, numbers or underscore only.")
    if not is_valid_email(email): return _bad("Enter a valid email (at least 2 letters before @, between @ and the dot, and after the dot).")
    ok, m = is_strong_password(pw, username, email)
    if not ok: return _bad(m)
    cust = None
    if role == "Customer": cust = ensure_customer((d.get("customer_id") or "").strip().upper() or None, username)
    try:
        with _db() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone(): return _bad("Username already taken.")
            if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): return _bad("This email is already registered.")
            c.execute("INSERT INTO users(email,username,password_hash,role,customer_id,created_at,must_change,pw_kind) VALUES(?,?,?,?,?,?,?,?)", (email, username, hash_text(pw), role, cust, now_s(), 1 if kind == "temporary" else 0, kind))
    except sqlite3.IntegrityError: return _bad("Username or email already exists.")
    log_activity(actor["username"], "user_added", email, f"role {role}, {kind} password")
    return {"ok": True, "msg": f"User {username} created as {role} with a {kind} password." + (" They must set a new password at first sign-in." if kind == "temporary" else " They can change it themselves from their Profile page.")}

def _target(uid):
    t = get_user(uid)
    if not t: raise HTTPException(404, "User not found.")
    return t

def admin_set_role(actor, uid, role):
    t = _target(uid)
    if role not in rbac.ROLES: return _bad("Unknown role.")
    if t["id"] == actor["id"]: return _bad("You cannot change your own role. Ask another administrator.", 403)
    if t["role"] == role: return _bad("The user already has that role.")
    if t["role"] == "Admin" and count_admins() <= 1: return _bad("The last remaining Admin cannot be demoted.", 403)
    cust = t.get("customer_id")
    if role == "Customer" and not cust: cust = ensure_customer(None, t["username"])
    with _db() as c: c.execute("UPDATE users SET role=?, customer_id=? WHERE id=?", (role, cust, t["id"]))
    log_activity(actor["username"], "role_changed", t["email"], f"{t['role']} -> {role}")
    return {"ok": True, "msg": f"{t['username']} is now {role}."}

def admin_set_lock(actor, uid, lock):
    t = _target(uid)
    if t["id"] == actor["id"]: return _bad("You cannot lock or unlock your own account.", 403)
    with _db() as c:
        if lock: c.execute("UPDATE users SET locked=1, lock_reason='admin', token_version=token_version WHERE id=?", (t["id"],))
        else: c.execute("UPDATE users SET locked=0, lock_reason='', failed_attempts=0 WHERE id=?", (t["id"],))
    clear_attempts("login:" + t["email"].lower()); clear_attempts("login:" + t["username"].lower())
    if lock: clear_seen(t["id"])
    log_activity(actor["username"], "user_locked" if lock else "user_unlocked", t["email"])
    return {"ok": True, "msg": f"{t['username']} is now {'locked' if lock else 'unlocked'}."}

def admin_set_password(actor, uid, pw, kind):
    """Admin sets a new password for another user: temporary (must be changed at next sign-in) or permanent (the user may change it later)."""
    t = _target(uid)
    if t["id"] == actor["id"]: return _bad("Change your own password on the Profile page.", 403)
    kind = "permanent" if kind == "permanent" else "temporary"
    ok, m = is_strong_password(pw or "", t["username"], t["email"])
    if not ok: return _bad(m)
    with _db() as c:
        c.execute("UPDATE users SET password_hash=?, must_change=?, pw_kind=?, token_version=token_version+1, failed_attempts=0, locked=CASE WHEN lock_reason='attempts' THEN 0 ELSE locked END, lock_reason=CASE WHEN lock_reason='attempts' THEN '' ELSE lock_reason END WHERE id=?",
                  (hash_text(pw), 1 if kind == "temporary" else 0, kind, t["id"]))
    clear_attempts("login:" + t["email"].lower()); clear_attempts("login:" + t["username"].lower()); clear_seen(t["id"])
    log_activity(actor["username"], "password_set", t["email"], f"{kind} password")
    return {"ok": True, "msg": f"New {kind} password set for {t['username']}. They were signed out." + (" They must change it at next sign-in." if kind == "temporary" else "")}

def admin_set_customer(actor, uid, cid):
    t = _target(uid)
    if t["role"] != "Customer": return _bad("Only Customer accounts have a customer id.")
    cid = ensure_customer((cid or "").strip().upper() or None, t["username"])
    with _db() as c: c.execute("UPDATE users SET customer_id=? WHERE id=?", (cid, t["id"]))
    log_activity(actor["username"], "customer_linked", t["email"], cid)
    return {"ok": True, "msg": f"{t['username']} is linked to {cid}."}

def admin_delete(actor, uid, confirm):
    t = _target(uid)
    if not confirm: return _bad("Deletion must be confirmed.")
    if t["id"] == actor["id"]: return _bad("You cannot delete your own account.", 403)
    if t["role"] == "Admin" and count_admins() <= 1: return _bad("The last remaining Admin cannot be deleted.", 403)
    with _db() as c: c.execute("DELETE FROM users WHERE id=?", (t["id"],)); c.execute("DELETE FROM otp_codes WHERE email=?", (t["email"],))
    log_activity(actor["username"], "user_deleted", t["email"], t["role"])
    return {"ok": True, "msg": f"User {t['username']} deleted."}


# ------------------------------------------------------------------ first-run accounts
def seed_defaults():
    """One Admin on first start (and optional demo accounts for the other roles). Passwords come from Colab Secrets
    (ADMIN_PASSWORD / DEMO_PASSWORD) or are generated and written to initial_credentials.txt beside the data (never to git)."""
    init_db(); made = []
    def mk(username, email, role, pw, cust=None):
        with _db() as c:
            if c.execute("SELECT 1 FROM users WHERE username=? OR email=?", (username, email)).fetchone(): return
            c.execute("INSERT INTO users(email,username,password_hash,role,customer_id,created_at) VALUES(?,?,?,?,?,?)", (email, username, hash_text(pw), role, cust, now_s()))
        made.append((role, username, email, pw))
    if count_admins() == 0:
        ap = os.environ.get("ADMIN_PASSWORD") or gen_password()
        mk("admin", os.environ.get("ADMIN_EMAIL") or "admin@freightquote.ai", "Admin", ap)
        if os.environ.get("ADMIN_PASSWORD"): made[:] = [(r, u, e, "(your ADMIN_PASSWORD secret)") if r == "Admin" else (r, u, e, p) for r, u, e, p in made]
    if os.environ.get("SEED_DEMO_USERS", "1") == "1":
        dp = os.environ.get("DEMO_PASSWORD"); shown = "(your DEMO_PASSWORD secret)" if dp else None
        for username, email, role, cust in [("ops_manager", "ops.manager@freightquote.ai", "Operations Manager", None), ("freight_broker", "broker@freightquote.ai", "Freight Broker", None),
                                            ("auditor", "auditor@freightquote.ai", "Auditor", None), ("customer_demo", "customer@freightquote.ai", "Customer", "CUST-001")]:
            pw = dp or gen_password()
            if cust: cust = ensure_customer(cust, "Corporate Client 001")
            n0 = len(made); mk(username, email, role, pw, cust)
            if shown and len(made) > n0: made[-1] = (role, username, email, shown)
    if made:
        p = os.path.join(config.DATA_DIR, "initial_credentials.txt")
        try:
            with open(p, "a") as f:
                for r, u, e, pw in made: f.write(f"{r} | {u} | {e} | {pw}\n")
            os.chmod(p, 0o600)
        except Exception: pass
    return made
