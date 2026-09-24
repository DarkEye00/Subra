# SUBRA

Upload a PowerPoint, pay per deck with M-Pesa, and get back a polished, fully editable .pptx that keeps every piece of the original content. The full plan is in [deck-polisher-build-plan.md](deck-polisher-build-plan.md); status is in [PROGRESS.md](PROGRESS.md).

## How it fits together

- `polisher/` is the core pipeline, a standalone package with no Django dependency: validate → extract → plan (Claude) → render (in place, on a copy of the customer's file) → verify → preview. Video, audio, embedded objects, equations, 3D models, SmartArt and ink are kept exactly and only moved or scaled.
- `accounts/`, `billing/`, `orders/`, and `core/` make up the Django app: email login, price tiers and M-Pesa payments via IntaSend (behind a provider interface), orders with a logged state machine, Celery tasks, the admin, and privacy/terms pages.
- `templates/` and `static/` hold the mobile-first frontend (Tailwind v4, htmx).

## Polish a deck from the command line

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...            # not needed with --planner heuristic
.venv/bin/python -m polisher input.pptx --theme navy -o output.pptx --preview previews/ --report report.json
```

Themes: `navy`, `savanna`, `slate`. `--planner heuristic` uses the free rule-based planner (no API calls), which is useful for testing rendering. `--extract-only` prints the extracted content as JSON.

Each run prints every planner attempt with its tokens, cost, and verification result.

## Run the web app locally

Needs LibreOffice (`soffice`) and `pdftoppm` for previews.

```bash
npm install && npm run build:css
export DJANGO_DEBUG=1 POLISHER_PLANNER=heuristic   # or set ANTHROPIC_API_KEY and leave POLISHER_PLANNER unset
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Without `REDIS_URL`, Celery tasks run inline. Without IntaSend keys, DEBUG mode shows a "simulate a successful payment" button on the quote page, so you can try the whole flow.

## Tests

```bash
.venv/bin/pytest
```

The tests cover validation (including the 25/100 MB limits), extraction of every locked object type and image case, verification, planning and repair, in-place rendering round trips for every layout and theme, the retry and escalation pipeline, payments (IntaSend webhooks, idempotency, amount mismatches, late and duplicate payments), order processing, the spend cap, file deletion, and the views.

## Deploy (one DigitalOcean droplet, 4 GB RAM or more)

```bash
cp .env.example .env    # fill it in
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Compose runs `web` (gunicorn), `worker` (Celery; runs LibreOffice), `beat` (file deletion, queue release, payment sweeps), `postgres`, `redis`, and `caddy` (automatic HTTPS for `DOMAIN`). Point the domain's DNS at the droplet before starting, because IntaSend webhooks need HTTPS. Register `https://<domain>/billing/webhooks/intasend/` in the IntaSend dashboard with the same challenge as `INTASEND_WEBHOOK_SECRET`.
