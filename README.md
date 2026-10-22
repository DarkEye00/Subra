# SUBRA

Upload a PowerPoint, see it redesigned, then pay per deck with M-Pesa to get back a polished, fully editable .pptx that keeps every piece of the original content. The full plan is in [deck-polisher-build-plan.md](deck-polisher-build-plan.md); status is in [PROGRESS.md](PROGRESS.md).

## How it fits together

- `polisher/` is the core pipeline, a standalone package with no Django dependency: validate → extract → classify (a cheap model labels each slide) → plan (Claude picks one of 12 layouts per slide) → render (in place, on a copy of the customer's file) → verify (every word, number, table cell and chart value) → check (overlap, overflow, text below 12pt, contrast) → repair (faulted slides go back to the planner, twice at most, then get the plainest layout and a flag) → preview. Vision QA, where a model scores each rendered slide, sits behind `VISION_QA`. Video, audio, embedded objects, equations, 3D models, SmartArt and ink are kept exactly and only moved or scaled.
- `accounts/`, `billing/`, `orders/`, and `core/` make up the Django app: email login, price tiers and M-Pesa payments via Paystack (behind a provider interface; IntaSend and Daraja are alternatives), orders with a logged processing lifecycle and a separate paid flag, Celery tasks, the admin, and privacy/terms pages.
- `templates/` and `static/` hold the mobile-first frontend (Tailwind v4, htmx).

## Polish a deck from the command line

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...            # not needed with --planner heuristic
.venv/bin/python -m polisher input.pptx --theme navy -o output.pptx --preview previews/ --report report.json
```

Themes: `navy`, `savanna`, `slate`. `--planner heuristic` uses the free rule-based planner (no API calls), which is useful for testing rendering. `--classifier off` skips the slide classifier, `--repair-passes N` caps how often a faulted slide goes back to the planner (default 2), and `--vision-qa` (with `--preview DIR`) scores every rendered slide with a vision model. `--extract-only` prints the extracted content as JSON.

Each run prints every stage (classify, plan, repair, vision) with its tokens, cost and result, a QA line (slides repaired, fallbacks, flagged) and the time per stage. `--report` writes all of that as JSON, including the QA report the order page shows.

## Layout sample page and eval set

```bash
.venv/bin/python scripts/render_layouts.py --out out/layouts   # every layout in every theme, then open out/layouts/index.html
.venv/bin/python scripts/eval_decks.py test-decks               # polish a folder of real decks, write eval/<timestamp>.json
.venv/bin/python scripts/eval_decks.py test-decks --planner claude --baseline eval/<earlier>.json
```

The sample page is the place to look after changing a layout or theme; it exits 1 if any slide fails the QA checks. The eval runner tracks retention, cost, time, fallback and flagged rates per deck (and human scores from `test-decks/scores.json`), and with `--baseline` fails on any regression. Re-run it after every meaningful change; don't ship a change that lowers any of those numbers.

## Run the web app locally

Needs LibreOffice (`soffice`) and `pdftoppm` for previews.

```bash
npm install && npm run build:css
export DJANGO_DEBUG=1 POLISHER_PLANNER=heuristic   # or set ANTHROPIC_API_KEY and leave POLISHER_PLANNER unset
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Without `REDIS_URL`, Celery tasks run inline. Without payment keys, DEBUG mode shows a "simulate a successful payment" button on a ready deck, so you can try the whole flow.

## Tests

```bash
.venv/bin/pytest
```

`scripts/record_paystack_fixtures.py` re-records the Paystack test-mode fixtures (test keys only).

The tests cover validation (including the 25/100 MB limits), extraction of every locked object type and image case, verification, planning and repair, in-place rendering round trips for every layout and theme (12 layouts, no text below 12pt, no QA faults on the sample page), the deterministic QA checks and theme contrast, the repair loop and fallbacks, the classifier and vision stages against fake providers, the retry and escalation pipeline, payments (Paystack and IntaSend webhooks, signatures, idempotency, amount mismatches, late and duplicate payments, the Starter-limit alerts), generation before payment, theme switching from the saved plan, order processing and the QA report, the spend cap, file deletion, and the views.

## Deploy (one DigitalOcean droplet, 4 GB RAM or more)

```bash
cp .env.example .env    # fill it in
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Compose runs `web` (gunicorn), `worker` (Celery; runs LibreOffice), `beat` (file deletion, queue release, payment sweeps), `postgres`, `redis`, and `caddy` (automatic HTTPS for `DOMAIN`). Point the domain's DNS at the droplet before starting, because Paystack webhooks need HTTPS. In the Paystack dashboard, set the webhook URL to `https://<domain>/billing/webhooks/paystack/`. Webhooks are verified with `PAYSTACK_SECRET_KEY`, so there's no separate secret.
