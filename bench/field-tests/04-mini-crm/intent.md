# Mini CRM

Build a small CRM for tracking companies, contacts, and deals.

Required behavior:

- Provide a web UI and a FastAPI backend.
- Persist data in a local JSON file or SQLite database. No external services.
- Entities:
  - Companies: name, industry, website.
  - Contacts: name, email, role, company.
  - Deals: title, company, value, stage, next step.
- UI workflows:
  - Create and list companies.
  - Create and list contacts linked to companies.
  - Create and update deals linked to companies.
  - Filter deals by stage.
  - Show a simple dashboard with total pipeline value and open deal count.
- Include `start.sh` at the repo root. It must serve the user-facing app on
  `$PORT`; use `$API_PORT` or `$BACKEND_PORT` for any separate backend process.
- Include `tests/run_acceptance.py` that verifies the API and one end-to-end
  seed workflow through the app shell or served HTML.

Keep the scope deliberately tight. No auth, no teams, no email sending, and no
third-party CRM integrations.
