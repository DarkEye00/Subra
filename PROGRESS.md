# SUBRA progress

Plan: [deck-polisher-build-plan.md](deck-polisher-build-plan.md). Last updated 6 Oct 2026.

## Done

**Amendment 2 (added 5 Oct 2026)**
- **Pay after generation:** customers polish first, look through every slide, then pay with M-Pesa to download. Failed decks cost them nothing. `Order.status` covers processing only; payment is `paid_at` plus the `Payment` records. One unpaid deck per account. Unpaid and failed decks are deleted after 48 hours, paid ones 7 days after payment, and customers can discard an unpaid deck.
- **Free theme switching:** the verified plan is saved, so a switch re-renders it with no Claude call (verified again, rate-limited to 10 an hour). A failed or stuck switch leaves the previous deck in place.
- **Paystack (Starter Business)** is the default provider: M-Pesa charge API, HMAC-SHA512 webhooks, status checks, coded against recorded test-mode payloads (`+254…` phone format, receipt in `receipt_number`, fees in cents). Admin alerts at 80% and 95% of the KES 250,000 Starter limit. IntaSend and Daraja remain as alternatives.
- Admin: paid column and filter, payments can be marked refunded, failed generations are emailed, and the Daily spend page shows this month's AI cost on paid vs unpaid decks and the share paid for.
- Checked in a browser at 390px and 1280px with real LibreOffice previews: quote, polish, preview, theme switch, the one-unpaid-deck block, simulated payment, download, discard.

**Amendment 1 (added 24 Sep 2026)**
- A. **Payments via IntaSend** behind a `PaymentProvider` interface (`initiate`, `handle_callback`, `check_status`). Webhooks are authenticated by IntaSend's `challenge` and our `api_ref`. Idempotent, every raw payload stored, status polled after 60 seconds, and only a matching amount marks an order paid. Fields are provider-neutral (`provider`, `provider_ref`, `provider_receipt`). IntaSend's fee is logged per payment (estimated at 4.5% if it isn't reported) and subtracted in the admin margin. The Daraja client is kept as a future `DarajaProvider`. The API shape was checked against IntaSend's official SDK and docs; it hasn't been run against the sandbox yet (no keys).
- B. **In-place rendering:** the renderer transforms a copy of the customer's file. It rewrites the theme's colours and fonts on every master, sets backgrounds, removes the original text, table and chart shapes, and draws redesigned ones. It keeps the original slide size (4:3 stays 4:3), masters, layouts, notes and footers.
- C. **Locked objects:** video, audio, embedded and linked OLE, equations, 3D models, SmartArt, ink, and groups holding them. They're detected from the raw XML, with SHA-256 hashes of every related part and an XML signature. They're planned as opaque visuals on their slide's first part, and only moved and scaled, with aspect preserved and the same shape ID, so animations keep working. Animations on removed shapes are pruned. Verification checks hashes, XML, shape ID and slide, and equation counts. The quote screen lists locked objects and warns about linked files, and the preview page notes equation and 3D model rendering.
- D. **Upload limit:** 25 MB, or 100 MB for decks with video or audio (detected from content types).
- E. **Images:** original picture elements are kept, so crop, alt text, title, SVG plus PNG fallback, and effects survive. Master and layout images are recorded and verified, and titles and decoration avoid logos that show through. Picture backgrounds are kept as pictures when the planner says they carry content, otherwise replaced and logged.
- Test decks with every locked type and every image case are in `tests/lockedbuilder.py`. They're synthetic, written as raw XML, so real PowerPoint files are still needed.

**Pipeline (`polisher/`)**
- Step 1, validate: extension, size, macros (including a renamed .pptm), password protection, legacy .ppt, zip bombs, and the 50-slide limit. Every error has a customer-facing message.
- Step 2, extract: stable IDs (`s3-t2`); paragraphs with role, level, bullets and runs; tables with merges; chart data read from the chart XML (category, XY and bubble); images; locked objects; backgrounds; master images; notes; hidden slides; heuristic title detection; reading order.
- Step 3, plan: Claude planner using structured output (JSON schema, which also works on Opus 5.5, where forced tool use is rejected). The system prompt and layout library are cached. Nine layouts. Plans are validated against the hard rules and repaired deterministically where possible, so near-misses don't cost a retry.
- Step 4, render (in place, see above): three themes (Navy Executive, Savanna Warm, Clean Slate). Text is fitted with metric-compatible fonts. Native editable tables and charts; charts python-pptx can't rebuild keep their original element. Stat highlights, two-column flow for long lists, and numbered-list continuity.
- Step 5, verify: text, numbers, table cells and merges, chart values, images, and notes order, all compared as multisets.
- Step 6, previews: LibreOffice → PDF → PNG, including hidden slides, with bundled fonts.
- Retry once with feedback, escalate once, then fail. API outages defer the job instead of failing it. CLI: `python -m polisher`.

**Web app**
- Email accounts; generic `PriceTier` and `Payment`; `Order` with a logged state machine and a `product_type` field (ready for doc-to-deck); `AIJob`; `DailySpend`.
- M-Pesa via Paystack (Amendment 2; IntaSend from Amendment 1A is kept as an alternative): STK push, idempotent webhooks, a status check after 60 seconds and on a sweep, amount check, late-success and duplicate-payment handling (the admin is alerted to refund).
- Celery: processing, a per-attempt AI cost log (KES includes the FX markup), the daily spend cap (keeps orders queued and alerts once a day), a failure alert, free theme switches from the saved plan, and file deletion (48 hours unpaid, 7 days after payment; tested on disk).
- Frontend: home with a real before/after slider, upload → quote → polish → live status (htmx) → preview gallery and theme switching → M-Pesa → download (signed URLs), or discard; order history; sign-in and sign-up. Checked at 390px and 1280px.
- Admin: orders with AI cost and margin columns, attempts, payments and events inline, and a mark-refunded action; price tiers, themes, and spend.
- Rate limits on uploads and payments. Privacy and terms drafts (shown with a "draft" banner until `LEGAL_PAGES_REVIEWED=1`).
- Docker Compose (web, worker, beat, postgres, redis, caddy) and `.env.example`. The image builds, and the polisher (including LibreOffice previews) runs verified inside it.

## Needs Joe

- [ ] **Anthropic API key.** No key has been used yet, so the Claude planner has only been tested against fakes. Run the CLI on a few decks as soon as the key exists (`python -m polisher deck.pptx -o out.pptx --report r.json`) and check cost per deck against the appendix.
- [ ] **10 to 20 real decks** for day 2 and day 9 testing. So far everything has been tested on synthetic decks. Amendment 1D asks for at least one real deck per locked object type (video, audio, embedded and linked Excel, equation, 3D model, SmartArt, ink, group with media), plus one with a logo on the master, a cropped photo, a picture background, and SVG icons.
- [ ] **Paystack:** submit Starter KYC for live keys, set the webhook URL `https://<domain>/billing/webhooks/paystack/` in the dashboard, and put the live key in `PAYSTACK_SECRET_KEY` on the server. Register the business before collections reach KES 250,000 (the 80% alert is the reminder), then set `PAYSTACK_STARTER_LIMIT_KES=0`.
- [ ] **Legal:** confirm Anthropic's commercial data terms, review `templates/core/privacy.html` and `terms.html`, register with the ODPC, then set `LEGAL_PAGES_REVIEWED=1`.
- [ ] Droplet, domain, Spaces bucket, and SMTP credentials for alert emails.

## Next

1. Run the Claude planner on real decks, then tune the system prompt and effort using the `AIJob` numbers.
2. Deploy to a droplet with Paystack test keys, then run a real end-to-end payment.
3. Test with real decks: SmartArt, master-slide logos, odd charts, and non-English text.

## Known issues and gaps

- Locked objects are kept exactly, so their own fonts and colours don't follow the new theme. That's by design, but it can look mixed on a slide.
- Groups that contain pictures but no locked objects are dissolved: pictures are lifted out and text is restyled. A group used as a diagram (shapes and arrows) loses its shapes, though its text is kept.
- Original decorative shapes without text (lines, bars) are removed with the old design. Shapes on masters and layouts stay, recoloured by the new theme, and may clash with some themes.
- Animations on text that was redrawn are dropped (the shapes are new). Animations on kept objects stay.
- Converting table data into a native chart (allowed by the plan) isn't implemented.
- Step 7, visual QA with Claude (behind a feature flag, off at launch), isn't built.
- In our synthetic fixtures, LibreOffice previews don't show SmartArt without its cached drawing part, or equation and ink fallbacks. Real PowerPoint files include these, but check previews on real decks.
- Text fitting uses metric-compatible fonts. If a customer's machine lacks Cambria (the Navy theme headings), PowerPoint substitutes a font and titles may reflow slightly.
- LibreOffice parses customer files to make previews, which is a known attack surface. It runs in the worker container as a non-root user, with a timeout and a fresh profile per job. Keep the image updated.
- Every generation now costs AI money whether or not it's paid for. Watch the paid share on the Daily spend admin page; if it's low, the levers are a lifetime cap on unpaid generations, email verification, or a deposit.
- The Paystack provider was built from test-mode payloads. Check the first live `charge.success` webhooks (a payment's raw callbacks in admin) for the M-Pesa receipt field.
