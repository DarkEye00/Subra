# Pay after generation, and Paystack instead of IntaSend

Date: 5 October 2026. Status: approved in conversation, waiting for spec review.
Build plan entry: Amendment 2 in [deck-polisher-build-plan.md](../../../deck-polisher-build-plan.md).

## Why

- **IntaSend needs a registered business.** Joe doesn't have one yet, so IntaSend blocks launch. Paystack's Starter Business account accepts unregistered merchants up to KES 250,000 in lifetime collections, and charges 1.5% on M-Pesa (IntaSend: ~4.5%).
- **Customers should see the result before they pay.** Paying after generation builds trust, and a deck that fails verification costs the customer nothing, so refunds almost disappear.

## Decisions

| Question | Decision |
|---|---|
| What the customer sees before paying | Every slide's preview image, as today. Only the editable .pptx download is locked. |
| Abuse limit | One unpaid deck per account at a time. The daily AI spend cap stays as the backstop. |
| Themes | Switch freely between the three themes, before and after paying, re-rendered from the saved plan with no AI call. The free re-run is removed. |
| State model | Two separate tracks: `status` for processing, `paid_at` for payment. |
| Failure alerts | A failed generation still emails the admins, even though no refund is needed. |
| Provider | Paystack (Starter Business). IntaSend and Daraja stay in the code as alternatives. |
| Pricing | Unchanged: per deck, by slide count. No subscriptions. |

This overrides the build plan where they conflict: section 2 (free re-run), section 3 ("payment happens before processing", "no AI calls on unpaid orders", refund on failure), section 8, Amendment 1A (IntaSend), and the section 12 line "Failures refund cleanly", which becomes "Failures cost the customer nothing". The "never cut" list still holds: verification, the spend cap, file auto-deletion and payment idempotency are all kept.

## 1. Order lifecycle

### Processing status (`Order.status`)

| Status | Label shown | Meaning |
|---|---|---|
| `quoted` | Ready to polish | Uploaded, validated, priced. Customer picks a starting theme. No AI has run. |
| `queued` | In queue | Waiting for a worker, the spend cap to reset, or the AI provider to recover. |
| `processing` | Polishing | Plan, render, verify, previews. |
| `ready` | Ready | Result and previews exist. |
| `rerendering` | Switching theme | Re-rendering the saved plan in another theme. |
| `failed` | Couldn't polish | Generation failed after retries and escalation. |
| `expired` | Expired (files deleted) | Files are gone, by retention or by discard. |

Allowed transitions:

| From | To |
|---|---|
| `quoted` | `queued`, `expired` |
| `queued` | `processing` |
| `processing` | `ready`, `failed`, `queued` (spend cap reached mid-job, or AI outage) |
| `ready` | `rerendering`, `expired` |
| `rerendering` | `ready` |
| `failed` | `expired` |
| `expired` | (none) |

Removed statuses: `uploaded`, `payment_pending`, `payment_failed`, `paid`, `refund_pending`, `refunded`. Removed fields and code: `Order.rerun_used`, `Order.can_rerun`, `services.start_rerun`, the `rerun` view and URL, and the run-2 branch in `processing.fail`.

### Payment track

- New field `Order.paid_at` (nullable datetime). Billing sets it when a payment of the exact amount is confirmed. `Order.is_paid` becomes `paid_at is not None`.
- Payment progress ("check your phone", "you cancelled the prompt") is read from the order's latest `Payment`. A failed payment no longer changes `Order.status`.
- Because `OrderEvent` logs status changes, a payment is logged as an event with the same from and to status and a note such as "Paid, M-Pesa receipt QJK…".
- **A customer can start a payment only when:** `status == ready`, `paid_at` is empty, no payment is pending, and files exist.

### Confirming a payment (`billing.services._mark_paid`)

| Order state when a matching payment is confirmed | Result |
|---|---|
| `ready` or `rerendering`, unpaid, files exist | Set `paid_at`, set `expires_at` to now + `FILE_RETENTION_DAYS`, log the event. |
| Already paid | Duplicate: alert admins to refund (as today). |
| `expired`, `failed`, or files deleted | Alert admins to refund: "paid for a deck that no longer exists". |

`_mark_failed` updates only the `Payment`.

### One unpaid deck per account

- An order is **unpaid work** when its status is `queued`, `processing`, `ready` or `rerendering` and `paid_at` is empty.
- Clicking "Polish my deck" (`quoted` → `queued`) is refused while the account has other unpaid work. The message is "Pay for or discard <original name> first", linking to that order.
- `quoted` orders don't count, because no AI has run on them.
- The check and the transition happen in one transaction with the user's orders locked (`select_for_update`), so two quick clicks can't start two decks.

### Discard

- "Discard this deck" is offered on `quoted`, `failed`, and unpaid `ready` orders with no payment pending.
- It deletes the files immediately and moves the order to `expired` with the note "Discarded by customer".
- Paid orders can't be discarded. Their files expire on schedule.

### File retention

| When | `expires_at` |
|---|---|
| Order created (`quoted`) | now + `UNPAID_RETENTION_HOURS` (48) |
| Generation finishes (`ready`, unpaid) or fails (`failed`) | now + `UNPAID_RETENTION_HOURS` |
| Payment confirmed | now + `FILE_RETENTION_DAYS` (7 days) |
| Theme switch | unchanged |

`delete_expired_files` skips orders that are `queued`, `processing` or `rerendering`, and orders with a pending payment. A pending payment is given up on after 30 minutes by the existing sweep, so expiry is delayed by at most that long. A late confirmation after expiry triggers the refund alert above.

### Migration of existing rows

There are no real customers yet, so a data migration maps statuses directly:
- `uploaded`, `quoted`, `payment_pending`, `payment_failed` → `quoted`
- `paid`, `processing` → `queued`
- `done` → `ready`
- `failed`, `refund_pending`, `refunded` → `failed`
- `expired` → `expired`

`paid_at` is filled in from the earliest paid `Payment`. Payments on orders that were `refunded` get the new `Payment` status `refunded`.

## 2. Processing and theme switching

### Generation

- "Polish my deck" moves the order from `quoted` to `queued` and enqueues `process_order`, which now picks up `queued` orders instead of `paid` ones. `release_queued_orders` does the same.
- The spend cap, the retry and escalation schedule, the outage handling (back to `queued` with `waiting_for_capacity`) and the per-attempt AI cost log are unchanged.
- `PolishResult` gains `plan`, the winning attempt's plan dict, which is already recorded on `AttemptRecord.plan`. On success it is stored in a new `Order.plan` JSON field, next to the result file and previews, and the order moves to `ready`.
- On failure the order moves to `failed`, with `failure_reason` set and `expires_at` set as above. Admins get an email with the order, the customer's email, the reason, the number of attempts and the AI cost in KES. No refund state, because nothing was paid.

### Theme switching

- **Before generation (`quoted`):** the theme picker just sets `Order.theme`, as today.
- **After generation:** `POST /orders/<id>/theme/` with a theme slug. It is accepted only when:
  - the status is `ready`
  - the theme is active and differs from the current one
  - files exist and no payment is pending
  - the account hasn't used up `THEME_SWITCH_RATE_LIMIT` (10 per hour)
- The order moves to `rerendering` and a new Celery task, `rerender_order(order_id, theme_slug)`, runs.
- **New pipeline function:** `polisher.pipeline.rerender(input_path, output_path, theme, plan_dict, render_fn=render)`. It validates and extracts the original, loads the plan with `parse_plan`, checks it with `validate_plan`, renders, and runs `verify`. It makes no planner call and does no retry, and it returns a result with `ok`, `render_report`, `verification` and `failure`. Verification still runs because a different font can reflow text.
- **On success:** previews are rendered, the result and previews are replaced (old files deleted), the theme is set, and the order returns to `ready`.
- **On failure:** the order returns to `ready` with the previous theme and result untouched. A new field, `Order.switch_error`, holds the message ("We couldn't switch to Savanna Warm; your deck is unchanged"). It shows on the order page and is cleared by the next successful switch.
- **Stuck jobs:** `release_queued_orders` also moves orders stuck in `rerendering` for more than 10 minutes back to `ready`, with a timeout `switch_error`.
- Re-renders use no AI, so they don't check the spend cap and don't create `AIJob` rows.
- Only the current theme's result is stored. Switching back re-renders.

## 3. Payments: Paystack

### Provider (`billing/providers/paystack.py`, `PaystackProvider`, `name = "paystack"`)

The base URL is `https://api.paystack.co` and authentication is `Authorization: Bearer <PAYSTACK_SECRET_KEY>`. Test or live mode follows the key (`sk_test_…` or `sk_live_…`), so there is no environment setting.

- **`initiate(order, phone)`**
  - Sends `POST /charge` with `email` (the account email), `amount` (`price_kes × 100`), `currency: "KES"`, `reference` (`subra-<order pk>-<8 hex>`), `mobile_money: {phone, provider: "mpesa"}`, and `metadata: {order: <public_id>}`.
  - `pay_offline` or `pending` means the prompt was sent.
  - An immediate failure raises `ProviderError`, and the customer sees the existing "couldn't reach M-Pesa, try again" message. The phone number has already been checked by `normalize_phone`.
  - Returns `InitResult(provider_ref=reference, reference=reference, raw=response)`. Paystack identifies transactions by our reference, so it serves as both.
- **`check_status(ref)`**
  - Sends `GET /transaction/verify/<ref>`.
  - `success` → paid. `failed`, `abandoned` or `reversed` → failed. Anything else → pending.
  - `amount` and `fees` are divided by 100.
- **`handle_callback(body, headers)`**
  - Computes the HMAC-SHA512 of the raw body with the secret key and compares it to `X-Paystack-Signature` with `hmac.compare_digest`. A mismatch raises `CallbackRejected`.
  - `charge.success` → a paid event.
  - Other events → a pending event, which `apply_event` ignores.
- **Phone format:** `normalize_phone` keeps returning `2547XXXXXXXX`, and the provider converts it to whatever Paystack expects.
- **Receipt:** the M-Pesa receipt if the payload carries one, otherwise blank.

### Interface change

`PaymentProvider.handle_callback` takes `(body: bytes, headers)` instead of a parsed dict, because Paystack signs the raw bytes. Each provider parses its own JSON and raises `ValueError` for a malformed body.
- `IntaSendProvider` is updated to match. Its challenge check is unchanged.
- The `daraja` provider class is updated to match.
- One view, `billing/webhooks/<provider>/`, handles all providers: 400 on `ValueError`, 403 on `CallbackRejected`, 200 otherwise. The existing IntaSend URL keeps working through the same route.
- Everything in `billing.services` is unchanged apart from `_mark_paid`, `_mark_failed` and the payable check described in section 1. That includes idempotency, raw payload storage, the 60-second status check and the sweep, the exact-amount rule, and the partial-payment and mismatch alerts.

### Starter Business limit

- `PAYSTACK_STARTER_LIMIT_KES` defaults to 250000, and 0 turns the check off.
- After each confirmed Paystack payment, the lifetime total of Paystack payments that were ever confirmed (status `paid` or `refunded`) is compared before and after. Crossing 80% or 95% of the limit sends one admin email telling Joe to register the business before payments stop.
- The check needs no stored state: refunds don't lower the total, so each threshold is crossed only once.

### Other provider changes

- `PaymentProvider` gains a `configured` property (keys present). The development "fake payment" button works when `DEBUG` is on and the active provider isn't configured, replacing the IntaSend-specific check.
- The privacy page lists Paystack instead of IntaSend. Paystack receives the phone number, the account email and the amount. Sending the email is new, because the charge API requires it.

### To confirm in Paystack test mode, before the provider is finished

These come from secondary sources. Record real test-mode payloads as test fixtures and code against those:
1. The phone format `mobile_money.phone` accepts (`07…`, `2547…` or `+2547…`).
2. Which field of `charge.success` and of the verify response holds the M-Pesa receipt number.
3. What `fees` contains for M-Pesa charges.

## 4. Screens

Order page (`templates/orders/detail.html`), by state:

| State | Shows |
|---|---|
| `quoted` | Slide count, locked-object notes, price with "You pay only after you see your deck", theme picker, **Polish my deck**, **Discard**. If other unpaid work exists, the refusal message with a link to it. |
| `queued`, `processing` | Live status, as today. |
| `ready`, unpaid | Preview gallery, theme buttons, M-Pesa number field, **Pay KES <price> to download**, "Unpaid decks are deleted after 48 hours" with the time left, **Discard this deck**. Theme buttons are disabled while a payment is pending, and a pending payment shows "Check your phone". |
| `ready`, paid | **Download**, preview gallery, theme buttons, "Files are deleted on <date>". |
| `rerendering` | "Switching to <theme>…" with live status. |
| `failed` | "We couldn't polish this deck. You haven't been charged." |
| `expired` | As today. |

- **Live status:** the HTMX status check compares a combined key `<status>:<paid>:<latest payment status>` instead of the status alone, so the page refreshes when either track changes. Polling runs while the status is `queued`, `processing` or `rerendering`, or while a payment is pending.
- **Order history:** a paid or unpaid badge next to the status.
- **Download:** requires `status == ready` and `paid_at`.

### Wording

- **Home page:**
  - "From KES 200 per deck. Pay with M-Pesa" becomes "Pay with M-Pesa only after you see your redesigned deck".
  - "Includes one free re-run in a different theme" becomes "Switch between themes as often as you like".
  - The refund sentence becomes "If it still can't, you don't pay".
- **Terms:**
  - Remove the free re-run clause.
  - "Prices and payment": you see your redesigned deck before you pay, and payment unlocks the download.
  - "Refunds": covers duplicate or mistaken payments and complaints within the retention period. There's no automatic refund case any more, because failed decks aren't charged.
- **Privacy:** Paystack replaces IntaSend, as described in section 3.

## 5. Admin and alerts

- **Orders:**
  - A "Paid" column (from `paid_at`).
  - Filters for status and paid or unpaid.
  - Margin shown only for paid orders ("—" otherwise).
  - The `mark_refunded` action moves to payments.
- **Payments:** new status `refunded`, a `refunded_at` field, and a "Mark refunded" action for paid payments.
- **Daily spend:** the changelist shows a line for the current month:
  - AI cost (KES) on orders that were paid, and on orders that weren't, from `AIJob.cost_kes` for jobs created this month.
  - The share of orders that reached `ready` this month that are now paid.
- **Alert emails:**
  - **New:** a failed generation, the Paystack 80% and 95% limit warnings, and a payment confirmed for a deck that no longer exists.
  - **Unchanged:** the spend cap, a duplicate payment, an amount mismatch, and a partial payment.

## 6. Settings

| Setting | Value |
|---|---|
| `PAYMENT_PROVIDER` | default `paystack` |
| `PAYSTACK_SECRET_KEY` | new, empty by default |
| `PAYSTACK_STARTER_LIMIT_KES` | new, default `250000` |
| `PAYMENT_FEE_RATE` | default `0.015` (was `0.045`) |
| `UNPAID_RETENTION_HOURS` | new, default `48` |
| `THEME_SWITCH_RATE_LIMIT` | new, `(10, 3600)` |
| `INTASEND_*` | kept, used only when `PAYMENT_PROVIDER=intasend` |

Update `.env.example` and the README to match.

## 7. Testing

Write each test first, in the style of the existing suite.

- **Order lifecycle:**
  - Every allowed transition succeeds and every other one raises `InvalidTransition`.
  - The one-unpaid-deck rule: refused with other unpaid work, allowed after paying or discarding, and `quoted` orders don't count.
  - Discard deletes files and is refused on paid orders and with a payment pending.
- **Retention:**
  - 48 hours for unpaid and failed orders, 7 days from payment for paid ones.
  - The sweep skips active statuses and orders with a pending payment.
- **Payments:**
  - Allowed only when `ready`, unpaid, with no payment pending.
  - Confirmation sets `paid_at` and extends retention.
  - A duplicate payment triggers the refund alert, as does a payment for an expired or failed order.
  - A failed payment leaves `status` unchanged.
- **Download:** a 404 until paid.
- **Processing:**
  - Success stores `plan` and lands on `ready`.
  - Failure lands on `failed`, sends the alert and sets 48-hour retention.
  - Spend cap and outage requeue to `queued`.
- **Theme switching:**
  - Success replaces the files and the theme.
  - A verification failure or crash keeps the old result and sets `switch_error`.
  - Refused while a payment is pending, when the theme is the same, and when the rate limit is hit.
  - Stuck `rerendering` orders are released.
  - `pipeline.rerender` makes no planner call.
- **Paystack provider** (against recorded test-mode payloads):
  - Correct charge request body, including the amount in cents.
  - Responses mapped to pending, paid and failed.
  - A valid signature is accepted, and a bad or missing one is rejected.
  - Malformed JSON is rejected.
- **Webhook view:** 200, 400 and 403 for each provider.
- **Starter limit alerts:** each threshold fires once, and nothing fires when the limit is 0.
- **Spend page summary:** paid and unpaid totals, and the paid share.
- **Existing suite:** the 291 tests are updated where they assume paying first, and the full suite passes.
- **In the browser:** run the app with `POLISHER_PLANNER=heuristic` and the development payment button. Go through quote, polish, preview, theme switch, pay, download and discard at 390px and 1280px.

## 8. Not in this change

- **"Describe your deck" (Amendment 3, designed next):**
  - Customers describe the look (colours, fonts, mood) and the layout style ("more charts, fewer bullets").
  - Claude plans with those instructions. The customer's words are never changed, so verification still applies.
  - Each new description is a new plan and counts toward the unpaid limit and the spend cap.
- Subscriptions, recurring payments and M-Pesa Ratiba.
- Email verification and lifetime caps on unpaid generations. Revisit if the spend page shows abuse.
- Keeping a rendered file per theme.
- Moving to Daraja once the business is registered.

## 9. Economics to watch

Every generation now costs AI money whether or not it is paid for. At 1 in 3 decks paid, a 20-slide deck's AI cost per sale rises from about KES 37 to about KES 111 on a KES 350 price. Watch the spend page's paid share in the first weeks. If it's low, the levers are a lifetime cap on unpaid generations, email verification, or a small deposit.

## 10. Joe's actions

1. Create a Paystack Kenya account as a Starter Business and copy the **test** secret key into `.env`.
2. Set the webhook URL `https://<domain>/billing/webhooks/paystack/` in the Paystack dashboard.
3. Submit Starter KYC for live keys.
4. Register the business before collections reach KES 250,000. The 80% alert is the reminder.
