# Subscriptions with credits, and company accounts

Date: 27 October 2026. Status: **approved for build** (revision 2). The product decisions were made with Joe on 23 October. Revision 2, on 26 October, follows Joe's instruction that "payments should be handled by Paystack": monthly billing now runs on **Paystack Plans and Subscriptions** instead of a SUBRA renewal scheduler. Joe asked for the recommended answer wherever his input would otherwise be needed; those answers are marked **(recommended, decided)** and are easy to reverse.

Build plan entry: [docs/superpowers/plans/2026-10-27-subscriptions-and-credits.md](../plans/2026-10-27-subscriptions-and-credits.md). This reverses "Business accounts, invoicing, monthly subscriptions" in the out-of-scope list of [deck-polisher-build-plan.md](../../../deck-polisher-build-plan.md) and "No subscriptions" in the [Amendment 2 spec](2026-10-05-pay-after-generation-paystack-design.md).

## Why

- Companies and regular users want to pay monthly by card instead of per deck by M-Pesa; some hold USD cards.
- The Paystack account is a Kenyan Starter Business and bills in KES. A USD card is charged the KES amount; the card network converts and the customer's bank adds its own foreign-transaction fee. SUBRA receives the KES amount less Paystack's fee.
- M-Pesa can't be charged automatically each month, so subscriptions are card-only. Pay per deck with M-Pesa stays for everyone.

## Decisions

| Question | Decision |
|---|---|
| Currency | KES only. |
| Who runs billing | **Paystack.** Each SUBRA plan is a Paystack Plan; Paystack stores the card, charges it monthly, and hosts the page where the customer updates the card. SUBRA reacts to Paystack's webhooks and re-reads the subscription from Paystack's API. |
| Pay per deck | Unchanged, for everyone, including members of a subscribed company. |
| What a subscription gives | Credits each month. No cap on decks, only on credits. |
| Credit cost of a deck | By slide-count tier, known before generating: Small 4, Standard 7, Large 10 (1 credit ≈ KES 50). |
| When credits are taken | When the deck is downloaded (generate first, pay to download, as today). |
| Running out | Buy a top-up pack (card or M-Pesa, on Paystack's page), or pay per deck with M-Pesa. |
| Plans | Solo KES 2,000 / 40 credits, Team KES 5,000 / 115, Business KES 12,000 / 300. Placeholders until real AI costs exist. |
| Unused credits | Carry over while the subscription is live; expire when it ends. A monthly grant never lifts the balance above two months of the plan's credits. |
| Failed renewal | 7 days of grace to fix the card; credits stay spendable meanwhile. |
| Company accounts | One owner, invited members with their own logins, one shared balance. The owner sees usage per member, can download members' decks, removes members, and manages the plan. |
| Individuals | A company with one member. |
| Not in v1 | Per-member limits, more than one admin, proration, billing in USD. |

### Answers to the open questions (recommended, decided)

1. **Retention:** a credit-paid deck is kept 7 days after download is unlocked, like an M-Pesa deck. Every deck row shows "available until".
2. **Top-up credits** share the balance and its rules: they expire when the subscription ends. The top-up button and the terms say so.
3. **One unpaid deck per person** stays for subscribers. It's the only per-customer brake on AI spend.
4. **Cancellation:** no further charge; credits stay until the end of the paid period, then expire. The owner can resume before then.
5. **The cap:** a grant is `max(0, min(plan credits, 2 × plan credits − balance))`. Nothing already in the balance is ever destroyed by the cap.
6. **Numbers:** the placeholders above, editable in admin. Re-derive them from `AIJob` once real decks have run.
7. **Top-ups:** owner only.
8. **Receipts:** Paystack emails a receipt for every card charge (enabled on each plan). Prices are shown as **VAT-inclusive** final amounts. Before a VAT-registered company needs a tax invoice, Joe confirms VAT and KRA eTIMS obligations with an accountant; until then, receipts carry no VAT breakdown.
9. **Ownership hand-over:** staff-only in v1, through an admin action that moves the owner role to another member. The owner can't leave their own company in-app.
10. **Members with an unpaid deck can't switch company:** accepting an invite is refused until that deck is paid or discarded, so no deck is stranded without a payer.

## How Paystack runs it

Paystack API (`https://api.paystack.co`, bearer secret key, amounts in cents):

| Step | Call |
|---|---|
| Create the plans | `POST /plan` `{name, amount, interval: "monthly", currency: "KES", send_invoices: true, send_sms: false}` → `plan_code`. Run by `manage.py sync_paystack_plans`, which stores `Plan.paystack_plan_code` and updates the amount when a price changes (`PUT /plan/:code`; Paystack applies a new amount to future charges). |
| Subscribe | `POST /transaction/initialize` `{email, amount, plan: plan_code, reference: subra-sub-<company>-<hex8>, channels: ["card"], callback_url, metadata}`. Paystack creates the subscription when the first charge succeeds and sends `subscription.create`. |
| Return from checkout | `GET /billing/card/return/?reference=…`: login required; trusts nothing in the query string except as a lookup key into the user's own company's pending payments; calls `GET /transaction/verify/<reference>`. |
| Read a subscription | `GET /subscription/:code` → `status`, `next_payment_date`, `email_token`, `authorization` (last4, exp, brand), `plan`. This is the source of truth. Every subscription webhook triggers a re-read; SUBRA never relies on a webhook payload's shape for state. |
| Find a new subscription | `GET /subscription?customer=<customer id>&plan=<plan id>` when `subscription.create` hasn't arrived. |
| Change card | `GET /subscription/:code/manage/link` → a Paystack-hosted page. The owner is redirected there. |
| Cancel | `POST /subscription/disable` `{code, token: email_token}`. |
| Resume before period end | `POST /subscription/enable` `{code, token}`. |
| Top-up | `POST /transaction/initialize` without a plan, `channels: ["card", "mobile_money"]`, reference `subra-top-<company>-<hex8>`. |

Subscription statuses from Paystack and what SUBRA does:

| Paystack status | SUBRA status | Meaning |
|---|---|---|
| `active` | `active` | Paid up; `renews_at = next_payment_date`. |
| `attention` | `past_due` | The last charge failed. Paystack won't retry until the next payment date, so SUBRA gives 7 days of grace from the first failure. |
| `non-renewing` | `cancelling` | Cancelled; no further charges; credits last until `renews_at`. |
| `cancelled`, `complete`, `completed` | `cancelled` | Ended. |

Webhook events handled (same `/billing/webhooks/paystack/` view and HMAC-SHA512 check):

- `charge.success` with our `subra-` deck reference: unchanged M-Pesa path.
- `charge.success` with a `subra-sub-` or `subra-top-` reference: settles that payment (first grant or top-up).
- `charge.success` with any other reference that carries a `plan` or matches a known `customer_code`: a **renewal**. A `Payment(kind=renewal)` is created keyed on Paystack's reference, the transaction is verified with `GET /transaction/verify`, then the grant is written. Idempotent on the reference.
- `subscription.create`, `subscription.not_renew`, `subscription.disable`, `invoice.create`, `invoice.update`, `invoice.payment_failed`, `subscription.expiring_cards`: matched by `subscription_code` (or customer), then the subscription is re-read from Paystack and synced. `invoice.payment_failed` also starts grace.
- `refund.processed`: marks the payment refunded and claws back its credits.
- `charge.dispute.*`: stored and alerted.
- Anything else: stored and ignored.

Every delivery is stored in a `WebhookDelivery` row before it's processed (provider, event, reference, body, outcome), so unknown and failed events leave a trace. Rows older than 90 days are pruned.

**Why re-read instead of trusting payloads:** paystack.com isn't reachable from the build environment and Paystack publishes few payload samples for subscription events. A re-read via the documented `GET /subscription/:code` makes the code correct whatever the webhook's nesting is. Before launch, record real test-mode payloads (section 13) and add them as fixtures.

## 1. Plans, packs and credit prices

- `PriceTier.credits` (default 0; seeded Small 4, Standard 7, Large 10). The credits path is offered only when it is above 0.
- `Plan`: `code`, `name`, `price_kes`, `monthly_credits`, `paystack_plan_code`, `active`, `sort_order`.
- `CreditPack`: `name`, `credits`, `price_kes`, `active`. Seeded: 10 credits KES 500, 25 credits KES 1,150.
- `Order.credit_cost` is snapshotted from the tier at upload. The upload event reads "20 slides, Standard tier, KES 350 or 7 credits".

## 2. Company accounts

- `Company`: `name`, `owner`, `created_at`, `archived_at`.
- `Membership`: `company`, `user` (unique), `role` owner|member, `invited_by`, `created_at`. `company_for(user)` get-or-creates a personal company, so nothing changes at signup and nothing is backfilled.
- `Invite`: `company`, `email`, `token_hash` (SHA-256 of a random token; the raw token is only in the email), `invited_by`, `created_at`, `expires_at` (7 days), `accepted_at`, `revoked_at`.
- Permissions are enforced in views. Owner: everything a member can do, plus manage the plan and card, buy top-ups, invite, revoke, remove members, see usage, download any company deck. Member: their own decks, paying with credits or M-Pesa.
- Accepting an invite: the signed-in user must match the invited email (or sign up with the email locked). Refused while the user's own company has a live subscription, a positive balance, or other members, or while the user has an unpaid deck. Earlier orders stay with the old company; the emptied personal company is archived.
- Removing a member deletes the membership; they get a fresh personal company on their next request and keep their login and decks.
- `Order.company` is set at upload from the uploader's membership.
- Password reset (Django's built-in flow) is added.

## 3. Subscription lifecycle

`Subscription`: `company`, `plan`, `status`, `paystack_subscription_code`, `paystack_email_token`, `paystack_customer_code`, `card_last4`, `card_brand`, `card_exp`, `renews_at`, `period_index`, `past_due_since`, `grace_ends_at`, `consent_text`, `consented_at`, `cancelled_at`, `ended_at`, `last_synced_at`, `raw`, timestamps. One live subscription per company (partial unique constraint). `transition()` logs a `SubscriptionEvent`.

| SUBRA status | Meaning |
|---|---|
| `incomplete` | Checkout opened; first payment pending. Abandoned after 24 hours. |
| `active` | Paid up. |
| `past_due` | A renewal failed; credits spendable until `grace_ends_at`. |
| `cancelling` | No further charges; credits until `renews_at`. |
| `cancelled`, `lapsed`, `abandoned` | Terminal. |

- **First payment verified** → `active`, grant 1.
- **Renewal paid** → `active`, next grant, `renews_at` re-read from Paystack, grace cleared.
- **Renewal failed** → `past_due`, `grace_ends_at = past_due_since + 7 days`, email the owner ("We couldn't charge your card ending 4081. Your 23 credits still work until 30 Oct. Update your card to keep them."), banner for everyone.
- **Fixing it:** "Update card" opens Paystack's manage page. Because Paystack doesn't promise to re-charge immediately after a card update, the plan page also offers **Pay now with a new card**: SUBRA disables the failed Paystack subscription and opens a new subscription checkout on the same plan. The balance is kept (it hasn't expired), the new subscription grants under the cap, and the new period starts today.
- **Grace over** (hourly task) → the Paystack subscription is disabled, the balance expires, status `lapsed`, email.
- **Cancel** → `POST /subscription/disable`, status `cancelling`; at `renews_at` (hourly task) → `cancelled`, balance expires. **Resume** before `renews_at` → `POST /subscription/enable`, back to `active`, re-read.
- **Change plan (v1):** cancel the current subscription and start a new checkout on the new plan, charged today. Credits carry over (subject to the cap on the new grant). The screen says "Your new plan starts today and is charged now; your credits carry over." No proration.
- Recurring consent: a checkbox stating the amount and "every month until I cancel", stored with a timestamp. Paystack sends the charge receipts; SUBRA sends a reminder 3 days before each renewal from the stored `renews_at`.
- A system failure (Paystack unreachable, bad key) never lapses anyone: lapsing requires Paystack to report the subscription as not active on a fresh re-read. `RENEWALS_PAUSED` stops all lapsing and expiry while on.

## 4. The payments table

`Payment` stays the one money table:

- `order` becomes nullable. New: `kind` (deck | subscription | renewal | topup), `channel` (mpesa | card), `company`, `subscription`, `credit_pack`. `phone` is blank-able. Existing rows: deck, mpesa.
- `Payment.provider` defaults to `paystack` (it defaulted to `intasend`).
- Card payments create their `Payment` row with our reference **before** calling Paystack, outside any lock.
- `apply_event` dispatches on `kind`: deck → `_mark_paid` (unchanged); subscription → activate and grant; topup → grant; renewal → grant.
- `FAILURE_MESSAGES` gains card failures; fallback copy is per channel.
- Fees: `PAYMENT_FEE_RATES = {"mpesa": 0.015, "card": 0.029}` for estimates.

## 5. The credits ledger

`CreditTransaction`, append-only: `company`, `delta`, `balance_after`, `kind` (grant | topup | debit | refund | expire | clawback | adjust), `idempotency_key` (unique), `kes_value`, `order`, `payment`, `subscription`, `user`, `note`, `created_by`, `created_at`. Every write locks the Company row first (lock order: Company, then Order).

| Event | Delta | Key |
|---|---|---|
| Subscription or renewal paid | + `max(0, min(plan, 2 × plan − balance))` | `grant:payment:<pk>` |
| Top-up paid | + pack credits | `topup:payment:<pk>` |
| Download with credits | − `order.credit_cost` | `debit:order:<pk>` |
| Subscription ended with no other live one | − balance | `expire:sub:<pk>` |
| A card payment refunded | − what it granted (may go negative; spending blocked) | `clawback:payment:<pk>` |
| Staff adjustment | ± n with a note | `adjust:<uuid>` |

`pay_with_credits(order, user)` locks the Company then the Order, requires `order.can_pay`, the order in the user's company, a live subscription and enough balance, writes the debit, and settles the order with `paid_with = "credits"`. No `Payment` row.

## 6. Order flow changes

- Quote panel: "KES 350"; subscribers also see "or 7 credits".
- Ready and unpaid, enough credits: **Use 7 credits to download**, the balance underneath, and the M-Pesa form under "Or pay KES 350 with M-Pesa". Not enough: "You have 3 credits; this deck needs 7", then top-up (owner) or "Ask <owner> to top up" (member), then the M-Pesa form. No subscription: today's page, unchanged.
- Paid: "Paid with 7 credits by <email>" or "Paid, M-Pesa receipt …".
- `download` and `detail` are visible to the uploader or the company owner; everything that changes a deck stays uploader-only.

## 7. Jobs

Beat: `subscriptions-hourly` (lapse after grace, end cancelled periods, abandon stale checkouts, re-sync subscriptions whose `renews_at` passed more than 6 hours ago without a renewal), `billing-daily` (3-day reminders, ledger integrity check, prune webhook deliveries). `manage.py billing_tick [--now ISO]` runs both offline. `manage.py sync_paystack_plans` creates and updates Paystack plans.

## 8. Screens

- **Plans** (`/accounts/plans/`): each plan's price, credits, roughly how many decks per tier, KES per credit, the carry-over rule, what happens if the card fails, the consent checkbox, **Subscribe with card**.
- **Plan & credits** (`/accounts/plan/`): balance; plan and renewal date with the card's last four digits; past-due banner with **Update card** and **Pay now with a new card**; cancel or resume; change plan; top-ups; members and invites; usage per member; company decks; ledger history. Members see the balance, their usage, and their decks.
- **Nav:** "Plan & credits" and a balance pill for companies that have ever subscribed.
- **Emails (ours):** invite, subscription active, renewal failed, last day of grace, credits expired, cancellation confirmed, 3-day reminder. Paystack sends charge receipts.
- **Legal:** privacy and terms gain the subscription paragraphs; the "draft" banner stays until Joe reviews them.

## 9. Admin and alerts

Admin for plans, packs, companies (members, balance, adjust credits, transfer ownership), subscriptions (read-only, with events and "re-sync from Paystack"), the ledger (read-only), and webhook deliveries. `PriceTierAdmin` gains `credits`. `mark_refunded` also claws back credits. Alerts: unexpected subscription with no matching company, clawback leaving a negative balance, dispute, ledger mismatch, plan missing its Paystack code.

## 10. Settings

| Setting | Default |
|---|---|
| `SUBSCRIPTIONS_ENABLED` | `False` |
| `RENEWALS_PAUSED` | `False` |
| `SUBSCRIPTION_GRACE_DAYS` | `7` |
| `CREDIT_CARRYOVER_MULTIPLE` | `2` |
| `PAYMENT_FEE_RATES` | `{"mpesa": 0.015, "card": 0.029}` |
| `INVITE_TTL_DAYS` | `7` |
| `INVITE_RATE_LIMIT` | `(20, 3600)` |
| `BILLING_RATE_LIMIT` | `(10, 600)` |

## 11. Prerequisites (Joe)

1. Register the business with Paystack before subscriptions go live; set `PAYSTACK_STARTER_LIMIT_KES` from the dashboard figure until then.
2. Tick "Accept international payments" in Paystack settings.
3. SMTP credentials on the server.
4. Ask Paystack: do Kenyan cards return reusable authorizations, do any issuers demand 3-D Secure on recurring charges, and does updating a card on the manage page charge an outstanding invoice immediately?
5. In test mode: run `manage.py sync_paystack_plans`, subscribe with test card 4084 0840 8408 4081, and save the `charge.success`, `subscription.create` and `invoice.*` payloads from the admin's webhook deliveries as fixtures.
6. Pilot with a few friendly companies before advertising plans; then set `SUBSCRIPTIONS_ENABLED=1`.

## 12. Not in this change

Billing in USD, proration, per-member limits, multiple admins, one-off card payments for a single deck, per-company AI caps, M-Pesa Ratiba.

## 13. Testing

Test-first, in the existing style. Paystack is faked at the HTTP session level (`FakeSession`) with responses built from the documented fields, marked as unrecorded. Covered: lazy companies; invites (binding, expiry, revoke, the refusals); the ledger (grant once per payment, the cap, top-ups never destroyed, debit once, expiry, clawback); pay with credits including two members racing for the last credits; ownership table for owner, member, stranger; subscription sync from every Paystack status; webhook dispatch for each event; grace and lapse with and without `RENEWALS_PAUSED`; a Paystack outage never lapsing anyone; cancel and resume; the flag hiding every entry point; and the existing suite passing unchanged.
