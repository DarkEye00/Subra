# SUBRA progress

Plan: [deck-polisher-build-plan.md](deck-polisher-build-plan.md). Last updated 24 Sep 2026.

## Done

**Pipeline (`polisher/`)**
- Step 1, validate: extension, size, macros (including a renamed .pptm), password protection, legacy .ppt, zip bombs, and the 50-slide limit. Every error has a customer-facing message.
- Step 2, extract: stable IDs (`s3-t2`); paragraphs with role, level, bullets and runs; tables with merges; chart data read from the chart XML (category, XY and bubble); cropped images with alt text; SmartArt text; notes; hidden slides; heuristic title detection; reading order. Video, audio, OLE objects and other unsupported content are reported, so the deck is rejected before payment.
- Step 3, plan: Claude planner using structured output (JSON schema, which also works on Opus 5.5, where forced tool use is rejected). The system prompt and layout library are cached. Nine layouts. Plans are validated against the hard rules and repaired deterministically where possible, so near-misses don't cost a retry.
- Step 4, render: three themes (Navy Executive, Savanna Warm, Clean Slate) that also rewrite the Office theme colours and fonts, and a 16:9 master. Text is fitted with metric-compatible fonts. Native editable tables and charts, with a clone fallback for combo and unusual charts. Stat highlights, two-column flow for long lists, numbered-list continuity, notes, footers, and hidden flags.
- Step 5, verify: text, numbers, table cells and merges, chart values, images, and notes order, all compared as multisets.
- Step 6, previews: LibreOffice → PDF → PNG, including hidden slides, with bundled fonts.
- Retry once with feedback, escalate once, then fail. API outages defer the job instead of failing it. CLI: `python -m polisher`.

**Web app**
- Email accounts; generic `PriceTier` and `Payment`; `Order` with a logged state machine and a `product_type` field (ready for doc-to-deck); `AIJob`; `DailySpend`.
- M-Pesa (sandbox-ready): STK push, idempotent callbacks behind a secret URL, a status query after 60 seconds and on a sweep, amount check, late-success and duplicate-payment handling (the admin is alerted to refund).
- Celery: processing, a per-attempt AI cost log (KES includes the FX markup), the daily spend cap (queues paid orders and alerts once a day), refund-pending on failure, the free re-run (a failed re-run keeps the first result), and 7-day file deletion (tested on disk).
- Frontend: home with a real before/after slider, upload → quote → M-Pesa → live status (htmx) → preview gallery → download (signed URLs) → re-run; order history; sign-in and sign-up. Checked at 390px and 1280px.
- Admin: orders with AI cost and margin columns, attempts, payments and events inline, and a mark-refunded action; price tiers, themes, and spend.
- Rate limits on uploads and payments. Privacy and terms drafts (shown with a "draft" banner until `LEGAL_PAGES_REVIEWED=1`).
- Docker Compose (web, worker, beat, postgres, redis, caddy) and `.env.example`.

## Needs Joe

- [ ] **Anthropic API key.** No key has been used yet, so the Claude planner has only been tested against fakes. Run the CLI on a few decks as soon as the key exists (`python -m polisher deck.pptx -o out.pptx --report r.json`) and check cost per deck against the appendix.
- [ ] **10 to 20 real decks** for day 2 and day 9 testing. So far everything has been tested on synthetic decks.
- [ ] **Daraja go-live** (paybill or till). The sandbox works through the code paths; production needs Safaricom approval. The fallback aggregator is not built.
- [ ] **Legal:** confirm Anthropic's commercial data terms, review `templates/core/privacy.html` and `terms.html`, register with the ODPC, then set `LEGAL_PAGES_REVIEWED=1`.
- [ ] Droplet, domain, Spaces bucket, and SMTP credentials for alert emails.

## Next

1. Run the Claude planner on real decks, then tune the system prompt and effort using the `AIJob` numbers.
2. Deploy to a droplet with the sandbox Daraja, then run a real end-to-end payment.
3. Test with real decks: SmartArt, master-slide logos, odd charts, and non-English text.

## Known issues and gaps

- Logos and footer art on the original **slide master** aren't carried over, because the new theme replaces the master. Customers may want their logo back; this could be done by copying small master images onto the new master.
- Converting table data into a native chart (allowed by the plan) isn't implemented; tables stay tables.
- Step 7, visual QA with Claude (behind a feature flag, off at launch), isn't built.
- Decks with video, audio, embedded Excel objects, equations, or 3D models are rejected at upload with a clear message, not carried over.
- SmartArt parsing is unit-tested against XML but not yet against a real SmartArt deck.
- Images python-pptx can't re-insert (rare formats) would fail rendering and trigger a retry or refund.
- Text fitting uses metric-compatible fonts. If a customer's machine lacks Cambria (the Navy theme headings), PowerPoint substitutes a font and titles may reflow slightly.
- LibreOffice parses customer files to make previews, which is a known attack surface. It runs in the worker container as a non-root user, with a timeout and a fresh profile per job. Keep the image updated.
