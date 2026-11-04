"""Profile and Admin pages. They are rendered by dedicated front-end blocks that talk to /api/profile and /api/admin/*."""
import blocks as B

def profile_page(c):
    return B.page("My profile", "Your account, role and password", [{"t": "profile", "key": "profile"}], live=0)

def admin_page(c):
    return B.page("Admin dashboard", "Users, active / inactive status, roles, passwords, locks and the activity log", [{"t": "admin", "key": "admin"}], live=0)


def run_agents_page(c):
    import views_core as VC
    AG = [("route_watch", "Route & congestion agent", "Compares Suez and Cape routings, classifies port congestion and checks the weather along each lane."),
          ("weather_watch", "Weather risk agent", "Reads live wind, waves and rain for every port and raises re-route tickets for vessels in high-risk weather."),
          ("customs_watch", "Customs & tariff agent", "Checks required documents per HS chapter, holds shipments that miss documents and clears them when complete."),
          ("quote_watch", "Quotes, margin & approvals agent", "Prices spot quotes, checks the margin and sends quotes above $50,000 or below 10% margin to an approver."),
          ("anomaly_watch", "Anomaly agent", "Looks for unusual prices, delays and carrier behaviour across the live tables.")]
    bl = [B.callout("Each card shows the latest run of one agent. Press Run now to run it immediately, or use Run all agents at the top. The agents also run by themselves in the background.", "info", "Run agents")]
    bl += [VC.hero(c, a, t, d) for a, t, d in AG]
    return B.page("Run agents", "Start any agent now, or all of them", bl, live=10, actions=[{"label": "Run all agents", "url": "/api/run_cycle", "tone": "primary"}])
