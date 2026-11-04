"""Role-Based Access Control for FreightQuote AI.

ROLE_MENU  : role -> set of page ids the role may open (also enforced on /api/page/<id>)
ROLE_CAPS  : role -> set of capability names (checked by need(ctx, capability) in every action route)

Capability names
  copilot   use the AI Copilot
  view      read the internal operational pages (tables, agent tickets, margins ...)
  view_own  read only the records that belong to the user's own customer account
  quote     create a spot quote
  run       run agents / refresh weather
  approve   re-route a voyage
  resolve   edit customs data (attach documents to a held shipment or quote)
  storm     inject a demo storm
  users     manage users and roles, read the activity log (Admin dashboard)
"""
ROLES = ["Admin", "Operations Manager", "Freight Broker", "Auditor", "Customer"]
ROLE_BLURB = {
    "Admin": "Platform operator",
    "Operations Manager": "Runs logistics",
    "Freight Broker": "Sells and prices",
    "Auditor": "Compliance reviewer",
    "Customer": "External shipper",
}
PUBLIC_SIGNUP_ROLE = "Customer"          # self-registration always creates a Customer

PAGES = ["copilot", "route", "quotes", "carriers", "weather", "margin", "customs", "runagents", "profile", "admin"]
PAGE_LABEL = {"runagents": "Run agents", "copilot": "AI Copilot", "route": "Route", "quotes": "Quotes", "carriers": "Carriers", "weather": "Weather",
              "margin": "Margin", "customs": "Customs", "admin": "Users (Admin)"}

ROLE_MENU = {
    "Admin":              {"copilot", "route", "quotes", "carriers", "weather", "margin", "customs", "runagents", "profile", "admin"},
    "Operations Manager": {"copilot", "route", "carriers", "weather", "customs", "runagents", "profile"},
    "Freight Broker":     {"copilot", "quotes", "carriers", "margin", "profile"},
    "Auditor":            {"copilot", "route", "quotes", "carriers", "weather", "margin", "customs", "profile"},
    "Customer":           {"copilot", "quotes", "profile"},
}

ROLE_CAPS = {
    "Admin":              {"copilot", "view", "quote", "run", "approve", "resolve", "storm", "users"},
    "Operations Manager": {"copilot", "view", "run", "approve", "resolve"},
    "Freight Broker":     {"copilot", "view", "quote"},
    "Auditor":            {"copilot", "view"},
    "Customer":           {"copilot", "quote", "view_own"},
}

# Which parts of the data each role's Copilot may read (see copilot_plus.py). A question about a domain the role may not read is refused.
ROLE_DATA = {
    "Admin":              {"ports", "carriers", "shipments", "quotes", "weather", "customs", "tickets", "customers"},
    "Operations Manager": {"ports", "carriers", "shipments", "weather", "customs", "tickets"},
    "Freight Broker":     {"ports", "carriers", "quotes", "customers"},
    "Auditor":            {"ports", "carriers", "shipments", "quotes", "weather", "customs", "tickets", "customers"},
    "Customer":           {"own_quotes", "own_shipments"},
}

# Which entity kinds (drawer panels) are reachable from which pages
KIND_PAGES = {"quote": {"quotes", "margin"}, "shipment": {"route", "weather", "customs", "carriers"}, "port": {"route", "weather", "carriers", "customs"}}


def role_of(user):
    r = (user or {}).get("role")
    return r if r in ROLE_MENU else None


def menu_ids(role):
    return ROLE_MENU.get(role, set())


def caps(role):
    return ROLE_CAPS.get(role, set())


def has_cap(role, cap):
    return cap in ROLE_CAPS.get(role, set())


def can_open_page(role, pid):
    return pid in ROLE_MENU.get(role, set())


def can_open_entity(role, kind):
    return bool(KIND_PAGES.get(kind, set()) & ROLE_MENU.get(role, set()))


def permission_rows():
    """Rows for the permission table shown in the README and on the Admin dashboard."""
    cols = [("copilot", "Copilot"), ("route", "Route"), ("quotes", "Quotes"), ("carriers", "Carriers"), ("weather", "Weather"),
            ("margin", "Margin"), ("customs", "Customs")]
    rows = []
    for r in ROLES:
        d = {"role": r, "who": ROLE_BLURB[r]}
        for k, lab in cols: d[lab] = k in ROLE_MENU[r]
        d["Run agents"] = "run" in ROLE_CAPS[r]
        d["Users"] = "users" in ROLE_CAPS[r]
        rows.append(d)
    return rows


def permission_markdown():
    rows = permission_rows(); heads = [k for k in rows[0] if k not in ("role", "who")]
    out = "| Role | " + " | ".join(heads) + " |\n|---|" + "---|" * len(heads) + "\n"
    for r in rows: out += f"| {r['role']} | " + " | ".join("✓" if r[h] else "✗" for h in heads) + " |\n"
    return out


# Sidebar: every item is listed; the ones the role may not open are shown greyed out and disabled. (id, label, icon, group)
NAV = [("copilot", "AI Copilot", "bot", "Assistant"), ("route", "Route AI", "route", "Agents"), ("quotes", "Spot Quotes", "dollar", "Agents"), ("carriers", "Carriers", "truck", "Agents"),
       ("weather", "Weather Risk", "cloud", "Agents"), ("margin", "Margin", "trend", "Agents"), ("customs", "Customs", "file", "Agents"),
       ("runagents", "Run agents", "play", "Control"), ("admin", "Users", "users", "Control")]

def nav_for(role):
    return [{"id": i, "label": l, "icon": ic, "group": g, "enabled": i in ROLE_MENU.get(role, set())} for i, l, ic, g in NAV]
