"""FreightQuote AI Copilot, Milestone 2 edition: greetings + exact answers from SQL only (no LLM, no RAG).

* every answer is produced by a fixed, whitelisted, parameterised SELECT; the SQL and the row count are returned with the answer
* spelling mistakes and different wordings are handled (vocabulary correction + several phrasings per intent)
* RBAC: every intent belongs to a data domain; a role only reads the domains in rbac.ROLE_DATA (a Customer only reads own quotes / shipments)
* out-of-scope questions get a polite reply with three valid example questions for the user's role
"""
import re, math, json, difflib
import pandas as pd
from blocks import Q
import rbac

# ------------------------------------------------------------------ vocabulary used for spelling correction
VOCAB = set("""cheapest cheaper cheap costliest expensive carrier carriers port ports congested congestion busiest dwell shipment shipments shipping delayed delay delays customs hold held
quote quotes quotation margin margins weather storm storms wind waves wave temperature reliable reliability punctual rated rating risk risky highest lowest least most worst best top bottom
average total count many number status pending approved rejected draft submitted accepted transit delivered anchorage vessels vessel customers customer platinum gold silver tier credit
duty tariff documents document required restricted pharmaceuticals machinery electronics textiles chemicals tickets ticket critical warning open heaviest weight region regions asia europe africa
americas oceania mediterranean middle east suez panama malacca hormuz bosphorus cape red sea latest recent below above under over between revenue price value fastest slowest longest shortest arriving
departing destination origin route routes hapag maersk cosco evergreen yang ming""".split())
PHRASE_FIX = {"on time": "on-time", "ontime": "on-time", "kustoms": "customs", "carrer": "carrier"}
COMMON_WORDS = set("""the a an of to in on at is are was were be by for from with and or not do does did have has had which what who whom how why when where me my our your show tell give list
find get see can could would should please any all some there their them this that these those it its as into out than then also about more less much each every per vs versus i we you
shipments are ports""".split())

REGIONS = {"asia": "Asia", "europe": "Europe", "africa": "Africa", "americas": "Americas", "america": "Americas", "oceania": "Oceania", "middle east": "Middle East", "mediterranean": "Europe/Mediterranean"}
CHOKE = {"red sea": ("Red Sea", 18.0, 40.0), "suez": ("Suez Canal", 30.5, 32.3), "panama": ("Panama Canal", 9.1, -79.7), "malacca": ("Strait of Malacca", 2.5, 101.5),
         "hormuz": ("Strait of Hormuz", 26.5, 56.5), "bosphorus": ("Bosphorus", 41.1, 29.0), "cape of good hope": ("Cape of Good Hope", -34.4, 18.5),
         "bay of bengal": ("Bay of Bengal", 14.0, 88.0), "arabian sea": ("Arabian Sea", 16.0, 66.0), "south china sea": ("South China Sea", 14.0, 114.0), "mediterranean": ("Mediterranean", 35.0, 18.0)}
STATUS_WORDS = {"customs hold": "Customs Hold", "on hold": "Customs Hold", "in transit": "In Transit", "delivered": "Delivered", "anchorage": "Anchorage Pending",
                "port congestion delay": "Port Congestion Delay"}
QSTATUS = {"pending approval": "Pending Approval", "pending": "Pending Approval", "approved": "Approved", "rejected": "Rejected", "on hold": "On Hold", "draft": "Draft", "submitted": "Submitted", "accepted": "Accepted"}


def _norm(t):
    t = (t or "").lower().replace("%", " % "); t = re.sub(r"(?<=\d),(?=\d)", "", t); t = re.sub(r"[^a-z0-9%.\- ]", " ", t); t = re.sub(r"\s+", " ", t).strip()
    for a, b in PHRASE_FIX.items(): t = t.replace(a, b)
    return t


def _dynamic_vocab():
    """Port, carrier and place names are added to the vocabulary so 'rotterdm' or 'maerks' are corrected too."""
    try:
        v = set()
        for n in Q("SELECT port_name FROM ports").port_name: v.update(w for w in re.sub(r"[^a-z ]", " ", n.lower()).split() if len(w) >= 5)
        for n in Q("SELECT name FROM carriers LIMIT 40").name: v.update(w for w in re.sub(r"[^a-z ]", " ", n.lower()).split() if len(w) >= 5)
        return v
    except Exception: return set()


def correct(text):
    """-> (corrected text, [(wrong, right)]). Only words that are not ordinary English and are close to a domain word are changed."""
    vocab = VOCAB | _dynamic_vocab(); out, fixes = [], []
    for w in _norm(text).split():
        if len(w) < 4 or w in vocab or w in COMMON_WORDS or re.search(r"\d", w) or w.startswith("-") or w.rstrip("s") in vocab or w + "s" in vocab: out.append(w); continue
        m = difflib.get_close_matches(w, vocab, n=1, cutoff=0.8)
        if m and m[0] != w: fixes.append((w, m[0])); out.append(m[0])
        else: out.append(w)
    return " ".join(out), fixes


# ------------------------------------------------------------------ helpers
def _has(q, *phrases):
    return any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?:s|es)?(?![a-z0-9])", q) for p in phrases)

def _num(q, default=None):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|m)?\b", q.replace(",", ""))
    if not m: return default
    v = float(m.group(1)); return v * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1)

def _topn(q, default=5):
    m = re.search(r"\b(?:top|first|bottom|best|worst|highest|lowest|last)\s+(\d{1,2})\b", q) or re.search(r"\b(\d{1,2})\s+(?:ports|carriers|shipments|quotes|customers|vessels)\b", q)
    return max(1, min(int(m.group(1)), 25)) if m else default

def _pct_threshold(q, default):
    m = re.search(r"(?:below|under|less than|lower than|above|over|more than|greater than|at least|at most|<|>)\s*(\d+(?:\.\d+)?)\s*(?:%|percent)?", q)
    return float(m.group(1)) if m else default

def _alias(name):
    base = re.sub(r"\(.*?\)", " ", name.lower()); base = re.sub(r"[^a-z0-9 ]", " ", base).strip(); out = {base}
    core = [w for w in base.split() if w not in ("port", "terminal", "line", "lines", "shipping", "container", "international")]
    if core: out.add(" ".join(core))
    for inner in re.findall(r"\((.*?)\)", name): out.add(re.sub(r"[^a-z0-9 ]", " ", inner.lower()).strip())
    return {a for a in out if len(a) >= 3}

_CACHE = {}
def _lists():
    import time
    if _CACHE.get("t", 0) + 120 > time.time(): return _CACHE["ports"], _CACHE["carriers"]
    ports = Q("SELECT port_name FROM ports").port_name.tolist(); carriers = Q("SELECT name FROM carriers").name.tolist()
    first = {}
    for p in ports:
        core = [w for w in _alias(p) if " " not in w]
        for w in core: first.setdefault(w, []).append(p)
    pa = {}
    for p in ports:
        for a in _alias(p): pa.setdefault(a, p)
    for w, ps in first.items():
        if len(ps) == 1 and len(w) >= 5: pa.setdefault(w, ps[0])
    ca = {}
    for c in carriers:
        for a in _alias(c): ca.setdefault(a, c)
    _CACHE.update(t=time.time(), ports=pa, carriers=ca); return pa, ca

def find_ports(q):
    pa, _ = _lists(); hits = []
    for a in sorted(pa, key=len, reverse=True):
        m = re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", q)
        if m and not any(m.start() < h[1] and h[0] < m.end() for h in hits): hits.append((m.start(), m.end(), pa[a]))
    hits.sort(); seen = []
    for h in hits:
        if h[2] not in seen: seen.append(h[2])
    return seen

def find_carrier(q):
    _, ca = _lists()
    for a in sorted(ca, key=len, reverse=True):
        if len(a) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", q): return ca[a]
    return None

def find_region(q):
    for k in sorted(REGIONS, key=len, reverse=True):
        if _has(q, k): return REGIONS[k]
    return None

def hav(la1, lo1, la2, lo2):
    r = 6371.0; p1, p2 = math.radians(la1), math.radians(la2); dp = p2 - p1; dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2; return 2 * r * math.asin(math.sqrt(a))

def show_sql(sql, params):
    """The query with its bound values written in, for display only (execution always uses bound parameters)."""
    it = iter(params)
    def sub(_):
        try: v = next(it)
        except StopIteration: return "?"
        return str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"
    return re.sub(r"\?", sub, " ".join(sql.split()))

def run(sql, params=()):
    s = sql.strip()
    if not s.upper().startswith("SELECT") or ";" in s: raise ValueError("only a single SELECT is allowed")
    df = Q(s, tuple(params)); return df, show_sql(s, params)

def fmt_df(df, rename=None):
    d = df.copy()
    for c in d.columns:
        if d[c].dtype.kind == "f": d[c] = d[c].round(2)
    return d.rename(columns=rename or {})

class Ctx:
    def __init__(self, user, q, orig, fixes):
        self.user, self.q, self.orig, self.fixes = user, q, orig, fixes
        self.role = (user or {}).get("role"); self.cid = (user or {}).get("customer_id"); self.own = self.role == "Customer"
        self.ports = find_ports(q); self.carrier = find_carrier(q); self.region = find_region(q); self.n = _topn(q)
    def qscope(self, alias=""):
        a = alias + "." if alias else ""
        return (f" AND {a}customer_id = ?", [self.cid]) if self.own else ("", [])
    def sscope(self, alias=""):
        a = alias + "." if alias else ""
        return (f" AND {a}shipment_id IN (SELECT shipment_id FROM freight_quotes WHERE customer_id = ?)", [self.cid]) if self.own else ("", [])

def A(text, df=None, sql="", params=(), note=""):
    return {"text": text, "df": df, "sql": sql, "rows": 0 if df is None else int(len(df)), "note": note}


# ------------------------------------------------------------------ intents: ports
def i_port_congestion_top(c):
    least = _has(c.q, "least", "lowest", "smoothest", "quietest", "fastest", "emptiest")
    w, p = ("WHERE region = ?", [c.region]) if c.region else ("", [])
    order = "ASC" if least else "DESC"
    df, s = run(f"SELECT port_name, country, region, ROUND(congestion_index,2) AS congestion_index, avg_dwell_days FROM ports {w} ORDER BY congestion_index {order}, avg_dwell_days {order} LIMIT ?", p + [c.n])
    if not len(df): return None
    top = df.iloc[0]
    return A(f"{'Least' if least else 'Most'} congested port{' in ' + c.region if c.region else ''}: **{top.port_name}** (index {top.congestion_index}, dwell {top.avg_dwell_days} days). Top {len(df)} below.", df, s)

def i_port_congestion_lookup(c):
    if not c.ports: return None
    ph = ",".join("?" * len(c.ports[:5]))
    df, s = run(f"SELECT port_name, country, region, ROUND(congestion_index,2) AS congestion_index, avg_dwell_days FROM ports WHERE port_name IN ({ph}) ORDER BY congestion_index DESC", c.ports[:5])
    if not len(df): return None
    r = df.iloc[0]; lvl = "high" if r.congestion_index >= 4 else "moderate" if r.congestion_index >= 3 else "low"
    return A(f"**{r.port_name}** has a congestion index of **{r.congestion_index}** ({lvl}) and an average dwell time of **{r.avg_dwell_days} days**.", df, s)

def i_port_dwell(c):
    short = _has(c.q, "shortest", "lowest", "least", "fastest", "quickest")
    df, s = run(f"SELECT port_name, country, region, avg_dwell_days, ROUND(congestion_index,2) AS congestion_index FROM ports ORDER BY avg_dwell_days {'ASC' if short else 'DESC'}, congestion_index {'ASC' if short else 'DESC'} LIMIT ?", [c.n])
    r = df.iloc[0]; return A(f"{'Shortest' if short else 'Longest'} average dwell time: **{r.port_name}** with **{r.avg_dwell_days} days**.", df, s)

def i_ports_by_region(c):
    if c.region and _has(c.q, "how many", "number of", "count"):
        df, s = run("SELECT region, COUNT(*) AS ports, ROUND(AVG(congestion_index),2) AS avg_congestion FROM ports WHERE region = ? GROUP BY region", [c.region])
        return A(f"There are **{int(df.ports[0]) if len(df) else 0}** ports in {c.region}.", df, s) if len(df) else None
    df, s = run("SELECT region, COUNT(*) AS ports, ROUND(AVG(congestion_index),2) AS avg_congestion, ROUND(AVG(avg_dwell_days),1) AS avg_dwell_days FROM ports GROUP BY region ORDER BY ports DESC")
    return A(f"The platform monitors **{int(df.ports.sum())}** ports in {len(df)} regions; the busiest region by average congestion is **{df.sort_values('avg_congestion', ascending=False).region.iloc[0]}**.", df, s)

def i_congested_count(c):
    thr = _pct_threshold(c.q, 4.0)
    df, s = run("SELECT COUNT(*) AS congested_ports FROM ports WHERE congestion_index >= ?", [thr])
    return A(f"**{int(df.congested_ports[0])}** ports have a congestion index of {thr:g} or more.", df, s)

# ------------------------------------------------------------------ carriers
def i_carrier_cheapest(c):
    dear = _has(c.q, "costliest", "expensive", "dearest", "priciest", "highest cost")
    order = "DESC" if dear else "ASC"
    if c.ports:
        dest = c.ports[0]
        df, s = run(f"SELECT s.carrier, ROUND(c.avg_cost_index,2) AS cost_index, c.on_time_pct, c.risk_level, COUNT(*) AS shipments_to_port FROM shipments s JOIN carriers c ON c.name = s.carrier WHERE s.dest_port = ? GROUP BY s.carrier ORDER BY c.avg_cost_index {order} LIMIT ?", [dest, c.n])
        if len(df):
            r = df.iloc[0]; return A(f"{'Most expensive' if dear else 'Cheapest'} carrier serving **{dest}**: **{r.carrier}** (cost index {r.cost_index}, {r.on_time_pct}% on time). Cost index is relative: lower means cheaper.", df, s)
        o, so = run(f"SELECT name AS carrier, ROUND(avg_cost_index,2) AS cost_index, on_time_pct, risk_level FROM carriers ORDER BY avg_cost_index {order} LIMIT ?", [c.n])
        r = o.iloc[0]; return A(f"No shipment to **{dest}** is on record, so here is the overall ranking: **{r.carrier}** has the {'highest' if dear else 'lowest'} cost index ({r.cost_index}).", o, so)
    df, s = run(f"SELECT name AS carrier, ROUND(avg_cost_index,2) AS cost_index, on_time_pct, risk_level FROM carriers ORDER BY avg_cost_index {order}, on_time_pct DESC LIMIT ?", [c.n])
    r = df.iloc[0]; return A(f"{'Most expensive' if dear else 'Cheapest'} carrier overall: **{r.carrier}** (cost index {r.cost_index}). Lower index = cheaper.", df, s)

def i_carrier_reliable(c):
    worst = _has(c.q, "least", "worst", "unreliable", "lowest", "late", "slowest")
    df, s = run(f"SELECT name AS carrier, on_time_pct, rating, risk_level FROM carriers ORDER BY on_time_pct {'ASC' if worst else 'DESC'}, rating DESC LIMIT ?", [c.n])
    r = df.iloc[0]; return A(f"{'Least' if worst else 'Most'} reliable carrier: **{r.carrier}** with **{r.on_time_pct}%** on-time delivery.", df, s)

def i_carrier_risk(c):
    lvl = "High" if _has(c.q, "high") or not _has(c.q, "medium", "low") else ("Medium" if _has(c.q, "medium") else "Low")
    df, s = run("SELECT name AS carrier, rating, on_time_pct, risk_level FROM carriers WHERE risk_level = ? ORDER BY on_time_pct ASC LIMIT 25", [lvl])
    tot = Q("SELECT COUNT(*) n FROM carriers WHERE risk_level = ?", (lvl,)).n[0]
    return A(f"**{int(tot)}** carriers are rated **{lvl}** risk" + (f"; showing the {len(df)} with the lowest on-time rate." if len(df) < tot else "."), df, s)

def i_carrier_rating(c):
    low = _has(c.q, "lowest", "worst", "poorest")
    df, s = run(f"SELECT name AS carrier, rating, on_time_pct, risk_level FROM carriers ORDER BY rating {'ASC' if low else 'DESC'}, on_time_pct DESC LIMIT ?", [c.n])
    r = df.iloc[0]; return A(f"{'Lowest' if low else 'Highest'} rated carrier: **{r.carrier}** ({r.rating} / 5).", df, s)

def i_carrier_lookup(c):
    if not c.carrier: return None
    df, s = run("SELECT c.name AS carrier, c.rating, c.on_time_pct, c.avg_cost_index AS cost_index, c.risk_level, COUNT(sh.shipment_id) AS shipments, SUM(CASE WHEN sh.status LIKE '%Delay%' OR sh.status = 'Customs Hold' THEN 1 ELSE 0 END) AS problem_shipments FROM carriers c LEFT JOIN shipments sh ON sh.carrier = c.name WHERE c.name = ? GROUP BY c.name", [c.carrier])
    if not len(df): return None
    r = df.iloc[0]; return A(f"**{r.carrier}**: rating {r.rating}/5, {r.on_time_pct}% on time, cost index {r.cost_index}, {r.risk_level} risk. It carries **{int(r.shipments)}** shipments, **{int(r.problem_shipments or 0)}** of them delayed or on customs hold.", df, s)

# ------------------------------------------------------------------ shipments
def i_customs_hold(c):
    sc, sp = c.sscope()
    df, s = run(f"SELECT dest_port, COUNT(*) AS shipments_on_hold FROM shipments WHERE status = 'Customs Hold'{sc} GROUP BY dest_port ORDER BY shipments_on_hold DESC LIMIT 10", sp)
    tot, st = run(f"SELECT COUNT(*) AS n FROM shipments WHERE status = 'Customs Hold'{sc}", sp)
    return A(f"**{int(tot.n[0])}** shipments are on customs hold" + (f"; most at **{df.dest_port.iloc[0]}** ({int(df.shipments_on_hold.iloc[0])})." if len(df) else "."), df, st + "  -- plus the breakdown: " + s)

def i_shipments_delayed(c):
    sc, sp = c.sscope()
    df, s = run(f"SELECT status, COUNT(*) AS shipments FROM shipments WHERE (status LIKE '%Delay%' OR status = 'Anchorage Pending'){sc} GROUP BY status ORDER BY shipments DESC", sp)
    n = int(df.shipments.sum()) if len(df) else 0
    return A(f"**{n}** shipments are delayed or waiting at anchorage.", df, s)

def i_shipments_by_status(c):
    sc, sp = c.sscope(); st = next((v for k, v in STATUS_WORDS.items() if _has(c.q, k)), None)
    df, s = run(f"SELECT status, COUNT(*) AS shipments FROM shipments WHERE 1=1{sc} GROUP BY status ORDER BY shipments DESC", sp)
    if not len(df): return A("You have no shipments on record yet.", df, s)
    if st and st in set(df.status): n = int(df[df.status == st].shipments.iloc[0]); return A(f"**{n}** shipments are **{st}** (out of {int(df.shipments.sum())}).", df, s)
    return A(f"There are **{int(df.shipments.sum())}** shipments; the most common status is **{df.status.iloc[0]}** ({int(df.shipments.iloc[0])}).", df, s)

def i_shipments_risk(c):
    sc, sp = c.sscope()
    df, s = run(f"SELECT shipment_id, origin_port, dest_port, carrier, status, ROUND(predicted_delay_risk,2) AS delay_risk FROM shipments WHERE 1=1{sc} ORDER BY predicted_delay_risk DESC LIMIT ?", sp + [c.n])
    if not len(df): return None
    r = df.iloc[0]; return A(f"Highest predicted delay risk: **{r.shipment_id}** ({r.origin_port} to {r.dest_port}, risk {r.delay_risk}).", df, s)

def i_shipments_to_port(c):
    if not c.ports: return None
    dep = _has(c.q, "from", "leaving", "departing", "origin", "out of"); col = "origin_port" if dep else "dest_port"; sc, sp = c.sscope()
    df, s = run(f"SELECT shipment_id, origin_port, dest_port, carrier, status, eta FROM shipments WHERE {col} = ?{sc} ORDER BY eta LIMIT 15", [c.ports[0]] + sp)
    tot = Q(f"SELECT COUNT(*) n FROM shipments WHERE {col} = ?{sc}", tuple([c.ports[0]] + sp)).n[0]
    return A(f"**{int(tot)}** shipments {'leave' if dep else 'are heading to'} **{c.ports[0]}**" + (f"; the next {len(df)} by ETA are below." if len(df) else "."), df, s)

def i_shipments_heaviest(c):
    sc, sp = c.sscope()
    light = _has(c.q, "lightest", "smallest")
    df, s = run(f"SELECT shipment_id, origin_port, dest_port, carrier, ROUND(weight_kg/1000.0,1) AS weight_tonnes, status FROM shipments WHERE 1=1{sc} ORDER BY weight_kg {'ASC' if light else 'DESC'} LIMIT ?", sp + [c.n])
    if not len(df): return None
    r = df.iloc[0]; return A(f"{'Lightest' if light else 'Heaviest'} shipment: **{r.shipment_id}** at **{r.weight_tonnes} t** ({r.origin_port} to {r.dest_port}).", df, s)

def i_vessels_at_sea(c):
    sc, sp = c.sscope("s")
    df, s = run(f"SELECT COUNT(*) AS vessels, ROUND(AVG(v.progress)*100,0) AS avg_progress_pct, ROUND(AVG(v.speed_kn),1) AS avg_speed_kn FROM vessels v JOIN shipments s ON s.shipment_id = v.shipment_id WHERE 1=1{sc}", sp)
    return A(f"**{int(df.vessels[0])}** vessels are being tracked at sea, on average {int(df.avg_progress_pct[0] or 0)}% of the way, at {df.avg_speed_kn[0]} knots.", df, s)

def i_shipment_lookup(c):
    ids = re.findall(r"shp-?\d+", c.orig.lower())
    if not ids: return None
    sid = ids[0].upper().replace("SHP", "SHP-") if "-" not in ids[0] else ids[0].upper()
    sc, sp = c.sscope("s")
    df, s = run(f"SELECT s.shipment_id, s.origin_port, s.dest_port, s.carrier, s.status, s.eta, ROUND(s.weight_kg/1000.0,1) AS weight_tonnes, s.hs_code, ROUND(s.predicted_delay_risk,2) AS delay_risk, ROUND(v.progress*100,0) AS progress_pct FROM shipments s LEFT JOIN vessels v ON v.shipment_id = s.shipment_id WHERE s.shipment_id = ?{sc}", [sid] + sp)
    if not len(df): return A(f"I could not find shipment **{sid}**" + (" among your shipments." if c.own else "."), df, s)
    r = df.iloc[0]; return A(f"**{r.shipment_id}** ({r.origin_port} to {r.dest_port}) is **{r.status}** with {r.carrier}, ETA {r.eta}, delay risk {r.delay_risk}.", df, s)

# ------------------------------------------------------------------ quotes
def i_quotes_low_margin(c):
    thr = _pct_threshold(c.q, 10.0)
    df, s = run("SELECT quote_id, customer_id, ROUND(final_price,0) AS price, margin_pct, status FROM freight_quotes WHERE margin_pct < ? ORDER BY margin_pct ASC LIMIT 25", [thr])
    n = int(Q("SELECT COUNT(*) n FROM freight_quotes WHERE margin_pct < ?", (thr,)).n[0])
    return A(f"**{n}** quote{'s' if n != 1 else ''} {'have' if n != 1 else 'has'} a margin below **{thr:g}%**" + (f"; the lowest is **{df.quote_id.iloc[0]}** at {df.margin_pct.iloc[0]}%." if len(df) else "."), df, s)

def i_quotes_high_margin(c):
    thr = _pct_threshold(c.q, 20.0)
    df, s = run("SELECT quote_id, customer_id, ROUND(final_price,0) AS price, margin_pct, status FROM freight_quotes WHERE margin_pct > ? ORDER BY margin_pct DESC LIMIT 25", [thr])
    n = int(Q("SELECT COUNT(*) n FROM freight_quotes WHERE margin_pct > ?", (thr,)).n[0]); return A(f"**{n}** quotes have a margin above **{thr:g}%**.", df, s)

def i_quotes_high_value(c):
    m = re.search(r"(?:above|over|more than|greater than|exceed\w*|>)\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m)?", c.q.replace(",", ""))
    thr = float(m.group(1)) * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1) if m else 50000.0; sc, sp = c.qscope()
    df, s = run(f"SELECT quote_id, customer_id, ROUND(final_price,0) AS price, status, created_at FROM freight_quotes WHERE final_price > ?{sc} ORDER BY final_price DESC LIMIT 25", [thr] + sp)
    n = int(Q(f"SELECT COUNT(*) n FROM freight_quotes WHERE final_price > ?{sc}", tuple([thr] + sp)).n[0]); return A(f"**{n}** quotes are priced above **${thr:,.0f}**.", df, s)

def i_quotes_by_status(c):
    sc, sp = c.qscope(); st = next((v for k, v in sorted(QSTATUS.items(), key=lambda kv: -len(kv[0])) if _has(c.q, k)), None)
    df, s = run(f"SELECT status, COUNT(*) AS quotes, ROUND(SUM(final_price),0) AS total_value FROM freight_quotes WHERE 1=1{sc} GROUP BY status ORDER BY quotes DESC", sp)
    if not len(df): return A("There are no quotes yet.", df, s)
    if st: return A(f"**{int(df[df.status == st].quotes.iloc[0]) if st in set(df.status) else 0}** quotes are **{st}** (of {int(df.quotes.sum())} in total).", df, s)
    return A(f"There are **{int(df.quotes.sum())}** quotes in total; **{df.status.iloc[0]}** is the most common status ({int(df.quotes.iloc[0])}).", df, s)

def i_quotes_avg(c):
    df, s = run("SELECT COUNT(*) AS quotes, ROUND(AVG(margin_pct),2) AS avg_margin_pct, ROUND(MIN(margin_pct),1) AS min_margin_pct, ROUND(MAX(margin_pct),1) AS max_margin_pct, ROUND(AVG(final_price),0) AS avg_price FROM freight_quotes")
    r = df.iloc[0]; return A(f"Across **{int(r.quotes)}** quotes the average margin is **{r.avg_margin_pct}%** (range {r.min_margin_pct}% to {r.max_margin_pct}%) and the average price is **${r.avg_price:,.0f}**.", df, s)

def i_quotes_latest(c):
    sc, sp = c.qscope()
    df, s = run(f"SELECT quote_id, customer_id, ROUND(final_price,0) AS price, status, created_at FROM freight_quotes WHERE 1=1{sc} ORDER BY created_at DESC, quote_id DESC LIMIT ?", sp + [c.n])
    if not len(df): return A("There are no quotes yet.", df, s)
    r = df.iloc[0]; return A(f"The latest quote is **{r.quote_id}** (${r.price:,.0f}, {r.status}). Showing the {len(df)} most recent.", df, s)

def i_quote_lookup(c):
    ids = re.findall(r"qte-?\d+", c.orig.lower())
    if not ids: return None
    qid = ids[0].upper() if "-" in ids[0] else ids[0].upper().replace("QTE", "QTE-"); sc, sp = c.qscope()
    cols = "quote_id, customer_id, shipment_id, carrier, ROUND(final_price,0) AS price, status, created_at" + ("" if c.own else ", margin_pct, ROUND(base_cost,0) AS base_cost")
    df, s = run(f"SELECT {cols} FROM freight_quotes WHERE quote_id = ?{sc}", [qid] + sp)
    if not len(df): return A(f"I could not find quote **{qid}**" + (" on your account." if c.own else "."), df, s)
    r = df.iloc[0]; return A(f"**{r.quote_id}** is **{r.status}**, priced at **${r.price:,.0f}**" + ("." if c.own else f" with a margin of {r.margin_pct}%."), df, s)

def i_top_customers(c):
    df, s = run("SELECT q.customer_id, cu.name, cu.priority_tier, COUNT(*) AS quotes, ROUND(SUM(q.final_price),0) AS total_value FROM freight_quotes q LEFT JOIN customers cu ON cu.customer_id = q.customer_id GROUP BY q.customer_id ORDER BY total_value DESC LIMIT ?", [c.n])
    if not len(df): return None
    r = df.iloc[0]; return A(f"Top customer by quoted value: **{r.customer_id}** ({r['name']}) with **${r.total_value:,.0f}** across {int(r.quotes)} quotes.", df, s)

def i_customer_credit(c):
    df, s = run("SELECT customer_id, name, priority_tier, credit_risk FROM customers ORDER BY credit_risk DESC LIMIT ?", [c.n]); r = df.iloc[0]
    return A(f"Highest credit risk: **{r.customer_id}** ({r['name']}) at {r.credit_risk:.2f}.", df, s)

def i_customer_tiers(c):
    tier = next((t for t in ("platinum", "gold", "silver") if _has(c.q, t)), None)
    if tier:
        df, s = run("SELECT customer_id, name, industry, priority_tier, credit_risk FROM customers WHERE priority_tier = ? ORDER BY credit_risk DESC LIMIT 25", [tier.title()])
        return A(f"There are **{len(df)}** {tier.title()} customers.", df, s)
    if _has(c.q, "credit", "risky", "risk"):
        df, s = run("SELECT customer_id, name, priority_tier, credit_risk FROM customers ORDER BY credit_risk DESC LIMIT ?", [c.n]); r = df.iloc[0]
        return A(f"Highest credit risk: **{r.customer_id}** ({r['name']}) at {r.credit_risk:.2f}.", df, s)
    df, s = run("SELECT priority_tier, COUNT(*) AS customers, ROUND(AVG(credit_risk),2) AS avg_credit_risk FROM customers GROUP BY priority_tier ORDER BY customers DESC")
    return A(f"There are **{int(df.customers.sum())}** customers across {len(df)} priority tiers.", df, s)

# ------------------------------------------------------------------ weather
def _storm_rows():
    df, s = run("SELECT v AS storms_json FROM live_state WHERE k = 'storms'")
    try: items = json.loads(df.storms_json[0]) if len(df) else []
    except Exception: items = []
    return items, s

def i_storms(c):
    items, s = _storm_rows(); place = next((v for k, v in sorted(CHOKE.items(), key=lambda kv: -len(kv[0])) if _has(c.q, k)), None)
    rows = [{"storm": i + 1, "lat": round(x.get("lat", 0), 2), "lon": round(x.get("lon", 0), 2), "wind_ms": x.get("wind"), "radius_nm": x.get("r", 700)} for i, x in enumerate(items)]
    if place:
        name, la, lo = place
        for r in rows: r["km_from_" + name.split()[0].lower()] = round(hav(la, lo, r["lat"], r["lon"]))
        near = [r for r in rows if r["km_from_" + name.split()[0].lower()] <= 1500]; pr = Q("SELECT p.port_name, p.country, ROUND(p.congestion_index,1) AS congestion, w.current_severity AS weather, w.wind_speed AS wind_kmh, p.lat, p.lon FROM ports p LEFT JOIN weather_risks w ON w.port_name = p.port_name")
        pr["km"] = [round(hav(la, lo, a, b)) for a, b in zip(pr.lat, pr.lon)]; ports = pr[pr.km <= 1200].sort_values("km").drop(columns=["lat", "lon"]).head(10)
        txt = (f"**{len(near)}** active storm cell(s) within 1,500 km of the **{name}**" if near else f"No active storm cell is within 1,500 km of the **{name}**") + f". **{len(pr[pr.km <= 1200])}** ports lie within 1,200 km; the nearest are listed below."
        return A(txt, ports, s + "  -- ports: SELECT p.port_name, ... FROM ports p LEFT JOIN weather_risks w ON w.port_name = p.port_name (distance computed from lat/lon)")
    if not rows: return A("There are **no active storms** right now.", pd.DataFrame(columns=["storm", "lat", "lon", "wind_ms", "radius_nm"]), s)
    return A(f"**{len(rows)}** active storm cell(s). The strongest has winds of **{max(r['wind_ms'] or 0 for r in rows)} m/s**.", pd.DataFrame(rows), s)

def i_weather_worst(c):
    waves = _has(c.q, "wave", "swell")
    col = "wave_height" if waves else "wind_speed"
    df, s = run(f"SELECT port_name, current_severity AS severity, ROUND(wind_speed,1) AS wind_kmh, ROUND(wave_height,1) AS wave_m, ROUND(temperature,1) AS temp_c FROM weather_risks ORDER BY {col} DESC LIMIT ?", [c.n])
    r = df.iloc[0]; return A(f"Strongest {'waves' if waves else 'winds'}: **{r.port_name}** ({r.wave_m} m waves, {r.wind_kmh} km/h wind).", df, s)

def i_weather_port(c):
    if not c.ports: return None
    ph = ",".join("?" * len(c.ports[:5]))
    df, s = run(f"SELECT port_name, current_severity AS severity, ROUND(wind_speed,1) AS wind_kmh, ROUND(wave_height,1) AS wave_m, ROUND(temperature,1) AS temp_c, forecast FROM weather_risks WHERE port_name IN ({ph})", c.ports[:5])
    if not len(df): return A(f"I have no weather record for **{c.ports[0]}**.", df, s)
    r = df.iloc[0]; return A(f"Weather at **{r.port_name}**: wind {r.wind_kmh} km/h, waves {r.wave_m} m, {r.temp_c} °C, severity **{r.severity}**.", df, s)

def i_weather_high(c):
    df, s = run("SELECT port_name, current_severity AS severity, ROUND(wind_speed,1) AS wind_kmh, ROUND(wave_height,1) AS wave_m FROM weather_risks WHERE current_severity IN ('High', '3', '4', 3, 4) ORDER BY wind_speed DESC LIMIT 25")
    return A(f"**{len(df)}** ports are at high weather severity.", df, s)

# ------------------------------------------------------------------ customs
def i_customs_rules(c):
    goods = Q("SELECT goods FROM customs_rules").goods.tolist(); g = next((x for x in goods if _has(c.q, x.lower())), None)
    if g:
        df, s = run("SELECT chapter, goods, duty_pct, docs AS required_documents, restricted FROM customs_rules WHERE goods = ?", [g]); r = df.iloc[0]
        return A(f"**{r.goods}** (HS chapter {r.chapter}): duty **{r.duty_pct}%**, " + ("restricted goods, " if r.restricted else "") + f"required documents: {r.required_documents.replace(';', ', ')}.", df, s)
    if _has(c.q, "restricted", "prohibited", "controlled"):
        df, s = run("SELECT chapter, goods, duty_pct, docs AS required_documents FROM customs_rules WHERE restricted = 1 ORDER BY duty_pct DESC"); return A(f"**{len(df)}** goods categories are restricted.", df, s)
    df, s = run("SELECT chapter, goods, duty_pct, restricted FROM customs_rules ORDER BY duty_pct DESC LIMIT ?", [max(c.n, 5)]); r = df.iloc[0]
    return A(f"Highest import duty: **{r.goods}** at **{r.duty_pct}%**.", df, s)

def i_customs_docs_needed(c):
    df, s = run("SELECT type, severity, COUNT(*) AS open_tickets FROM agent_actions WHERE type = 'customs_docs' AND status IN ('open','proposed') GROUP BY type, severity")
    n = int(df.open_tickets.sum()) if len(df) else 0; return A(f"**{n}** shipments currently have an open ticket for missing customs documents.", df, s)

# ------------------------------------------------------------------ tickets
def i_tickets(c):
    df, s = run("SELECT type, severity, COUNT(*) AS tickets FROM agent_actions WHERE status IN ('open','proposed') GROUP BY type, severity ORDER BY tickets DESC")
    n = int(df.tickets.sum()) if len(df) else 0; crit = int(df[df.severity == "Critical"].tickets.sum()) if len(df) else 0
    return A(f"There are **{n}** open agent tickets, **{crit}** of them critical.", df, s)


# ------------------------------------------------------------------ intent registry
# must: list of phrase groups, every group needs one phrase;  boost: extra groups that add score.  domain -> rbac.ROLE_DATA.  cust: allowed for a Customer (own data only)
def I(id, domain, must, fn, examples, boost=(), cust=False):
    return {"id": id, "domain": domain, "must": must, "boost": boost, "fn": fn, "examples": examples, "cust": cust}

CHEAP = ("cheapest", "cheap", "cheaper", "costliest", "expensive", "dearest", "priciest", "lowest cost", "least expensive", "lowest price", "best price", "most economical")
INTENTS = [
    I("carrier_cheapest", "carriers", [CHEAP, ("carrier", "line", "shipping line", "liner", "who", "operator")], i_carrier_cheapest, ["Which carrier is cheapest to Rotterdam?", "Which carrier is the most expensive?"], boost=[("to", "for", "serving", "port")]),
    I("carrier_reliable", "carriers", [("reliable", "reliability", "on-time", "punctual", "on time", "late", "unreliable"), ("carrier", "line", "shipping line", "liner")], i_carrier_reliable, ["Which carrier is least reliable?", "Which carrier has the best on-time rate?"]),
    I("carrier_risk", "carriers", [("risk", "risky", "dangerous"), ("carrier", "line", "liner")], i_carrier_risk, ["Which carriers are high risk?", "How many carriers have medium risk?"]),
    I("carrier_rating", "carriers", [("rated", "rating", "stars", "top carrier", "best carrier"), ("carrier", "line", "liner")], i_carrier_rating, ["Which carrier has the highest rating?"]),
    I("carrier_lookup", "carriers", [("tell me about", "about", "details", "info", "how is", "how many", "does", "profile", "performance", "carry", "carries")], i_carrier_lookup, ["Tell me about Maersk Line", "How many shipments does MSC Cargo carry?"]),
    I("port_congestion_top", "ports", [("congested", "congestion", "busiest", "crowded", "jammed", "bottleneck")], i_port_congestion_top, ["Which ports are most congested?", "Which ports in Asia are most congested?", "Which ports are least congested?"], boost=[("ports", "port", "which", "top")]),
    I("port_congestion_lookup", "ports", [("congested", "congestion", "busy", "crowded", "dwell"), ("how", "what", "is", "level", "at", "in")], i_port_congestion_lookup, ["How congested is Singapore?", "What is the congestion at Rotterdam?"], boost=[("port",)]),
    I("port_dwell", "ports", [("dwell", "waiting time", "wait time", "turnaround")], i_port_dwell, ["Which port has the longest dwell time?"]),
    I("ports_by_region", "ports", [("region", "regions", "how many ports", "number of ports", "ports in")], i_ports_by_region, ["How many ports are in Asia?", "Show ports by region"], boost=[("how many", "number of", "count", "by region")]),
    I("congested_count", "ports", [("how many", "number of", "count"), ("congested", "congestion")], i_congested_count, ["How many ports have congestion above 4?"], boost=[("above", "over", "more than")]),
    I("customs_hold", "shipments", [("customs hold", "on hold", "held in customs", "held by customs", "customs delay", "held at customs"), ("shipments", "shipment", "how many", "cargo", "containers", "which")], i_customs_hold, ["How many shipments are on customs hold?"], cust=True),
    I("shipments_delayed", "shipments", [("delayed", "late", "delay", "behind schedule", "running late")], i_shipments_delayed, ["How many shipments are delayed?"], boost=[("shipments", "shipment", "cargo")], cust=True),
    I("shipments_by_status", "shipments", [("status", "in transit", "delivered", "anchorage", "by status", "how many shipments", "number of shipments", "shipments are")], i_shipments_by_status, ["Show shipments by status", "How many shipments are in transit?"], boost=[("shipments", "shipment")], cust=True),
    I("shipments_risk", "shipments", [("delay risk", "risk of delay", "at risk", "riskiest", "most likely to be late", "predicted delay"), ("shipments", "shipment", "cargo")], i_shipments_risk, ["Which shipments have the highest delay risk?"], cust=True),
    I("shipments_to_port", "shipments", [("shipments", "shipment", "cargo", "containers", "arriving", "heading", "bound", "departing", "leaving"), ("to", "from", "at", "for", "arriving", "heading", "bound", "departing", "leaving")], i_shipments_to_port, ["Which shipments are heading to Rotterdam?", "Shipments leaving Mundra Port"], boost=[("arriving", "heading", "bound", "departing", "leaving", "eta")], cust=True),
    I("shipments_heaviest", "shipments", [("heaviest", "lightest", "biggest", "largest", "smallest", "heavy", "weight"), ("shipments", "shipment", "cargo", "container")], i_shipments_heaviest, ["Which are the heaviest shipments?"], cust=True),
    I("vessels_at_sea", "shipments", [("vessels", "vessel", "ships at sea", "at sea", "tracked", "ais")], i_vessels_at_sea, ["How many vessels are at sea?"], cust=True),
    I("shipment_lookup", "shipments", [("shp",)], i_shipment_lookup, ["Where is SHP-0001?"], cust=True),
    I("quotes_low_margin", "quotes", [("margin", "margins", "low margin", "thin margin"), ("below", "under", "less than", "low", "lower than", "poor", "thin", "weak", "lowest")], i_quotes_low_margin, ["Which quotes are below 10% margin?", "Show quotes with a low margin"]),
    I("quotes_high_margin", "quotes", [("margin", "margins"), ("above", "over", "more than", "high", "highest", "best", "greater than")], i_quotes_high_margin, ["Show quotes above 20% margin"]),
    I("quotes_avg", "quotes", [("average", "mean", "avg", "overall"), ("margin", "margins", "quote value", "price")], i_quotes_avg, ["What is the average margin?"]),
    I("quotes_high_value", "quotes", [("quotes", "quote", "quotation"), ("above", "over", "more than", "exceed", "exceeding", "greater than", "big", "large", "expensive", "high value")], i_quotes_high_value, ["Show quotes above $50,000"], boost=[("price", "value", "worth", "$")], cust=True),
    I("quotes_by_status", "quotes", [("quotes", "quote", "quotation"), ("status", "pending", "approved", "rejected", "on hold", "draft", "submitted", "accepted", "how many", "number of", "by status")], i_quotes_by_status, ["How many quotes are pending approval?", "Show quotes by status"], cust=True),
    I("quotes_latest", "quotes", [("latest", "recent", "newest", "last", "new"), ("quotes", "quote", "quotation")], i_quotes_latest, ["Show the latest quotes"], cust=True),
    I("quote_lookup", "quotes", [("qte",)], i_quote_lookup, ["What is the status of QTE-0001?"], cust=True),
    I("customer_credit", "customers", [("customer", "customers", "client", "clients", "shipper"), ("credit", "credit risk", "risky")], i_customer_credit, ["Which customers have the highest credit risk?"], boost=[("credit",)]),
    I("top_customers", "customers", [("customer", "customers", "client", "clients", "shipper"), ("most", "top", "biggest", "largest", "best", "highest", "quoted value", "quote value", "revenue", "spend", "spends")], i_top_customers, ["Which customer has the highest quoted value?"]),
    I("customer_tiers", "customers", [("platinum", "gold", "silver", "tier", "how many customers", "number of customers", "customers")], i_customer_tiers, ["How many Platinum customers do we have?"]),
    I("storms", "weather", [("storm", "storms", "cyclone", "typhoon", "hurricane", "bad weather")], i_storms, ["Show storms near the Red Sea", "Are there any active storms?"]),
    I("weather_worst", "weather", [("wind", "winds", "windy", "waves", "wave", "swell", "rough", "weather"), ("worst", "strongest", "highest", "roughest", "most", "top")], i_weather_worst, ["Which ports have the strongest winds?", "Which ports have the highest waves?"]),
    I("weather_high", "weather", [("weather",), ("high risk", "severe", "high severity", "severity", "dangerous", "risk")], i_weather_high, ["Which ports are at high weather severity?"]),
    I("weather_port", "weather", [("weather", "wind", "waves", "temperature", "forecast", "conditions")], i_weather_port, ["What is the weather at Singapore?"], boost=[("at", "in", "for")]),
    I("customs_rules", "customs", [("duty", "tariff", "duties", "customs rules", "restricted", "documents required", "required documents", "hs code", "documents", "document", "import")], i_customs_rules, ["What is the duty on pharmaceuticals?", "Which goods are restricted?"]),
    I("customs_docs_needed", "customs", [("missing", "need", "needs", "require", "required", "lack"), ("documents", "document", "docs", "paperwork")], i_customs_docs_needed, ["How many shipments need customs documents?"]),
    I("tickets", "tickets", [("tickets", "ticket", "alerts", "alert", "issues", "incidents"), ("open", "critical", "how many", "pending", "active")], i_tickets, ["How many critical tickets are open?"]),
]
EXAMPLE_POOL = [(e, it["domain"], it["id"], it["cust"]) for it in INTENTS for e in it["examples"]]

GREETING = re.compile(r"^\W*(hi|hello|hey|hii+|good (morning|afternoon|evening)|namaste|yo|hola)\b[\w\s!.,]*$", re.I)
THANKS = re.compile(r"^\W*(thanks|thank you|thx|ty|great|cool|nice|awesome|perfect)\b[\w\s!.,]*$", re.I)
HELP = re.compile(r"\b(help|what can you do|what can i ask|examples?|capabilit\w+)\b", re.I)


def allowed(role, intent):
    data = rbac.ROLE_DATA.get(role, set())
    if intent["domain"] in data: return True
    if role == "Customer" and intent["cust"] and intent["domain"] in ("quotes", "shipments"): return True
    return False


def suggestions(role, text, k=3):
    pool = [e for e, dom, iid, cust in EXAMPLE_POOL if allowed(role, next(i for i in INTENTS if i["id"] == iid))]
    if role == "Customer": pool = [e for e in pool if "margin" not in e.lower() and "customer" not in e.lower()]
    if not pool: return []
    ranked = sorted(pool, key=lambda e: -difflib.SequenceMatcher(None, _norm(text), _norm(e)).ratio()); out = []
    for e in ranked:
        if e not in out: out.append(e)
        if len(out) == k: break
    return out


def _score(it, q):
    s = 50 if it["id"] in ("quote_lookup", "shipment_lookup") else 0
    for g in it["must"]:
        if not _has(q, *g): return 0
        s += 10
    for g in it["boost"]:
        if _has(q, *g): s += 2
    return s


def answer(user, text):
    """-> dict(kind, text, sql, rows, table, note, intent, suggestions). kind: answer | denied | unknown | greeting."""
    role = (user or {}).get("role"); raw = (text or "").strip()
    if GREETING.match(raw): return {"kind": "greeting", "text": "Hello! I answer questions about your freight data with exact database queries, and I show the SQL behind every answer. Try one of the examples below.", "sql": "", "rows": 0, "suggestions": suggestions(role, "")}
    if THANKS.match(raw): return {"kind": "greeting", "text": "You're welcome! Ask me anything else about your data.", "sql": "", "rows": 0, "suggestions": []}
    if HELP.search(raw) and len(raw.split()) <= 6: return {"kind": "greeting", "text": "I can answer questions about ports, carriers, shipments, quotes, weather, customs and open tickets (what your role may see). Pick one:", "sql": "", "rows": 0, "suggestions": suggestions(role, "", 5)}
    q, fixes = correct(raw); c = Ctx(user, q, raw, fixes)
    note = ("Interpreted as: “" + q + "”") if fixes else ""
    cands = sorted(((_score(it, q), n, it) for n, it in enumerate(INTENTS)), key=lambda x: (-x[0], x[1])); cands = [x for x in cands if x[0] > 0]
    denied = None
    for sc, _, it in cands:
        if not allowed(role, it):
            denied = it; break                # the best match is data this role may not read: refuse instead of answering something else
        try: r = it["fn"](c)
        except Exception as e: r = None
        if r:
            r.update(kind="answer", intent=it["id"], note=note)
            df = r.pop("df", None); r["table"] = None if df is None or not len(df) else {"cols": list(map(str, df.columns)), "rows": [[(None if (isinstance(v, float) and v != v) else v) for v in row] for row in fmt_df(df).head(25).itertuples(index=False, name=None)], "total": int(len(df))}
            return r
    if denied is not None:
        need = {"quotes": "quote and margin", "customers": "customer", "weather": "weather", "customs": "customs", "ports": "port", "carriers": "carrier", "shipments": "shipment", "tickets": "agent ticket"}.get(denied["domain"], denied["domain"])
        who = "your own shipments and quotes" if role == "Customer" else "the pages your role can open"
        return {"kind": "denied", "text": f"I'm sorry, I can't answer that for your role (**{role}**): it needs {need} data that this role is not allowed to read. I can only use {who}. Here are questions I can answer for you:", "sql": "", "rows": 0, "note": note, "suggestions": suggestions(role, raw)}
    return {"kind": "unknown", "text": "I'm sorry, I couldn't match that to the data I can query. I only answer from the database, so I won't guess. Here are three questions that work:", "sql": "", "rows": 0, "note": note, "suggestions": suggestions(role, raw)}
