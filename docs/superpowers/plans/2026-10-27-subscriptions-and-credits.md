# Subscriptions with credits, and company accounts: build plan

Spec: [2026-10-27-subscriptions-and-credits-design.md](../specs/2026-10-27-subscriptions-and-credits-design.md). Everything ships behind `SUBSCRIPTIONS_ENABLED` (off). Each phase is test-first, keeps the existing suite green, and is committed on its own.

## Phase 1: companies, the ledger, paying with credits

- `accounts`: `Company`, `Membership`, `company_for(user)`.
- `billing`: `PriceTier.credits` (seeded 4/7/10), `CreditTransaction`, `billing/credits.py` (`balance`, `grant`, `debit`, `expire`, `adjust`, `clawback`, `grant_amount` with the cap).
- `orders`: `Order.company`, `credit_cost`, `paid_with`; `create_order` snapshots them; `_settle_order` shared with `_mark_paid`; `pay_with_credits`; `pay_credits` view; owner visibility for `detail` and `download`.
- Admin: company with balance and "Adjust credits", ledger read-only, `PriceTierAdmin.credits`.

## Phase 2: Paystack plans, checkout, top-ups, webhooks

- `Plan`, `CreditPack`, `Subscription`, `SubscriptionEvent`, `WebhookDelivery`; `Payment` kind, channel, company, subscription, credit_pack; nullable order; provider default paystack.
- `PaystackProvider`: `create_plan`, `update_plan`, `initialize_checkout`, `verify`, `fetch_subscription`, `list_subscriptions`, `manage_link`, `disable_subscription`, `enable_subscription`.
- `billing/subscriptions.py`: `start_subscription_checkout`, `start_topup_checkout`, `settle_card_payment`, `sync_subscription`, `handle_paystack_event`.
- Views: plans, subscribe, top-up, card return; webhook view routes non-deck events.
- `manage.py sync_paystack_plans`.

## Phase 3: grace, lapse, cancel, resume, jobs, emails

- `past_due` on `invoice.payment_failed` or a sync reporting `attention`; hourly lapse; cancel/resume; change plan; pay now with a new card.
- `billing.tasks.subscriptions_hourly`, `billing_daily`; `manage.py billing_tick`.
- Customer emails in `templates/billing/email/`.
- `refund.processed` clawback; `mark_refunded` clawback.

## Phase 4: invites, password reset, screens, docs

- `Invite` model and flow, rate-limited; remove member; admin ownership transfer.
- Django password reset views and templates.
- Plan & credits page, plans page, nav pill (context processor), detail-page credit panel.
- Privacy and terms paragraphs; `.env.example`, README, PROGRESS.md.
- Browser check at 390px and 1280px with a simulated subscription (DEBUG only).
