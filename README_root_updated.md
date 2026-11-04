# Infosys Springboard Internship 7.0 — Maritime AI Project

This repository contains the team's milestone deliverables for **Infosys Springboard Internship 7.0, Batch 4**. The project develops an AI-assisted maritime freight platform, with milestone submissions covering user authentication and role-based freight operations.

## Project milestones

### Milestone 1 — User Authentication Module

A Python and Streamlit authentication application with user registration, login, JWT sessions, password recovery, and separate user and administrator dashboards.

- **Notebook:** [Infosys_Milestone1_Authentication.ipynb](Milestone1/Infosys_Milestone1_Authentication.ipynb)
- **Documentation and run instructions:** [Milestone1/README.md](Milestone1/README.md)
- **Application source:** [Milestone1/app.py](Milestone1/app.py)
- **Evidence screenshots:** [Milestone1/screenshots/](Milestone1/screenshots/)

The Milestone 1 README describes the features, technology stack, Google Colab setup, Colab Secrets, and screenshot evidence.

### Milestone 2 — FreightQuote AI

An agentic AI-powered maritime freight application for freight pricing and route optimization. Milestone 2 adds account management, five application roles, server-side role-based access control (RBAC), an Admin dashboard, profile management, freight operations, and a database-backed SQL Copilot.

- **Notebook:** [Milestone2/Milestone2.ipynb](Milestone2/Milestone2.ipynb)
- **Documentation, roles and privileges:** [Milestone2/README.md](Milestone2/README.md)
- **Python dependencies:** [Milestone2/requirements.txt](Milestone2/requirements.txt)
- **Application source:** [Milestone2/app/](Milestone2/app/)
- **Milestone 2 screenshots:** [Milestone2/Screenshots/](Milestone2/Screenshots/)

The Milestone 2 notebook is the canonical, self-contained Google Colab submission. The `app/` directory contains source files mirrored from the notebook's file-writing cells so reviewers can browse the application code directly on GitHub. The Milestone 2 README contains setup instructions, the role and permission matrices, Copilot question examples, security notes, and screenshot evidence.

## Team 03 — contribution summary

| Team member | Milestone 2 contribution |
|---|---|
| **Mehak** | Login page, Admin dashboard and Freight Broker role |
| **Bhavana** | Operations Manager role |
| **Bhargav** | Customer role |
| **Shahabuddin** | Auditor role |

## Technology overview

| Area | Milestone 1 | Milestone 2 |
|---|---|---|
| Application | Python, Streamlit | Python, FastAPI, Uvicorn |
| Authentication | JWT, PyJWT | JWT (HS256), PyJWT, bcrypt |
| Data storage | JSON file in the Colab runtime | SQLite |
| UI | Streamlit | HTML, CSS and JavaScript |
| Runtime / sharing | Google Colab, ngrok | Google Colab, Colab proxy / ngrok |
| Additional capabilities | Gmail OTP password recovery | Server-side RBAC, freight agents, SQL Copilot, maps and charts |

## Repository structure

```text
.
├── Milestone1/
│   ├── Infosys_Milestone1_Authentication.ipynb
│   ├── README.md
│   ├── app.py
│   └── screenshots/
├── Milestone2/
│   ├── Milestone2.ipynb
│   ├── README.md
│   ├── requirements.txt
│   ├── app/
│   └── Screenshots/
├── LICENSE
└── README.md
```

## Running the submissions

Both milestones are designed to be run in **Google Colab**.

1. Open the relevant milestone notebook from its folder.
2. Follow the Colab Secrets and setup instructions in that milestone's README.
3. Run the notebook cells from top to bottom.
4. Use the application link printed by the notebook while the Colab runtime is active.

Read the milestone-specific README before running the notebook. **Do not commit passwords, API keys, OTPs, JWT secrets, generated credentials, runtime databases, or executed notebook outputs containing private information.** Use test accounts for demonstrations and stop public tunnels when finished.

## Documentation

For complete feature descriptions, setup details, security notes and evidence, use the dedicated README for each milestone:

- [Milestone 1 README](Milestone1/README.md)
- [Milestone 2 README](Milestone2/README.md)
