# Build plan: AI presentation polisher (MVP)

> Paste this into Claude Code at the start of the project, or save it in the repo root as `PLAN.md` and tell Claude Code to read it first.
> Target: live by Saturday 3 October 2026.

---

## 1. Your role and how to work

You are building the MVP of **SUBRA**, a web app for Kenyan professionals and businesses. A user uploads a PowerPoint (.pptx) file, pays per deck via M-Pesa, and gets back a visually polished, **fully editable** .pptx that keeps **every piece of original content**.

Working rules:
- Work phase by phase in the order below. Don't start a phase until the previous one runs end to end.
- Commit after every working step, and keep a `PROGRESS.md` with what's done, what's next, and known issues.
- Write tests for the extraction and verification modules. They protect the core promise ("we never lose your data").
- Never hardcode secrets. Everything goes in `.env` (see section 9).
- If a requirement is ambiguous or a phase is running long, stop and ask before expanding scope. Section 11 lists what to cut first.
- This is also the foundation for a future multi-model AI platform (image and video generation via other providers). Keep AI provider calls behind a small provider interface, and keep billing generic (orders and payments, not "deck payments").

---

## 2. Product scope (MVP)

**In scope**
- Upload a .pptx (max 25 MB, max 50 slides)
- Instant quote based on slide count
- Pay per deck via M-Pesa STK Push
- AI redesign using one of 3 built-in themes
- Automatic verification that all text, numbers, tables, and chart data survived
- Preview (slide thumbnails) plus download of the editable .pptx
- One free re-run per paid order ("try a different theme")
- Files auto-deleted after 7 days
- Simple accounts (email and password), order history
- Django admin for orders, payments, AI cost per job, and margins

**Out of scope (later)**
- Business accounts, invoicing, monthly subscriptions
- Customer brand kits (upload your own template)
- Rewriting or summarizing text (the MVP never changes the user's words)
- Word document to presentation (planned as phase 2, see section 13)
- Image and video generation
- WhatsApp or Telegram front ends

---

## 3. Pricing (pay per deck)

Store prices in a database table (`PriceTier`) editable in Django admin, not in code.

| Tier | Slides | Price (KES) |
|---|---|---|
| Small | 1–15 | 200 |
| Standard | 16–30 | 350 |
| Large | 31–50 | 500 |

- Decks over 50 slides are rejected with a friendly message for now.
- Payment happens after the quote and before processing. No AI calls are made on unpaid orders.
- If processing fails after two attempts, mark the order `REFUND_PENDING` for manual M-Pesa refund from admin.

---

## 4. Tech stack

- **Backend:** Django 5, PostgreSQL
- **Jobs:** Celery with Redis (processing runs in the background, never in the request cycle)
- **PPTX handling:** `python-pptx`
- **Rendering previews:** LibreOffice headless (pptx → pdf), then `pdftoppm` (pdf → png)
- **AI:** Anthropic Python SDK (`anthropic`), Claude API
- **Payments:** Safaricom Daraja API (STK Push, callback, transaction status query)
- **Storage:** DigitalOcean Spaces (private bucket, S3-compatible via `django-storages` and `boto3`), signed URLs only
- **Frontend:** Django templates, Tailwind CSS, HTMX for job status polling. Mobile-first: most users will be on phones.
- **Hosting:** one DigitalOcean droplet (4 GB RAM minimum, since LibreOffice is memory-hungry), Docker Compose with web, worker, beat, redis, and postgres services

---

## 5. Data models (starting point)

- `User` (Django auth, email login)
- `PriceTier`: name, min_slides, max_slides, price_kes, active
- `Theme`: name, slug, template_file, preview_image, active
- `Order`: user, original_file, slide_count, tier, price_kes, theme, status, result_file, preview_images, rerun_used, created_at, expires_at
- `Payment`: order, phone, amount_kes, merchant_request_id, checkout_request_id, mpesa_receipt, status, raw_callback (JSON), created_at
- `AIJob`: order, attempt_number, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, cost_kes, duration_ms, verification_passed, error
- `DailySpend`: date, total_cost_usd (for the spend cap)

**Order status flow:**
`UPLOADED → QUOTED → PAYMENT_PENDING → PAID → PROCESSING → DONE`
with side exits `PAYMENT_FAILED`, `FAILED → REFUND_PENDING → REFUNDED`, and `EXPIRED` (files deleted).
Every status change is logged with a timestamp.

---

## 6. The processing pipeline (the core of the product)

Build this first as a standalone Python package (`polisher/`) with a CLI (`python -m polisher input.pptx --theme navy -o output.pptx`) so it can be tested on real decks before any Django or payment code exists.

### Step 1: Validate
- Reject non-.pptx files, macro files (.pptm), password-protected files, and anything over the limits.
- Confirm it's a valid zip or OOXML package before parsing.

### Step 2: Extract
Turn the deck into structured JSON. **Every atomic piece of content gets a stable ID.**
- Text: each paragraph with its ID, text, indent level, and bold/italic runs
- Tables: every cell with row and column position and its value
- Charts: chart type, categories, series names, and series values (read from the chart XML via python-pptx)
- Images: extract binaries and keep them with their IDs
- Speaker notes: keep them verbatim
- Slide titles: detected separately from body text

### Step 3: Plan the redesign (Claude)
Send the extracted JSON (text and structure only, no image binaries) to Claude with:
- A system prompt describing the layout library (below) and strict rules
- **Structured output via tool use**: Claude returns a JSON plan where each slide has a layout name and maps content IDs into that layout's slots

**Hard rules for Claude (put these in the system prompt):**
- Every content ID must appear in the plan exactly once.
- Never rewrite, shorten, translate, or invent text. Words and numbers are copied by ID, not retyped.
- Allowed changes: choosing layouts, grouping and ordering within a slide, choosing emphasis (for example turning a key number into a stat callout), and converting table-shaped data into a native chart **only if** the original table is also kept.
- Slide order stays the same. Splitting an overcrowded slide into two is allowed only if every item still appears once.

**Layout library (start with these 9):**
title, section divider, title + bullets, two column, stat callouts (2–4 big numbers), table, chart, image + text, quote or closing.

Use **prompt caching** on the system prompt and layout library, since they're identical on every call.

### Step 4: Render
Build the new .pptx in code with python-pptx on top of the chosen theme's master template.
- Fixed coordinates per layout per theme
- Auto-fit text: measure with the theme font's metrics (Pillow) and step the font size down within limits. If it still doesn't fit, flag the slide.
- Tables and charts rebuilt as **native, editable** PowerPoint objects using the original data
- Images placed with aspect ratio preserved
- Speaker notes copied across

### Step 5: Verify (no AI, pure code)
Extract the new deck with the same extractor and compare against the original:
- Every text item present with identical normalized text
- The set of all numbers (including percentages and currency) is identical
- Every table cell value is present
- Every chart series has identical values
- Every image is present
- Notes are unchanged

If verification fails: retry the planning step once, passing the list of what failed back to Claude. If it fails again, escalate to the stronger model once. If that fails too, the order goes to `FAILED → REFUND_PENDING`.

### Step 6: Preview and deliver
- Render thumbnails with LibreOffice and pdftoppm (see section 4)
- Store the result and thumbnails in Spaces, and serve through signed URLs that expire after 1 hour
- Show the preview gallery plus a download button

### Step 7: Optional visual QA (behind a feature flag, off at launch)
Send rendered slide images to Claude to check for overlapping or overflowing elements. It adds roughly KES 7–25 per deck, so leave it off until real users show it's needed.

---

## 7. Claude API usage and cost controls

- **Models via env:** `PLANNER_MODEL=claude-sonnet-5`, `ESCALATION_MODEL=claude-opus-5-5`
- Log input, output, and cache tokens from every API response into `AIJob`, and compute cost from a pricing table in settings.
- **Daily spend cap** (`AI_DAILY_CAP_USD`): if reached, new paid orders wait in a queue and admins get an email. Never silently fail a paid order.
- Set a monthly spend limit in the Anthropic Console as a second safety net.
- Timeouts plus retries with backoff on API errors (429 and 5xx), without double-charging the user.

---

## 8. M-Pesa payments (Daraja)

- STK Push initiated after the user confirms the quote and enters a phone number (normalize to the 2547XXXXXXXX format).
- Callback endpoint must be HTTPS, idempotent (the same callback twice changes nothing), and store the raw payload.
- If no callback arrives within 60 seconds, poll the transaction status query API.
- Only a confirmed, matching amount moves the order to `PAID`.
- **Build against the sandbox first.** Production go-live needs Safaricom approval and a paybill or till, which can take days. Start that paperwork on day 1. If it isn't approved in time, have a fallback ready via an aggregator that supports M-Pesa (for example IntaSend, Pesapal, or Paystack).

---

## 9. Environment variables

```
DJANGO_SECRET_KEY=
DATABASE_URL=
REDIS_URL=
ANTHROPIC_API_KEY=
PLANNER_MODEL=claude-sonnet-5
ESCALATION_MODEL=claude-opus-5-5
AI_DAILY_CAP_USD=10
USD_KES_RATE=129.20
SPACES_KEY=
SPACES_SECRET=
SPACES_BUCKET=
SPACES_REGION=
MPESA_ENV=sandbox
MPESA_CONSUMER_KEY=
MPESA_CONSUMER_SECRET=
MPESA_SHORTCODE=
MPESA_PASSKEY=
MPESA_CALLBACK_URL=
ADMIN_ALERT_EMAIL=
FILE_RETENTION_DAYS=7
```

---

## 10. Security and privacy (corporate customers will ask)

- Private storage bucket, signed URLs only, and uploads never publicly listable
- Celery beat job deletes originals, results, and previews after `FILE_RETENTION_DAYS`
- No human reviews files unless the customer asks for support
- Rate limit uploads and payment attempts per user and IP
- Privacy policy and terms pages stating what's stored, for how long, and that content is processed by the Claude API. **Joe to confirm Anthropic's current commercial data terms before publishing these pages.**
- Non-code task for Joe: register with the ODPC as a data controller/processor.

---

## 11. Schedule (Thursday 24 September to Saturday 3 October)

| Day | Date | Goal |
|---|---|---|
| 1 | Thu 24 – Fri 25 Sep | Repo, Docker Compose, Django skeleton, models. **Joe: start Daraja go-live, create Anthropic Console account and key, set spend limit.** |
| 2 | Sat 26 Sep | Validate and extract modules, tested on 10 real decks |
| 3 | Sun 27 Sep | Layout library, first theme, renderer |
| 4 | Mon 28 Sep | Claude planner with structured output, verification, retry and escalation loop. CLI works end to end. |
| 5 | Tue 29 Sep | LibreOffice previews, Celery pipeline, AI cost logging, spend cap |
| 6 | Wed 30 Sep | M-Pesa STK Push, callbacks, status query, order state machine (sandbox) |
| 7 | Thu 1 Oct | Frontend: upload, quote, pay, live status, preview, download, order history, auth |
| 8 | Fri 2 Oct | Deploy to DigitalOcean, HTTPS, production M-Pesa (or fallback), cleanup job, privacy and terms pages, admin |
| 9 | Sat 3 Oct | Run 20 real decks, fix issues, **launch** |

**If behind schedule, cut in this order:**
1. Themes 2 and 3 (launch with one)
2. The free re-run
3. Order history (email the download link instead)
4. Accounts (guest checkout with phone and email)

**Never cut:** verification, the spend cap, file auto-deletion, or payment idempotency.

---

## 12. Definition of done for launch

- A real user can upload, pay with M-Pesa, and download an editable, better-looking deck on a phone in under 5 minutes.
- Verification passes on at least 18 of 20 real test decks. Failures refund cleanly.
- Every order shows its AI cost and margin in admin.
- Files are really deleted after 7 days (tested).
- No secrets in the repo.

---

## 13. Later feature (phase 2): Word document to presentation

**Do not build this before launch.** It's documented here so the MVP code leaves room for it.

**What it does:** the user uploads a Word document (.docx), such as a report, proposal, or strategy paper. The app reads it, pulls together the related information, and produces a complete, polished, editable .pptx using the same themes and layouts as the polisher.

**Key difference from the polisher:** this feature **writes and condenses** content. The polisher never changes words, but here Claude summarizes. The main risk is wrong numbers or invented claims, so the safeguards below are mandatory, not optional.

**Pipeline:**
1. Validate and extract the .docx (headings, paragraphs, tables, images) into structured JSON with IDs, using `python-docx`.
2. **Outline (Claude):** propose a slide outline mapped to document sections.
3. **Slide content (Claude):** write each slide's content, citing the source section IDs it came from. Use prompt caching on the document.
4. **Render:** reuse the polisher's layout library, themes, and renderer unchanged.
5. **Number check (code, free):** every figure on every slide must exist in the source document. Any slide with an unmatched number is blocked and regenerated.
6. **Faithfulness check (Claude):** a second pass flags any claim the document doesn't support. Flagged slides are regenerated or dropped.
7. **Traceability:** speaker notes on each slide cite the document section(s) it came from.

**What to build into the MVP now so this slots in later:**
- Keep the renderer and layout library independent of the pptx extractor (input is a layout plan, not a pptx).
- Keep `Order` generic, with a `product_type` field (`deck_polish`, later `doc_to_deck`), and price tiers per product type.

**Pricing (proposed, to be confirmed):** around $20 (about KES 2,500) per document. Consider two tiers: about KES 1,500 for documents up to 10 pages and about KES 2,500 for up to 40 pages.

**Estimated AI cost** (20-page document, about 20 slides, with buffer): ~KES 80 on Sonnet 5, ~KES 160 on Opus 5.5. Gross margin at KES 2,500: over 90%.

**Scope decision (confirmed):** content comes **only from the uploaded document**. No web research or outside information of any kind. If the document doesn't say it, it doesn't go on a slide.

---

## Appendix: Claude API cost per deck

**Rates** (USD per million tokens, input / output), as of 24 September 2026:
- Claude Haiku 4.5: $1 / $5 (not used for planning; too weak for layout decisions)
- **Claude Sonnet 5: $2 / $10 (default planner)**
- Claude Opus 5.5: $4 / $20 (escalation only)
- Prompt cache reads: 10% of the input rate (5% on Opus 5.5)
- Batch API: 50% off, but results can take hours, so it's not used at launch

**Token estimates per deck:**
- System prompt and layout library: ~6,000 input tokens (cached after the first call)
- Slide content: ~400 input tokens per slide
- Layout plan: ~750 output tokens per slide
- A 30% buffer for the newer tokenizer and a 20% allowance for retries are included below
- USD converted at KES 129.20, plus 3% card forex markup

**Estimated AI cost per deck:**

| Slides | Sonnet 5 | Opus 5.5 (escalation) | + optional visual QA |
|---|---|---|---|
| 10 | ~KES 20 | ~KES 40 | +KES 5 |
| 20 | ~KES 37 | ~KES 74 | +KES 10 |
| 40 | ~KES 71 | ~KES 143 | +KES 19 |
| 50 | ~KES 89 | ~KES 177 | +KES 24 |

**Margin by tier** (worst case for the tier, Sonnet 5, visual QA on, before 2% payment fees):

| Tier | Price | Max AI cost | Gross margin |
|---|---|---|---|
| Small (≤15 slides) | KES 200 | ~KES 36 | ~82% |
| Standard (≤30 slides) | KES 350 | ~KES 70 | ~80% |
| Large (≤50 slides) | KES 500 | ~KES 113 | ~77% |

**Fixed monthly costs (outside the AI bill):** droplet ~$24, Spaces ~$5, domain. That's about KES 4,000 a month. **Break-even is roughly 15–20 decks a month.**

These are estimates. Replace them with real numbers from the `AIJob` table after the first 20 test decks.

---

## Amendment 1 (overrides sections 4, 6, 8, 9 where they conflict)

### A. Payments: IntaSend replaces Daraja for launch
- Use **IntaSend** (M-Pesa STK Push via IntaSend's API and callbacks/webhooks). No own paybill or Daraja go-live is needed.
- Keep payments behind a `PaymentProvider` interface (`initiate(order, phone)`, `handle_callback(payload)`, `check_status(ref)`) with an `IntaSendProvider` implementation. Daraja becomes a future provider class.
- Same rules as before: idempotent callbacks, store raw payloads, poll status if no callback within 60 seconds, and only a confirmed matching amount marks the order `PAID`.
- Rename payment fields to be provider-neutral: `provider`, `provider_ref`, `provider_receipt`.
- IntaSend fees (~4.5% unregistered) are a per-order cost. Log them on `Payment` and include them in the admin margin view.
- Env: remove `MPESA_*`, add `PAYMENT_PROVIDER=intasend`, `INTASEND_PUBLISHABLE_KEY`, `INTASEND_SECRET_KEY`, `INTASEND_WEBHOOK_SECRET`, `INTASEND_ENV=sandbox`.

### B. Rendering: transform in place, don't rebuild from a template
- The renderer works on a **copy of the uploaded file** and modifies it in place. It no longer builds a new deck from a theme template file.
- Themes are applied by rewriting the file's theme color and font scheme, and setting slide backgrounds. A `Theme` stores a color scheme, font pair, and background specs, not a .pptx template.
- Normal slides: remove the original text, table, and chart shapes, and add redesigned ones per the layout plan.
- Embedded parts (media, workbooks, models, images) never leave the package, so no cross-file part copying is needed.

### C. Locked objects
Treat these as **locked**: video, audio, OLE objects (embedded or linked Excel and others), equations (OMML), 3D models, SmartArt, ink, and grouped shapes containing any of these.
- **Extract step:** detect every locked object by inspecting the slide XML directly (python-pptx won't surface all of them, and equations are skipped by text extraction). Record type, slide, shape ID, position, and a SHA-256 hash of each related part.
- **Plan step:** locked objects are passed to Claude as opaque items with ID, type, and size. Claude may only assign them to a layout slot. It cannot remove, split, or resize them beyond the aspect ratio.
- **Render step:** keep the locked shape element untouched (same shape ID, so animations and timing still work). Only its position and size may change, aspect ratio preserved. Redesign the rest of the slide around it.
- **Verify step:** every locked object must exist in the output with identical part hashes. Equations count as content: the number of equations per slide must match. Any mismatch fails verification.
- **Linked objects:** keep the link and flag it in the quote ("this deck links to external files that may not open on other computers").
- **Quote screen:** list the locked objects found, for example "4 slides contain video or embedded objects. These are kept exactly as they are, and the slides are restyled around them."
- **Previews:** LibreOffice may render equations imperfectly and shows 3D models as their fallback image. Note this on the preview page. It doesn't affect the downloaded file.

### D. Limits
- Upload limit: 25 MB normally, **100 MB if the deck contains video or audio**.
- Test set must include at least one deck with each locked object type before launch.

### E. Images (extends sections 6 and Amendment 1)
- **Master and layout images** (logos, decorative graphics on slide masters and layouts): preserve them by default. The theme swap must never remove images from masters or layouts. Record them in the extract step and verify they still exist after rendering.
- **Slide background images:** detect picture backgrounds (`p:bg` with a blip fill). Keep them unless the chosen theme explicitly replaces backgrounds. If replaced, keep the original image on that slide as a regular picture only if it carries content (the planner decides), and log the change.
- **Cropping:** preserve crop settings (`a:srcRect`) exactly. Never reset to the full image.
- **SVG:** keep the SVG part and its PNG fallback together.
- **Alt text:** preserve each picture's description (`descr`) and title. If a shape is recreated, copy them across.
- **Effects:** keep original image effects (recolor, transparency, borders) unless the theme defines a picture style. Never apply filters that alter the image content itself.
- **Images of text:** pass images to the planner as opaque items with size and aspect ratio. The planner may only place and scale them. No OCR in the MVP.
- **Verify step:** every image part (slide, layout, master, background) is present with an identical SHA-256 hash, and crop and alt text are unchanged.
- **Test set:** include a deck with a logo on the master, a cropped photo, a picture background, and SVG icons.
