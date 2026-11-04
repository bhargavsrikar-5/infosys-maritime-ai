"""Web server: FastAPI JSON API + server-sent events + the single-page front end. Models live on the GPU in this process; the simulator and agent cycles run in worker.py."""
import sqlite3, os, sys, io, re, csv, json, time, asyncio, subprocess, threading, importlib, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from config import KIND, DB_PATH
from fastapi import FastAPI, Request, Response, HTTPException, UploadFile, File, Form, Header
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd
import numpy as np
import gpu, qa as QA, analyst as AN, live_engine as LE, entities as EN, blocks as B
import views_core as VC
import auth as AUTH, rbac as RB, copilot_plus as CP, views_platform as VP
from fastapi.responses import FileResponse as _FR
dom = importlib.import_module("domain_franchise" if KIND == "Franchise" else "domain_freight")
V = importlib.import_module("views_franchise" if KIND == "Franchise" else "views_freight")
HERE = os.path.dirname(os.path.abspath(__file__)); STATIC = os.path.join(HERE, "static")
app = FastAPI(title=f"{V.NAME} Platform", docs_url=None, redoc_url=None, openapi_url=None) if os.environ.get("PUBLIC_DOCS") != "1" else FastAPI(title=f"{V.NAME} Platform"); _worker = None; _ready = {"db": False}
import hardening; hardening.install(app, KIND)

@app.middleware("http")
async def sec(request, call_next):
    r = await call_next(request); r.headers["X-Content-Type-Options"] = "nosniff"; r.headers["Referrer-Policy"] = "same-origin"; r.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else r.headers.get("Cache-Control", "public, max-age=300"); return r

def _boot():
    from db import init_db; from seed_data import seed_all
    init_db(); seed_all(); LE.init_live_tables(); AUTH.init_db(); _creds = AUTH.seed_defaults(); _ready["db"] = True
@app.on_event("startup")
def startup():
    global _worker
    _boot()
    if os.environ.get("NO_WORKER") != "1":
        env = {**os.environ, "APP_KIND": KIND}; _worker = subprocess.Popen([sys.executable, os.path.join(HERE, "worker.py")], env=env, cwd=HERE)
@app.on_event("shutdown")
def shutdown():
    if _worker:
        try: _worker.terminate()
        except Exception: pass

# ------------------------------------------------------------------ auth + context
TEMP_PW_OK = ("/api/me", "/api/ping", "/api/profile", "/api/profile/password", "/api/auth/logout")     # all a user with a temporary password may call
def user_of(request: Request):
    """The only place identity enters: reads the Bearer token, re-checks the user row (exists, not locked, token still valid).
    A user whose administrator-set password is still temporary can only open the change-password screen until they choose their own."""
    u, err = AUTH.user_from_request(request)
    if not u: raise HTTPException(401, err or "Please sign in.")
    if u.get("must_change") and request.url.path not in TEMP_PW_OK: raise HTTPException(403, "Your password is temporary. Set a new password first.")
    return u
def ctx(u, params=None): return {"dom": dom, "kind": KIND, "role": u["role"], "email": u["email"], "user": u, "customer_id": u.get("customer_id"), "scope": None, "params": params or {}}
def need(c, cap):
    if not RB.has_cap(c["role"], cap):
        AUTH.log_activity(c["user"]["username"], "access_denied", cap, c["role"])
        raise HTTPException(403, f"Access denied (403): the {c['role']} role does not have the '{cap}' permission.")
def menu_for(u):
    """Pages the role may open (the server also refuses everything else with 403)."""
    return [m for m in RB.nav_for(u["role"]) if m["enabled"]]
def audit(u, event, target="", detail=""):
    LE.audit(u["email"], event, target, detail); AUTH.log_activity(u["username"], event, target, detail)
def ok(msg="Done", **kw): return {"ok": True, "msg": msg, **kw}
def bad(msg): return JSONResponse({"ok": False, "msg": msg}, status_code=400)

@app.get("/api/public")
def public():
    st = LE.get_state("boot", {}) if _ready["db"] else {}
    return {"name": V.NAME, "stage": (st or {}).get("stage", "starting"), "questions": AUTH.SECURITY_QUESTIONS, "email_enabled": AUTH.email_ready(), "jwt_secret": AUTH.JWT_FROM_SECRET, "roles": [{"role": r, "who": RB.ROLE_BLURB[r]} for r in RB.ROLES], "demo_users": ({"Admin": "admin", "Operations Manager": "ops_manager", "Freight Broker": "freight_broker", "Auditor": "auditor", "Customer": "customer_demo"} if os.environ.get("SEED_DEMO_USERS", "1") == "1" else {})}
@app.get("/api/ping")
def ping(request: Request): user_of(request); return {"ok": True}          # heartbeat: keeps the user "Active" while the app is open
@app.get("/api/me")
def me(request: Request):
    u = user_of(request)
    return {"user": AUTH.public_user(u), "kind": KIND, "name": V.NAME, "menu": menu_for(u), "nav": menu_for(u), "caps": sorted(RB.caps(u["role"])), "blurb": RB.ROLE_BLURB.get(u["role"], "")}
# ------------------------------------------------------------------ live pulse
def pulse(full=True):
    if not full: return {"stage": (LE.get_state("boot", {}) or {}).get("stage", "starting")}
    rt = LE.get_state("runtime", {}) or {}; boot = LE.get_state("boot", {}) or {}; d = VC.decision()
    oa = B.Q("SELECT status, severity, COUNT(*) n FROM agent_actions WHERE status IN ('open','proposed') GROUP BY status, severity")
    n = lambda st, sv=None: int(oa[(oa.status == st) & ((oa.severity == sv) if sv else True)].n.sum()) if len(oa) else 0
    return {"stage": boot.get("stage", "starting"), "ticks": rt.get("ticks", 0), "cycles": rt.get("cycles", 0), "last_cycle": rt.get("last_cycle"), "health": d["score"] if d else None, "risk": d["risk"] if d else None, "dec": d["id"] if d else 0,
            "open": n("open"), "critical": n("open", "Critical"), "proposed": 0, "gpu": {"cuda": True, "name": "", "used": 0, "total": 0, "llm": "off", "llm_name": "", "llm_done": 0, "llm_total": 0}, "worker_alive": bool(rt.get("last_cycle"))}
@app.get("/api/pulse")
def pulse_http(request: Request): u = user_of(request); return pulse(RB.has_cap(u["role"], "view"))
@app.get("/api/stream")
async def stream(request: Request):
    u = user_of(request); tok = AUTH.bearer(request); full = RB.has_cap(u["role"], "view")
    async def gen():
        last = None; t0 = time.time()
        while True:
            if await request.is_disconnected(): break
            if (await asyncio.to_thread(AUTH.user_from_token, tok))[0] is None: yield "data: " + json.dumps({"error": "signed_out"}) + "\n\n"; break     # locked / deleted / signed out: stop at once
            try: p = await asyncio.to_thread(pulse, full)
            except Exception as e: p = {"error": str(e)}
            k = json.dumps(p, sort_keys=True, default=str)
            if k != last or time.time() - t0 > 10: last = k; t0 = time.time(); yield f"data: {k}\n\n"
            await asyncio.sleep(1.5)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ------------------------------------------------------------------ pages
_TABS = {}
def _tabs_of(c, f):
    if f not in _TABS: _TABS[f] = f(c, None).get("tabs")
    return _TABS[f]
def _page(c, pid, tab):
    if not RB.can_open_page(c["role"], pid):
        AUTH.log_activity(c["user"]["username"], "access_denied", "page:" + pid, c["role"])
        raise HTTPException(403, f"Access denied (403): the {c['role']} role cannot open the '{pid}' page.")
    if pid == "copilot": return B.page("AI Copilot", "Ask about your data in plain English. Every answer is an exact database query, shown under the answer.", [{"t": "chat", "examples": CP.suggestions(c["role"], "", 5)}], [], live=0)
    if pid == "profile": return VP.profile_page(c)
    if pid == "runagents": need(c, "run"); return VP.run_agents_page(c)
    if pid == "admin": need(c, "users"); return VP.admin_page(c)
    f = V.PAGES.get(pid)
    if not f: raise HTTPException(404, "unknown page")
    o = f(c, tab); _TABS[f] = o.get("tabs"); return o
@app.get("/api/page/{pid}")
def page(pid: str, request: Request, tab: str = "", p: str = ""):
    u = user_of(request); c = ctx(u, json.loads(p) if p else {})
    t = time.time(); out = _page(c, pid, tab or None); out["id"] = pid; out["ms"] = round((time.time() - t) * 1000); return JSONResponse(B.clean(out))
@app.get("/api/detail/{eid:path}")
def detail(eid: str, request: Request):
    u = user_of(request); c = ctx(u); k = EN.kind_of(eid)
    if not RB.can_open_entity(u["role"], k): raise HTTPException(403, f"Access denied (403): the {u['role']} role cannot open {k} details.")
    if u["role"] == "Customer":
        own = k == "quote" and len(B.Q("SELECT 1 FROM freight_quotes WHERE quote_id=? AND customer_id=?", (eid, u.get("customer_id"))))
        if not own: raise HTTPException(403, "Access denied (403): you can only open your own quotes.")
        r = B.Q("SELECT final_price, status, created_at FROM freight_quotes WHERE quote_id=?", (eid,)).iloc[0]
        return JSONResponse(B.clean({"title": eid, "sub": r.status, "blocks": [B.kpis(("Price", f"${r.final_price:,.0f}"), ("Status", r.status), ("Created", r.created_at))]}))
    return JSONResponse(B.clean(V.detail(c, eid)))

# ------------------------------------------------------------------ tickets, cycles, policy
@app.post("/api/run_cycle")
def run_cycle(request: Request):
    u = user_of(request); c = ctx(u); need(c, "run"); t = time.time(); LE.run_cycle(dom, "manual", ""); audit(u, "manual_cycle", "all"); return ok(f"Agents finished in {time.time() - t:.1f}s")
@app.post("/api/run_agent")
async def run_agent(request: Request):
    u = user_of(request); c = ctx(u); need(c, "run"); a = (await request.json()).get("agent")
    if a not in dom.AGENTS: raise HTTPException(404, "unknown agent")
    await asyncio.to_thread(_run_one, a); audit(u, "manual_agent", a); return ok(f"{a} re-ran")
def _run_one(agent):
    cid = time.strftime("%m%d-%H%M%S") + "-page"; t = time.time(); s = {"cycle_id": cid, "trigger": "page", "request": "", "queue": [], "results": {}}
    if agent == "digest":
        for a in ("outlet", "inventory", "staff", "marketing", "audit"): s["results"][a] = dom.AGENTS[a](s)
    res = dom.AGENTS[agent](s); LE._log_run(cid, "page", res, int((time.time() - t) * 1000)); s["results"][agent] = res; LE.act(dom, {"actions": res.actions}, cid, {agent: res})

# ------------------------------------------------------------------ admin users

# ------------------------------------------------------------------ digital twin

# ------------------------------------------------------------------ data feeds

# ------------------------------------------------------------------ Freight actions
@app.post("/api/freight/quote")
async def fq_quote(request: Request):
    u = user_of(request); c = ctx(u); need(c, "quote"); d = await request.json()
    if d["o"] == d["d"]: return bad("Origin and destination must differ.")
    if u["role"] == "Customer": d["customer"] = u.get("customer_id")          # a Customer can only quote for their own account, whatever the request says
    rid = dom.submit_request(d["o"], d["d"], d["cargo"], float(d["weight"]), d["hs"], d["customer"], "English", d.get("docs") or []); await asyncio.to_thread(dom.run_quote, rid); audit(u, "quote_created", str(rid), f"{d['o']}->{d['d']}")
    return ok("Quote generated", blocks=B.clean(V.quote_result(rid, customer=(u["role"] == "Customer"))))
@app.post("/api/freight/storm")
async def fq_storm(request: Request):
    PRESET_STORMS = {"Red Sea": (18.0, 40.0), "Arabian Sea": (16.0, 66.0), "Bay of Bengal": (14.0, 88.0), "South China Sea": (14.0, 114.0), "Mediterranean": (35.0, 18.0)}; u = user_of(request); c = ctx(u); need(c, "storm"); d = await request.json() if request.headers.get("content-length", "0") not in ("0", "2") else {}
    la, lo = PRESET_STORMS.get(d.get("area") or "Red Sea"); dom.inject_storm(la, lo, wind=float(d.get("wind", 24))); audit(u, "inject_storm", d.get("area", "Red Sea")); return ok("Storm injected. The weather agent will propose re-routes on its next cycle.", refresh=True)
@app.post("/api/freight/reroute")
async def fq_reroute(request: Request):
    u = user_of(request); c = ctx(u); need(c, "approve"); j = await request.json(); sid = j["shipment"]; opt = j.get("opt")
    idx = int(str(opt).split(".")[0]) - 1 if opt else None; act = {"type": "reroute", "target": sid}
    if idx is not None: act["opt_label"] = str(opt).split(" · ", 1)[-1]; act["opt_idx"] = idx
    with LE._DB_LOCK, LE.conn() as cn: note = dom.apply(act, cn)
    audit(u, "manual_reroute", sid, note); return ok(note or "no change", refresh=True)
@app.post("/api/freight/attach")
async def fq_attach(request: Request):
    u = user_of(request); c = ctx(u); need(c, "resolve")
    d = await request.json(); dom.attach_doc(d["ref"], d["doc"], u["email"]); audit(u, "doc_attached", d["ref"], d["doc"]); return ok("Attached. Quotes re-run immediately; shipments clear on the next customs cycle.", refresh=True)
@app.post("/api/freight/readdoc")
async def fq_readdoc(request: Request, file: UploadFile = File(...)):
    u = user_of(request); need(ctx(u), "resolve"); data = await file.read(); txt = ""
    try:
        if file.filename.lower().endswith(".pdf"):
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf: txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        else: txt = data.decode("utf-8", errors="ignore")
    except Exception as e: return bad(f"Could not read the file: {e}")
    hs = re.findall(r"HS[- ]?\d{2,8}", txt); q = re.findall(r"QTE-L\d+", txt); wt = re.findall(r"(\d+(?:\.\d+)?)\s*(?:t|tons|kg)\b", txt)
    return ok("Read", blocks=[B.table([{"field": "HS code", "value": ", ".join(dict.fromkeys(hs)) or "-"}, {"field": "Quote reference", "value": ", ".join(dict.fromkeys(q)) or "-"}, {"field": "Weights", "value": ", ".join(wt[:3]) or "-"}], ["field", "value"], title="Fields found", h=170, key="ocr"), B.md("```\n" + txt[:2500] + "\n```")])

# ------------------------------------------------------------------ language, knowledge base

# ------------------------------------------------------------------ Copilot (agents first, then the GPU LLM; NLLB for every language) + executive brief
def _sse(ev, **kw): return f"data: {json.dumps({'ev': ev, **kw}, default=str)}\n\n"
SYSTEM = "You are the operations copilot of an agentic platform. Answer ONLY from the EVIDENCE. Quote numbers exactly as given, never invent any. Be concise: a one-line answer, then up to 5 short bullets with the entity ids. If the evidence does not answer, say so. When you use a numbered source, cite it as [1], [2]."
_BRIEF = {}
def _chat_db():
    c = sqlite3.connect(DB_PATH, timeout=30); c.execute("PRAGMA journal_mode=WAL;"); c.execute("CREATE TABLE IF NOT EXISTS copilot_chats(id TEXT PRIMARY KEY, email TEXT, title TEXT, ts TEXT, data TEXT)"); c.execute("CREATE INDEX IF NOT EXISTS ix_chat_email ON copilot_chats(email, ts)"); return c
@app.get("/api/chats")
async def chats_list(request: Request):
    u = user_of(request)
    with _chat_db() as c: rows = c.execute("SELECT id, title, ts FROM copilot_chats WHERE email=? ORDER BY ts DESC LIMIT 60", (u["email"],)).fetchall()
    return {"ok": True, "chats": [{"id": r[0], "title": r[1], "ts": r[2]} for r in rows]}
@app.get("/api/chats/{cid}")
async def chats_get(cid: str, request: Request):
    u = user_of(request)
    with _chat_db() as c: r = c.execute("SELECT title, data FROM copilot_chats WHERE id=? AND email=?", (cid, u["email"])).fetchone()
    if not r: raise HTTPException(404, "chat not found")
    return {"ok": True, "title": r[0], "msgs": json.loads(r[1])}
@app.post("/api/chats/save")
async def chats_save(request: Request):
    u = user_of(request); d = await request.json(); cid = re.sub(r"[^a-zA-Z0-9_-]", "", str(d.get("id") or ""))[:40] or ("c" + str(int(time.time() * 1000))); msgs = d.get("msgs") or []
    if not isinstance(msgs, list) or not msgs: return bad("nothing to save")
    msgs = [{"role": m.get("role"), "text": str(m.get("text", ""))[:20000], "en": (str(m["en"])[:20000] if m.get("en") else None), "lang": m.get("lang"), "sql": str(m.get("sql") or "")[:1500], "rows": m.get("rows"), "table": m.get("table"), "note": str(m.get("note") or "")[:300], "sug": m.get("sug") if isinstance(m.get("sug"), list) else None, "kind": m.get("kind")} for m in msgs[-80:]]
    title = next((m["text"] for m in msgs if m["role"] == "user"), "Chat")[:70]; now = datetime.datetime.now().isoformat(timespec="seconds")
    with _chat_db() as c:
        o = c.execute("SELECT email FROM copilot_chats WHERE id=?", (cid,)).fetchone()
        if o and o[0] != u["email"]: raise HTTPException(403, "not your chat")
        c.execute("INSERT OR REPLACE INTO copilot_chats VALUES(?,?,?,?,?)", (cid, u["email"], title, now, json.dumps(msgs)))
    return {"ok": True, "id": cid}
@app.delete("/api/chats/{cid}")
async def chats_del(cid: str, request: Request):
    u = user_of(request)
    with _chat_db() as c: c.execute("DELETE FROM copilot_chats WHERE id=? AND email=?", (cid, u["email"]))
    return {"ok": True}
@app.post("/api/weather/refresh")
async def wx_refresh(request: Request):
    u = user_of(request); c = ctx(u); need(c, "run")
    if KIND != "Freight": raise HTTPException(404)
    import weather as WX; n, msg = await asyncio.to_thread(WX.refresh); audit(u, "weather_refresh", "ports", msg)
    return ok(("Real weather updated: " if n else "Could not reach Open-Meteo, simulated values stay: ") + msg, refresh=True)

# ------------------------------------------------------------------ reports, exports, profile, invitations


@app.get("/api/health")
def health(): return {"ok": True, "gpu": gpu.status()}
@app.get("/")
def index(): return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ------------------------------------------------------------------ AI Copilot (SQL only: greetings + exact database answers)
@app.post("/api/copilot")
async def copilot(request: Request):
    u = user_of(request); c = ctx(u); need(c, "copilot"); d = await request.json(); q = str(d.get("q", "")).strip()[:500]
    if not q: return bad("Type a question first.")
    t0 = time.time(); r = await asyncio.to_thread(CP.answer, u, q)
    if r.get("kind") in ("answer", "denied", "unknown"): AUTH.log_activity(u["username"], "copilot_" + r["kind"], r.get("intent") or "", q[:120])
    # Admin and Auditor (read everything) may fall back to the older generic SQL engine when the new intents do not match
    if r.get("kind") == "unknown" and u["role"] in ("Admin", "Auditor"):
        try:
            ex = AN.answer(KIND, q, None, VC.decision(), dom) or QA.answer(KIND, q, None, None)
            if ex and ex.get("answer"):
                df = ex.get("df"); tbl = None if df is None or not len(df) else {"cols": [str(x) for x in df.columns], "rows": [[None if (isinstance(v, float) and v != v) else (v.item() if hasattr(v, "item") else v) for v in row] for row in df.head(25).itertuples(index=False, name=None)], "total": int(len(df))}
                r = {"kind": "answer", "text": re.sub(r"\*\*|`", "", ex["answer"]) if False else ex["answer"], "sql": ex.get("sql", ""), "rows": 0 if df is None else int(len(df)), "table": tbl, "note": "", "intent": "legacy"}
        except Exception as e: print("copilot legacy failed:", repr(e), file=sys.stderr)
    r["ms"] = round((time.time() - t0) * 1000); r.setdefault("table", None); r.setdefault("suggestions", [])
    return JSONResponse(B.clean(r))


# ------------------------------------------------------------------ authentication, profile, administration
async def _json(request):
    try: d = await request.json()
    except Exception: d = {}
    return d if isinstance(d, dict) else {}
@app.post("/api/auth/signup")
async def auth_signup(request: Request): return AUTH.signup(await _json(request))
@app.post("/api/auth/login")
async def auth_login(request: Request): return AUTH.login(await _json(request))
@app.post("/api/auth/question")
async def auth_question(request: Request): return {"ok": True, "question": AUTH.question_for(str((await _json(request)).get("username") or ""))}
@app.post("/api/auth/forgot")
async def auth_forgot(request: Request): return AUTH.forgot(await _json(request))
@app.post("/api/auth/reset")
async def auth_reset(request: Request): return AUTH.reset(await _json(request))
@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    u, _ = AUTH.user_from_request(request); AUTH.logout(AUTH.bearer(request))
    if u: AUTH.log_activity(u["username"], "logout", u["email"])
    return {"ok": True}
@app.get("/api/profile")
async def profile_get(request: Request):
    u = user_of(request); return {"ok": True, "user": AUTH.public_user(u), "blurb": RB.ROLE_BLURB.get(u["role"], ""), "pages": [m["label"] for m in menu_for(u) if m["id"] not in ("profile",)], "caps": sorted(RB.caps(u["role"]))}
@app.post("/api/profile/username")
async def profile_username(request: Request): u = user_of(request); return AUTH.change_username(u, (await _json(request)).get("username"))
@app.post("/api/profile/password")
async def profile_password(request: Request):
    u = user_of(request); d = await _json(request); return AUTH.change_password(u, d.get("current"), d.get("password"), d.get("confirm"))
@app.post("/api/profile/avatar")
async def profile_avatar(request: Request): u = user_of(request); return AUTH.set_avatar(u, (await _json(request)).get("avatar"))
@app.post("/api/admin/users/{uid}/password")
async def admin_password(uid: int, request: Request): u = user_of(request); need(ctx(u), "users"); d = await _json(request); return AUTH.admin_set_password(u, uid, d.get("password"), d.get("kind"))
@app.get("/api/admin/overview")
async def admin_overview(request: Request): u = user_of(request); need(ctx(u), "users"); return AUTH.overview()
@app.post("/api/admin/users")
async def admin_add(request: Request): u = user_of(request); need(ctx(u), "users"); return AUTH.admin_add_user(u, await _json(request))
@app.post("/api/admin/users/{uid}/role")
async def admin_role(uid: int, request: Request): u = user_of(request); need(ctx(u), "users"); return AUTH.admin_set_role(u, uid, (await _json(request)).get("role"))
@app.post("/api/admin/users/{uid}/lock")
async def admin_lock(uid: int, request: Request): u = user_of(request); need(ctx(u), "users"); return AUTH.admin_set_lock(u, uid, bool((await _json(request)).get("locked")))
@app.post("/api/admin/users/{uid}/customer")
async def admin_customer(uid: int, request: Request): u = user_of(request); need(ctx(u), "users"); return AUTH.admin_set_customer(u, uid, (await _json(request)).get("customer_id"))
@app.delete("/api/admin/users/{uid}")
async def admin_delete(uid: int, request: Request, confirm: str = ""): u = user_of(request); need(ctx(u), "users"); return AUTH.admin_delete(u, uid, confirm == "yes")
