# FreightQuote AI — Milestone 2

**Infosys Springboard Internship 7.0 · Batch 4**  
**Agentic AI for Maritime Freight Pricing and Route Optimization**

*FastAPI · SQLite · JWT · bcrypt · Role-Based Access Control · Google Colab · ngrok*

FreightQuote AI is a multi-user maritime freight platform. Milestone 2 brings authentication and account management into the provided freight application, introduces five role-based user types, protects pages and operations on the server, adds a Profile page and Admin dashboard, and extends the SQL-based AI Copilot.

The submission is provided as one self-contained Google Colab notebook. The notebook writes the Python application modules and front-end files into the Colab runtime, extracts the bundled front-end libraries, starts the FastAPI service and displays the application link.

---

## Repository layout

```text
Milestone2/
├── Milestone2.ipynb    # Complete, self-contained Colab project
└── README.md           # Project overview, setup and role documentation
```

The notebook creates the following application structure inside the Colab runtime; these files are embedded in notebook cells and are not separate repository uploads:

```text
/content/app/
├── server.py
├── auth.py
├── rbac.py
├── config.py
├── db.py
├── hardening.py
├── mailer.py
├── copilot_plus.py
├── analyst.py
├── qa.py
├── intent_router.py
├── domain_freight.py
├── live_engine.py
├── worker.py
├── seed_data.py
├── scale_data.py
├── entities.py
├── blocks.py
├── views_core.py
├── views_freight.py
├── views_platform.py
├── alts.py
├── seanav.py
├── weather.py
├── weather_context.py
├── gpu.py
└── static/
    ├── app.js
    ├── index.html
    ├── style.css
    └── vendor/            # Bundled front-end libraries extracted at runtime
```

Runtime databases and generated account credentials are stored under `/content/app_data/`, outside the repository submission folder.

---

## Features

### Authentication and account security

- Sign up and sign in using a username or registered e-mail address.
- Forgot-password flow supports security-question recovery and e-mail OTP recovery.
- Passwords and security answers are stored as bcrypt hashes.
- Login issues an HS256-signed JWT session token. The configured session lifetime is eight hours.
- Unknown-account and incorrect-password login attempts return the same generic error message.
- Five incorrect password attempts lock the account.
- The server rechecks the account on authenticated requests. Account locks, deletion, role changes and password changes are reflected in subsequent requests; revoked sessions are rejected.
- Password validation follows the Milestone 1 rules: at least eight characters, upper- and lower-case letters, a number and a special character. The implementation also rejects spaces, common passwords and passwords containing the username or e-mail name.
- E-mail validation checks the required minimum letter counts before `@`, between `@` and the final dot, and after the final dot.
- Self-registration creates a Customer account; users do not choose their own role on the sign-up form.

### Profile page

- Displays the signed-in user's account details and role.
- Supports username changes and password changes after confirming the current password.
- Supports a profile avatar, with preset avatars and image upload.
- Shows account and login information supplied by the authentication service.
- Temporary administrator-issued passwords require the user to choose a new password before using the rest of the application.

### Five roles and server-side RBAC

The backend defines five roles. The role is loaded from the authenticated account in SQLite; it is not selected by the user at login. The navigation menu is role-aware, and the API independently enforces permissions. Attempting to open a prohibited page or perform a prohibited action is rejected with HTTP `403`.

### Admin dashboard

- Displays total users, user counts by role, locked users, and active/inactive account presence.
- Lists user account details and status.
- Supports adding users, changing roles, locking and unlocking accounts, setting temporary or permanent passwords, and deleting users with confirmation.
- Records administrative and security-related actions in an activity log and displays the latest 50 entries.
- Prevents an administrator from deleting or locking their own account, and protects the last remaining Admin from deletion or demotion.
- Does not display password hashes.

### Freight operations and agents

The platform includes these freight-operational pages:

- **Route AI** — maritime route options, route comparison and congestion context.
- **Spot Quotes** — freight quote creation and the quote-processing workflow.
- **Carriers** — carrier information, rankings and risk context.
- **Weather Risk** — weather conditions, storm scenarios and weather-related route risk.
- **Margin** — quote margin and pricing information.
- **Customs** — customs rules, shipment holds and document handling.
- **Run agents** — an operations page for running an individual background agent or the agent cycle.

A background worker runs the freight-agent cycles and weather refresh work independently of the web request handler.

### AI Copilot

- Supports **35 defined question intents** across carriers, ports, shipments, quotes, customers, weather, customs and agent tickets.
- Answers from the application's database using fixed, parameterised SQL queries; it does not use an LLM or RAG to invent database figures.
- Displays the SQL used and the returned row count with database-backed answers.
- Handles alternate phrasings and vocabulary/spelling corrections for domain terms and names.
- Responds to greetings and help requests.
- For unsupported questions, replies politely and suggests valid questions.
- Applies role-based data access. A Customer's Copilot is limited to that customer's own shipments and quotes.

Example questions supported by the Copilot include:

- Which carrier is cheapest to Rotterdam?
- Which carrier is least reliable?
- Which ports are most congested?
- How many shipments are on customs hold?
- Which shipments have the highest delay risk?
- Which shipments are heading to Rotterdam?
- Which are the heaviest shipments?
- How many vessels are at sea?
- Which quotes are below 10% margin?
- What is the average margin?
- Show the latest quotes.
- What is the status of QTE-0001?
- Which customer has the highest quoted value?
- How many Platinum customers do we have?
- Show storms near the Red Sea.
- Which ports have the strongest winds?
- What is the weather at Singapore?
- What is the duty on pharmaceuticals?
- How many shipments need customs documents?
- How many critical tickets are open?

The figures returned depend on the data currently seeded in the runtime database and may therefore differ between runs.

---

## Roles and privileges

### Page access matrix

| Role | Copilot | Route | Quotes | Carriers | Weather | Margin | Customs | Run agents | Users / Admin | Profile |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Admin | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Operations Manager | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ |
| Freight Broker | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✓ |
| Auditor | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ |
| Customer | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |

### Role descriptions

| Role | Purpose and permissions |
|---|---|
| **Admin** | Platform operator. Access to all pages, quote creation, agent actions, storm injection, user and role management, and the activity log. |
| **Operations Manager** | Runs logistics. Access to Route, Carriers, Weather, Customs and Copilot; can run agents, re-route and resolve customs-document actions. Cannot manage users or use the Quotes and Margin pages. |
| **Freight Broker** | Sells and prices freight. Access to Copilot, Quotes, Carriers and Margin; can create quotes. Cannot inject storms, run agents or edit customs data. |
| **Auditor** | Compliance reviewer. Read-only access to operational pages and Copilot. Cannot create quotes, run agents or manage users. |
| **Customer** | External shipper. Access to Copilot and Quotes for their own account, plus Profile. Cannot access other operational pages. |

### Action capabilities

| Capability | Admin | Operations Manager | Freight Broker | Auditor | Customer |
|---|:---:|:---:|:---:|:---:|:---:|
| Use Copilot | ✓ | ✓ | ✓ | ✓ | ✓ |
| View internal operational data | ✓ | ✓ | ✓ | ✓ | ✗ |
| Create quotes | ✓ | ✗ | ✓ | ✗ | ✓ (own account) |
| View own customer records | — | — | — | — | ✓ |
| Run agents / refresh weather | ✓ | ✓ | ✗ | ✗ | ✗ |
| Approve re-route | ✓ | ✓ | ✗ | ✗ | ✗ |
| Resolve customs actions | ✓ | ✓ | ✗ | ✗ | ✗ |
| Inject a demo storm | ✓ | ✗ | ✗ | ✗ | ✗ |
| Manage users and activity log | ✓ | ✗ | ✗ | ✗ | ✗ |

The permission checks are performed on the server, not only by hiding navigation items. Customer quote creation and quote detail access are scoped to the Customer's own account.

---

## Technology stack

| Component | Technology |
|---|---|
| Application server and API | Python, FastAPI, Uvicorn |
| Authentication | JWT (HS256), PyJWT, bcrypt |
| Data storage | SQLite |
| Data processing | Pandas, NumPy, SciPy, scikit-learn |
| Background processing | Python worker process and live-engine modules |
| Front end | HTML, CSS and JavaScript single-page application |
| Charts and maps | ECharts and Leaflet (front-end libraries bundled in the notebook) |
| Weather and network requests | Requests and the weather modules included in the notebook |
| Document handling | python-multipart and pdfplumber |
| Runtime and sharing | Google Colab and pyngrok/ngrok fallback |

The notebook installs its Python dependencies in its first setup cell and extracts the bundled front-end libraries during execution.

---

## How to run in Google Colab

1. Open `Milestone2.ipynb` in Google Colab.
2. Open the **Secrets** panel using the key icon in the left sidebar.
3. Add the secrets listed below. Enable notebook access for each secret.
4. Choose **Runtime → Restart session**, then **Runtime → Run all** from the first cell.
5. Wait for the notebook to print **FreightQuote AI is running**.
6. Open the **Open the app** link printed by the final cell. The notebook uses the Colab proxy first and attempts ngrok if the Colab proxy is unavailable.
7. On the first start, the application initializes its databases, creates the Admin and demo accounts when needed, seeds the sample freight data and starts the background worker. The first agent cycle may take about a minute.

### Colab Secrets

| Secret | Purpose | Requirement |
|---|---|---|
| `JWT_SECRET` | Signs authentication tokens. | Required for the intended configured setup. |
| `NGROK_AUTHTOKEN` | Provides the public ngrok link if the Colab proxy is unavailable. | Fallback; add when using ngrok. |
| `EMAIL_ADDRESS` | Gmail address used to send password-reset OTP messages. | Needed for e-mail OTP recovery. |
| `EMAIL_PASSWORD` | Gmail App Password for the sender account. | Needed for e-mail OTP recovery. |
| `ADMIN_PASSWORD` | Password for the initial Admin account. | Optional; a generated password is used when not supplied. |
| `ADMIN_EMAIL` | E-mail address for the initial Admin account. | Optional; defaults to `admin@freightquote.ai`. |
| `DEMO_PASSWORD` | Password shared by the four seeded non-Admin demo accounts. | Optional; generated passwords are used when not supplied. |

Never type credentials, tokens or API keys directly into notebook cells. If generated account passwords are shown during the first run, record them privately. Do not save those outputs in the public repository.

### Seeded demo accounts

When demo-user seeding is enabled by the notebook, the application creates one initial account for each role. The usernames are:

| Role | Username |
|---|---|
| Admin | `admin` |
| Operations Manager | `ops_manager` |
| Freight Broker | `freight_broker` |
| Auditor | `auditor` |
| Customer | `customer_demo` |

The passwords are supplied through Colab Secrets or generated at runtime. They are intentionally not written into this README.

---

## Security and runtime notes

- Passwords and security answers are stored as bcrypt hashes; public user responses do not include password hashes.
- JWT tokens are signed, not encrypted. Sensitive values must not be placed in token payloads.
- Authentication and role permissions are checked by the API. A locked or deleted account cannot continue using an old session.
- Login, sign-up, password-reset and other sensitive routes are rate limited by the hardening middleware.
- The notebook clears saved cell outputs in this submission copy. Run the notebook in your own Colab session to regenerate runtime links and account credentials.
- SQLite databases, generated credential files, logs and runtime data belong in the Colab runtime data directory. Do not upload these files, tokens, logs or generated credentials to GitHub.
- Colab runtime storage may be temporary. The application data is kept in the runtime data directory unless a persistent data directory is configured.
- The ngrok URL is accessible to anyone who has the link while the tunnel is active. Use demo accounts for evaluation and stop the runtime/tunnel when finished.
- Before using the application outside an educational demonstration, configure strong credentials and review the demonstration defaults and deployment security settings.

---

## Suggested verification checklist

Use separate accounts for the five roles and verify the applicable page and action permissions.

- [ ] Sign up, sign in and sign out.
- [ ] Test password recovery with the security-question flow.
- [ ] Configure Gmail secrets and test e-mail OTP recovery.
- [ ] Confirm that five incorrect password attempts lock an account.
- [ ] Open Profile and change username and password.
- [ ] Confirm that each role sees only its permitted pages.
- [ ] Try a restricted page or action directly and confirm that the server returns HTTP `403`.
- [ ] As Admin, add a user, change a role, set a temporary/permanent password, lock/unlock and delete a user.
- [ ] Confirm that the Admin cannot lock or delete their own account and that the last Admin is protected.
- [ ] Ask the Copilot a greeting and several data questions; inspect the SQL and row count shown with answers.
- [ ] Generate a freight quote and inspect the Route, Weather, Margin and Customs pages.
- [ ] Run an individual agent and the full agent cycle.

This checklist is provided for the final Colab verification run; it is not a claim that every item was re-executed while preparing the repository ZIP.

---

## Submission contents

This Milestone 2 folder contains the complete self-contained notebook and this README. Screenshot evidence is managed separately and is not included in this archive.
