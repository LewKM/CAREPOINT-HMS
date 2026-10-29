# CarePoint HMS

A hospital management system built with FastAPI, SQLAlchemy 2, Alembic and PostgreSQL (Neon). Pages are server-rendered Jinja templates on a custom design system with light and dark themes, so there is no frontend build step.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # set DATABASE_URL and SECRET_KEY
.venv/bin/alembic upgrade head    # create the schema
.venv/bin/python -m scripts.seed --demo   # reference data + one account per role (dev only)
./scripts/dev.sh                  # http://127.0.0.1:8010
```

Or in Docker:

```bash
./bin/dev-up                     # dev stack on http://localhost:8010 (reloads on code changes)
RUN_SEEDS=true ./bin/dev-up      # also seed reference data + demo accounts
docker compose up -d --build     # production, behind caddy-proxy on the shared 'web' network
```

Without `--demo`, the seed creates only the reference data and one administrator with a random temporary password, which must be changed at first sign-in. Use that mode for production.

Demo accounts all use the password `CarePoint#2026`:

| Role | Email |
|---|---|
| Hospital Administrator | admin@carepoint.local |
| Doctor | d.otieno@carepoint.local · a.hassan@… · p.kiprono@… |
| Nurse | m.achieng@carepoint.local · j.mutua@… |
| Receptionist | reception@carepoint.local |
| Pharmacist | pharmacy@carepoint.local |
| Lab Technician | lab@carepoint.local |
| Cashier | cashier@carepoint.local |

## Roles

Access is defined once in `app/permissions.py`. Route guards (`require("perm")`), template controls (`can(user, "perm")`) and the sidebar (`NAV` in `app/templating.py`) all read the same keys.

| Role | Does | Cannot |
|---|---|---|
| **Administrator** | Staff accounts, departments and fees, wards and beds, lab and drug price lists, billing oversight, voiding bills, audit log | Read or write clinical notes, vitals, results or prescriptions |
| **Doctor** | Consultations, diagnosis, prescriptions, lab orders, admission, progress notes, discharge | Billing, stock, registration |
| **Nurse** | Triage and vitals, triage priority, nursing notes, bed transfers, bed turnover | Prescribe, order labs, discharge |
| **Receptionist** | Register and edit patients, book, cancel and mark no-shows, check in, walk-in and emergency visits, bed board | Any clinical record |
| **Pharmacist** | Dispensing queue, dispense or cancel, formulary, restock and write-off | Clinical notes |
| **Lab Technician** | Worklist (STAT → urgent → routine), sample collection, result release, abnormal flags, test catalogue | Clinical notes |
| **Cashier** | Bills, payments (cash, M-Pesa, card, insurance, bank), receipts, daily collections | Void bills, clinical records |

## Patient flow

```
Register → Book → Check in ─┐
               Walk-in ─────┴→ Triage (nurse) → Consultation (doctor) ─┬→ Close visit
                                                                      ├→ Prescription → Pharmacy dispenses
                                                                      ├→ Lab order → Collect → Result
                                                                      └→ Admit → Ward notes / transfer → Discharge
```

**One bill per visit.** Each chargeable event posts to the visit's invoice automatically:

- Check-in adds the department's consultation fee.
- Each lab order adds the test price; cancelling the order posts a credit line.
- Dispensing adds each drug at quantity × unit price.
- A ward transfer bills the bed-days at the old ward's rate. Discharge bills the rest at the current ward's rate.

The cashier only collects payments. Manual charges and credits are available for anything else, such as procedures and waivers.

## Safeguards

- Sessions are signed cookies with a rolling 8-hour expiry. Every form carries a CSRF token. Changing a user's role or password, or deactivating them, signs out their other sessions straight away.
- After 5 failed sign-ins the account locks for 15 minutes. New accounts and admin password resets issue a temporary password that must be changed at first sign-in.
- An audit log records sign-ins, every view of a patient, visit or admission, and every change, with the user and IP address.
- Patient pages are sent with `Cache-Control: no-store`.
- Overlapping appointments for the same doctor are refused. A patient can't have two open visits. Beds and drug stock are row-locked when assigned or dispensed.
- Vitals outside adult reference ranges are flagged red. Allergies appear on every clinical screen and on the prescribing and dispensing forms.

## Working day

- **Ctrl+K** (⌘K) opens the command palette: every page and action the role may use, plus patient search by name, MRN, phone or ID. `/` jumps to the patient search box.
- Queues update themselves every 20–30 s: the visit queue, every dashboard, the pharmacy queue, the lab worklist and appointments. New arrivals are highlighted, and a background tab gets a ● in its title. A region the user is typing in is left alone.
- Waiting times turn amber after 30 minutes and red after an hour.
- Documents: **visit summary**, **lab report**, **discharge summary** and **invoice / receipt**. Each opens as an A4 preview with **Print** and **Download PDF**, and every print or download is recorded in the audit log.

## Documents & PDF

Each document is one template in `templates/print/`, styled by `static/css/print.css`. Add `?format=pdf` to any document URL and `services/documents.py` renders the same HTML to PDF with WeasyPrint.

- **Page:** A4 (210 × 297 mm), margins 16 / 15 / 20 mm. Every page carries a footer with the hospital, document type, patient and MRN, plus "Page n of N".
- **Long documents:** table headers repeat on each page, and rows, vitals and signature blocks never split across a page break.
- **Paid / void:** a paid receipt carries a PAID watermark. A void invoice is marked VOID, with "VOID — NOT VALID FOR PAYMENT" in the top margin of every page.
- **Same output both ways:** the stylesheet uses only tables and blocks, so the browser's Print → Save as PDF matches the server PDF.
- **No network:** fonts (Inter) are self-hosted in `static/fonts/`, and the renderer refuses any URL outside `/static/`, so a PDF never waits on the internet.
- **System libraries:** WeasyPrint needs Pango. The Docker images install it; for the local `.venv`, install `libpango-1.0-0 libpangoft2-1.0-0` with apt.

## Performance

The database is remote (Neon), so each round trip costs far more than the query itself. The app is built around that:

- Page loads (GET) run in autocommit, which skips the `BEGIN`/`ROLLBACK` round trips. Every change is a POST and keeps a real transaction.
- No connection pre-ping. TCP keepalives keep pooled connections alive instead.
- Dashboard figures are batched into one query each (`_figures` in `routers/dashboard.py`).
- Static files are linked through `asset()` with a content hash, cached for a year and gzipped. Icons are a 20 KB self-hosted sprite (`static/icons.svg`). After using a new Lucide icon name, rebuild it with `python -m scripts.build_icons icon-nodes.json` (see the script for where to get the file).

## Layout

```
app/
  main.py           app, middleware, error pages
  models.py         schema (20 tables) + enums
  permissions.py    role → capability matrix
  security.py       passwords, sessions, CSRF, require()
  templating.py     Jinja setup, filters, sidebar nav
  services/         billing (invoice_for/charge), audit, documents (A4 preview / PDF)
  routers/          auth, dashboard, patients, appointments, visits, wards, pharmacy, lab, billing, admin
  templates/        one folder per module; _macros.html holds shared components, print/ the A4 documents
  static/           app.css (design tokens + components), print.css (A4 documents), app.js, icons.svg (sprite), fonts/
alembic/            migrations — `alembic revision --autogenerate -m "…"` after model changes
scripts/            seed.py, dev.sh, build_icons.py
```
