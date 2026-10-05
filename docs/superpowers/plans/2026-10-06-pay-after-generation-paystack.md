# Pay After Generation, Paystack Instead of IntaSend: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Customers upload a deck, SUBRA generates it, they look through every slide and switch themes for free, then pay with M-Pesa through Paystack to download the editable file.

**Architecture:** `Order.status` becomes a processing-only lifecycle (`quoted → queued → processing → ready ⇄ rerendering`, plus `failed` and `expired`). Payment is a separate `Order.paid_at` and the existing `Payment` records. The verified layout plan is saved on the order, so a theme switch re-renders it with no Claude call. Paystack becomes the default `PaymentProvider`. Webhooks pass the raw body, so Paystack's HMAC signature can be checked.

**Tech Stack:** Python 3.12, Django 5.2, Celery, pytest with pytest-django, python-pptx, requests, Tailwind 4 for the compiled CSS, htmx.

**Spec:** [docs/superpowers/specs/2026-10-05-pay-after-generation-paystack-design.md](../specs/2026-10-05-pay-after-generation-paystack-design.md). Read it before starting. This plan argues from it.

## Global Constraints

- Run tests from the repo root with `.venv/bin/python -m pytest`. Settings come from `subra.test_settings` via `pyproject.toml`.
- Run management commands with `--settings=subra.test_settings`, e.g. `.venv/bin/python manage.py makemigrations --settings=subra.test_settings`.
- **Never call Paystack with a live key.** The fixture script refuses anything but `sk_test_…`.
- **Never print, log or commit a secret.** `.env` is gitignored, and Joe pastes his own key into it.
- Customer-facing copy uses these exact strings from the spec:
  - `Pay for or discard <original name> first.`
  - `We couldn't switch to <theme name>; your deck is unchanged.`
  - `We couldn't polish this deck` followed by `You haven't been charged.`
  - `Unpaid decks are deleted after 48 hours`
- **Retention:**
  - `UNPAID_RETENTION_HOURS = 48` for quoted, unpaid and failed decks.
  - `FILE_RETENTION_DAYS = 7` counted from payment.
  - Theme switches don't change `expires_at`.
- **One unpaid deck per account:** statuses `queued`, `processing`, `ready` and `rerendering` with `paid_at` empty count. Other statuses don't.
- **Payment rules stay as they are:**
  - Callbacks are idempotent and their raw payloads are stored.
  - Only the exact quoted amount counts as paid.
  - Status is checked after 60 seconds, then by the 5-minute sweep.
  - Duplicate payments and amount mismatches alert the admins.
- **CSS:** after changing classes in templates, run `npm run build:css` and commit `static/css/app.css`.
- **Commits:** match the repo's style, an imperative sentence with no prefix (e.g. "Add Paystack provider"). End every message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Code style:** match the existing code. Sparse comments that explain why, lines up to 120 characters, and plain British English in UI text ("colours").

## Review Focus

These failure modes aren't covered by the spec's test list. Each one has a test in the task that owns the code:

1. **A payment arrives while the deck is switching themes** (`rerendering`), for example a late callback after a timeout. The order must be marked paid as normal. *(Task 5: `test_a_payment_that_arrives_during_a_switch_is_accepted`)*
2. **A theme switch finishes after the 10-minute release,** or after a newer switch has started. Its output must be thrown away, and the current file and theme kept. *(Task 5: `test_a_switch_that_finishes_after_being_released_is_discarded`)*
3. **"Discard" is pressed while the deck is queued, processing or switching.** It must be refused with the files kept. *(Task 4: `test_decks_in_flight_or_gone_cant_be_discarded`)*
4. **A Paystack webhook arrives for a charge SUBRA never made,** such as another charge on the same account. It must return 200 and change nothing. *(Task 3: `test_webhook_for_a_charge_we_never_made_is_accepted_and_ignored`)*
5. **LibreOffice fails, but the deck passed verification.** The page must say the previews are missing and still let the customer pay. *(Task 4: `test_a_deck_without_previews_says_so`)*

## Before starting

- `static/css/app.css` has an uncommitted Tailwind rebuild from before this plan. Ask Joe whether to keep or discard it. It's generated, so Task 4's `npm run build:css` regenerates it either way.
- Task 3 needs Joe's Paystack **test** secret key in `.env` (`PAYSTACK_SECRET_KEY=sk_test_…`). Check it's there without printing it:
  ```bash
  grep -c '^PAYSTACK_SECRET_KEY=sk_test_' .env
  ```
  `1` means it's ready. If you get `0`, ask Joe to paste it in, and do Tasks 1 and 2 meanwhile.

## File map

| File | Change |
|---|---|
| `polisher/pipeline.py` | `PolishResult.plan`, `RerenderResult`, `rerender()` (Task 1) |
| `billing/providers/__init__.py` | `handle_callback(body, headers)`, `configured`, Paystack in `get_provider` (Tasks 2, 3) |
| `billing/providers/intasend.py`, `daraja.py` | Parse raw bodies (Task 2). IntaSend gets `configured` (Task 3) |
| `billing/providers/paystack.py` | New `PaystackProvider` (Task 3) |
| `scripts/record_paystack_fixtures.py` | New: records test-mode payloads (Task 3) |
| `billing/tests/fixtures/paystack/*.json` | New: recorded payloads (Task 3) |
| `billing/views.py`, `billing/urls.py` | One webhook view for all providers (Tasks 2, 3) |
| `billing/services.py` | Raw-body callbacks (Task 2). Pay only `ready` decks, set `paid_at` (Task 4). Starter limit (Task 6) |
| `billing/models.py`, `billing/admin.py` | `Payment` `refunded` status and `refunded_at`, "Mark refunded" action (Task 4) |
| `orders/models.py` | New statuses, `paid_at`, `plan`, `switch_error` (Task 4). `switching_to` (Task 5) |
| `orders/migrations/0004–0006` | Schema, status mapping and the switching field (Tasks 4, 5) |
| `orders/services.py` | `start_generation`, `unpaid_work`, `discard_order` (Task 4). `start_theme_switch` (Task 5) |
| `orders/processing.py` | `ready` and `failed`, saved plan, failure alert (Task 4). `run_rerender` (Task 5) |
| `orders/tasks.py` | Queue on `queued`, retention rules (Task 4). `rerender_order` and stuck release (Task 5) |
| `orders/views.py`, `orders/urls.py` | `polish`, `discard`, two-track polling, download gate (Task 4). `switch_theme` (Task 5) |
| `orders/admin.py`, `orders/spend.py` | Paid column and filter (Task 4). Monthly summary (Task 6) |
| `templates/orders/*`, `templates/admin/orders/dailyspend/change_list.html` | New screens (Tasks 4, 5, 6) |
| `templates/orders/home.html`, `templates/core/terms.html`, `templates/core/privacy.html`, `core/views.py` | Wording (Task 7) |
| `subra/settings.py`, `subra/test_settings.py`, `.env.example` | New settings (Tasks 3–6) |
| `conftest.py` and test files | Shared fixtures and new tests in every task |
| `README.md`, `PROGRESS.md` | Docs (Task 8) |

---

### Task 1: Save the winning plan and re-render it in another theme

**Files:**
- Modify: `polisher/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces:
  - `PolishResult.plan -> dict | None`: the verified plan of a successful run, `None` otherwise.
  - `RerenderResult(ok: bool, output_path: Path | None, render_report=None, verification=None, failure: str | None = None)`, with property `overflow_slides -> list`.
  - `rerender(input_path, output_path, theme: str, plan_dict: dict, render_fn=render) -> RerenderResult`. It makes no planner call and does no retry. On failure the output file is never created.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_pipeline.py`. Also add `import re` to the imports, and change the pipeline import to `from polisher.pipeline import PlannerUnavailable, polish, rerender`.

```python
def _theme_name(path):
    """The colour scheme name apply_theme writes, e.g. "SUBRA Clean Slate"."""
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    part = Presentation(path).slide_master.part.part_related_by(RT.THEME)
    return re.search(rb'<a:clrScheme name="([^"]*)"', part.blob).group(1).decode()


def test_a_successful_run_exposes_its_verified_plan(tmp_path, deck_path):
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=HeuristicPlanner())
    assert result.plan is not None and result.plan == result.attempts[-1].plan
    assert len(result.plan["slides"]) == 7


def test_a_failed_run_has_no_plan(tmp_path, deck_path):
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=ScriptedPlanner(["bad", "bad"]))
    assert not result.ok and result.plan is None


def test_rerender_applies_another_theme_to_a_saved_plan(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())
    out = tmp_path / "slate.pptx"
    result = rerender(deck_path, out, "slate", first.plan)
    assert result.ok, result.failure
    assert result.verification.passed
    assert _theme_name(out) == "SUBRA Clean Slate"
    assert _theme_name(tmp_path / "navy.pptx") == "SUBRA Navy Executive"


def test_rerender_refuses_a_plan_that_no_longer_fits(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())
    broken = copy.deepcopy(first.plan)
    broken["slides"] = broken["slides"][:1]
    out = tmp_path / "out.pptx"
    result = rerender(deck_path, out, "slate", broken)
    assert not result.ok and "s2-t1" in result.failure
    assert not out.exists()


def test_rerender_refuses_an_unreadable_plan(tmp_path, deck_path):
    result = rerender(deck_path, tmp_path / "out.pptx", "slate", {"pages": []})
    assert not result.ok and "unreadable" in result.failure


def test_rerender_reports_a_verification_failure(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())

    def lossy_render(plan, deck, theme, output, source):
        report = render(plan, deck, theme, output, source)
        prs = Presentation(output)
        pic = next(sh for slide in prs.slides for sh in slide.shapes if sh.shape_type == 13)
        pic.part.related_part(pic._element.blipFill.blip.rEmbed)._blob = build_png_variant()
        prs.save(output)
        return report

    out = tmp_path / "out.pptx"
    result = rerender(deck_path, out, "slate", first.plan, render_fn=lossy_render)
    assert not result.ok and "Image [s5-i1]" in result.failure
    assert not out.exists()


def test_rerender_survives_a_renderer_crash(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())

    def crash(*args):
        raise RuntimeError("boom")

    out = tmp_path / "out.pptx"
    result = rerender(deck_path, out, "slate", first.plan, render_fn=crash)
    assert not result.ok and result.failure == "Render failed: RuntimeError: boom"
    assert not out.exists()
```

- [ ] **Step 2: Run them and check they fail.**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q`
Expected: ImportError, `cannot import name 'rerender'`.

- [ ] **Step 3: Implement.** In `polisher/pipeline.py`:

Change the plan import to:
```python
from .plan import PlanFormatError, parse_plan, repair_plan, validate_plan
```

In the module docstring, change "If that fails, the run fails and the order goes to refund." to "If that fails, the run fails (and the customer isn't charged)."

Add to `PolishResult` after `overflow_slides`:
```python
    @property
    def plan(self):
        """The winning attempt's verified plan (a dict), saved so a theme switch can re-render it."""
        return self.attempts[-1].plan if self.ok and self.attempts else None
```

Add after `polish()`:
```python
@dataclass
class RerenderResult:
    ok: bool
    output_path: Path | None
    render_report: object = None
    verification: object = None
    failure: str | None = None

    @property
    def overflow_slides(self):
        return self.render_report.overflow_slides if self.render_report else []


def rerender(input_path, output_path, theme, plan_dict, render_fn=render):
    """Render a saved, already-verified plan in another theme: no planner call and no retry.

    A new font can reflow text, so the result is verified again. On failure nothing is written.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    validate_file(input_path)
    deck = extract_deck(input_path)
    theme_obj = get_theme(theme)
    try:
        plan = parse_plan(plan_dict)
    except PlanFormatError as e:
        return RerenderResult(ok=False, output_path=None, failure=f"Saved plan unreadable: {e}")
    errors = validate_plan(plan, deck)
    if errors:
        return RerenderResult(ok=False, output_path=None,
                              failure="Saved plan no longer fits: " + "; ".join(errors[:5]))

    fd, tmp_name = tempfile.mkstemp(suffix=".pptx", dir=output_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        report = render_fn(plan, deck, theme_obj, tmp, input_path)
        verification = verify(deck, extract_deck(tmp), slide_sources=report.slide_sources,
                              dropped_backgrounds=report.dropped_backgrounds)
        if not verification.passed:
            return RerenderResult(ok=False, output_path=None, render_report=report, verification=verification,
                                  failure="Verification failed: " + "; ".join(verification.problems[:5]))
        os.replace(tmp, output_path)
        return RerenderResult(ok=True, output_path=output_path, render_report=report, verification=verification)
    except Exception as e:  # a crash leaves the previous deck in place
        return RerenderResult(ok=False, output_path=None, failure=f"Render failed: {type(e).__name__}: {e}")
    finally:
        tmp.unlink(missing_ok=True)
```

- [ ] **Step 4: Run the pipeline tests, then the whole suite.**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q && .venv/bin/python -m pytest -q`
Expected: all pass (291 existing + 7 new).

- [ ] **Step 5: Commit.**

```bash
git add polisher/pipeline.py tests/test_pipeline.py
git commit -m "$(printf 'Save the verified plan and re-render it in another theme\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 2: Webhooks pass the raw body to the provider

Paystack signs the raw request bytes, so providers must receive them. This task changes no payment behaviour.

**Files:**
- Modify: `billing/providers/__init__.py`, `billing/providers/intasend.py`, `billing/providers/daraja.py`, `billing/services.py`, `billing/views.py`, `billing/urls.py`
- Test: `billing/tests/test_intasend.py`, `billing/tests/test_daraja.py`, `billing/tests/test_services.py`

**Interfaces:**
- Produces:
  - `PaymentProvider.handle_callback(body: bytes, headers) -> PaymentEvent`. It raises `CallbackRejected` when the webhook isn't genuine and `ValueError` when it's malformed.
  - `billing.services.handle_callback(provider_name, body, headers, provider=None)`.
  - The view `billing.views.webhook(request, provider)` at `billing/webhooks/<slug:provider>/`, URL name `billing:webhook`.
  - `billing.views.WEBHOOK_PROVIDERS = {"intasend"}`. Task 3 adds `"paystack"`.

- [ ] **Step 1: Update the tests first.**

In `billing/tests/test_intasend.py`, add `import json` and this helper under `provider()`:
```python
def callback(p, payload):
    return p.handle_callback(json.dumps(payload).encode(), {})
```
Replace every `provider(FakeSession([])).handle_callback(<payload>)` with `callback(provider(FakeSession([])), <payload>)`. That covers the 6 tests from `test_callback_with_wrong_challenge_is_rejected` to `test_partial_payment_is_not_paid`. Then append:
```python
@pytest.mark.parametrize("body", [b"{not json", b"[]", b'{"state": "COMPLETE"}'])
def test_malformed_callbacks_are_value_errors(body):
    with pytest.raises(ValueError):
        provider(FakeSession([])).handle_callback(body, {})
```

In `billing/tests/test_daraja.py`, change line 113 to:
```python
    event = DarajaProvider(client=None).handle_callback(json.dumps(payload).encode(), {})
```
and append:
```python
def test_daraja_rejects_malformed_callbacks():
    with pytest.raises(ValueError):
        DarajaProvider(client=None).handle_callback(b'{"Body": {}}', {})
```

In `billing/tests/test_services.py`, change `FakeProvider.handle_callback` and the `callback` helper:
```python
    def handle_callback(self, body, headers):
        return event(**json.loads(body))
```
```python
def callback(**kw):
    return services.handle_callback("intasend", json.dumps(kw).encode(), {}, provider=FakeProvider())
```
and append:
```python
def test_webhook_for_an_unknown_provider_is_404(client, db):
    assert client.post("/billing/webhooks/stripe/", data="{}", content_type="application/json").status_code == 404
```

- [ ] **Step 2: Run them and check they fail.**

Run: `.venv/bin/python -m pytest billing/tests -q`
Expected: failures with `TypeError` (`handle_callback()` takes 2 positional arguments) and a 404 mismatch.

- [ ] **Step 3: Implement.**

In `billing/providers/__init__.py`, replace the `handle_callback` line in `PaymentProvider`:
```python
    def handle_callback(self, body: bytes, headers) -> PaymentEvent:
        """Authenticate and parse a webhook. Raises CallbackRejected if it isn't genuine, ValueError if malformed."""
```

In `billing/providers/intasend.py`, add `import json` and replace `handle_callback`:
```python
    def handle_callback(self, body, headers):
        payload = json.loads(body)
        if not isinstance(payload, dict) or "invoice_id" not in payload:
            raise ValueError("Not an IntaSend webhook")
        challenge = str(payload.get("challenge") or "")
        if not self.webhook_secret or not compare_digest(challenge, self.webhook_secret):
            raise CallbackRejected("IntaSend webhook challenge mismatch")
        return self._event(payload, raw=payload)
```

In `billing/providers/daraja.py`, add `import json` and change the start of `DarajaProvider.handle_callback`:
```python
    def handle_callback(self, body, headers):
        # Daraja doesn't sign callbacks: protect the callback URL with a secret path segment.
        payload = json.loads(body)
        try:
            stk = payload["Body"]["stkCallback"]
            stk["CheckoutRequestID"]
        except (KeyError, TypeError):
            raise ValueError("Not a Daraja STK callback")
        code = str(stk.get("ResultCode"))
```
The rest of the method is unchanged.

In `billing/services.py`, replace `handle_callback`:
```python
def handle_callback(provider_name, body, headers, provider=None):
    """Authenticate and apply a webhook. Raises CallbackRejected (not genuine) or ValueError (malformed)."""
    provider = provider or get_provider(provider_name)
    return apply_event(provider.name, provider.handle_callback(body, headers), source="callback")
```

Replace `billing/views.py`:
```python
import logging

from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.ratelimit import client_ip

from .providers import CallbackRejected
from .services import handle_callback

log = logging.getLogger(__name__)
WEBHOOK_PROVIDERS = {"intasend"}  # Daraja doesn't sign callbacks, so it gets no public webhook


@csrf_exempt
@require_POST
def webhook(request, provider):
    if provider not in WEBHOOK_PROVIDERS:
        raise Http404
    try:
        handle_callback(provider, request.body, request.headers)
    except CallbackRejected:
        log.warning("Rejected %s webhook from %s", provider, client_ip(request))
        return HttpResponseForbidden("rejected")
    except ValueError:
        return HttpResponseBadRequest("malformed webhook")
    return JsonResponse({"status": "ok"})
```

In `billing/urls.py`, replace the single path:
```python
    path("webhooks/<slug:provider>/", views.webhook, name="webhook"),
```

- [ ] **Step 4: Run the tests.**

Run: `.venv/bin/python -m pytest billing/tests -q && .venv/bin/python -m pytest -q`
Expected: all pass. The existing IntaSend webhook tests post to `/billing/webhooks/intasend/`, which still resolves.

- [ ] **Step 5: Commit.**

```bash
git add billing
git commit -m "$(printf 'Pass raw webhook bodies to payment providers\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 3: Paystack provider, from recorded test-mode payloads

**Files:**
- Create:
  - `scripts/record_paystack_fixtures.py`
  - `billing/tests/fixtures/paystack/*.json` (recorded)
  - `billing/providers/paystack.py`
  - `billing/tests/test_paystack.py`
- Modify:
  - `billing/providers/__init__.py`, `billing/providers/intasend.py`, `billing/views.py`
  - `orders/views.py`
  - `subra/settings.py`, `subra/test_settings.py`, `.env.example`
- Test: `billing/tests/test_paystack.py`, `orders/tests/test_views.py`

**Interfaces:**
- Consumes: `handle_callback(body, headers)` and `WEBHOOK_PROVIDERS` from Task 2.
- Produces:
  - `PaystackProvider(secret_key, session=None, timeout=30)`, with `name = "paystack"`, `from_settings()`, `configured`, `initiate`, `check_status` and `handle_callback`.
  - `paystack_phone(phone: str) -> str`.
  - `configured: bool` on every provider built by `get_provider()`.
  - `orders.views._dev_payment_enabled() -> bool`.
  - Settings `PAYSTACK_SECRET_KEY` and `PAYMENT_PROVIDER` (default `"paystack"`). `PAYMENT_FEE_RATE` now defaults to `0.015`.

- [ ] **Step 1: Write the fixture recorder.** Create `scripts/record_paystack_fixtures.py`:

```python
"""Record Paystack test-mode responses as test fixtures (Amendment 2).

    .venv/bin/python scripts/record_paystack_fixtures.py --phone <Paystack's test M-Pesa number>

Reads PAYSTACK_SECRET_KEY from the environment or from .env, and refuses anything but a test key
(sk_test_...), so it can never move real money. It starts one KES 10 M-Pesa charge, polls
/transaction/verify until the charge settles (2 minutes at most), and writes the raw JSON to
billing/tests/fixtures/paystack/. Webhooks can't reach a laptop, so webhook_charge_success.json is
built from the verify response: a charge.success event carries the same transaction object as "data".
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "billing" / "tests" / "fixtures" / "paystack"
API = "https://api.paystack.co"
UNSETTLED = {"pending", "ongoing", "processing", "queued", "pay_offline", "send_otp"}


def secret_key():
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    env_file = ROOT / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("PAYSTACK_SECRET_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
    if not key.startswith("sk_test_"):
        sys.exit("PAYSTACK_SECRET_KEY must be a Paystack TEST key (sk_test_...). Live keys are refused.")
    return key


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {(OUT / name).relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description="Record Paystack test-mode fixtures")
    parser.add_argument("--phone", required=True, help="exactly as Paystack should receive it")
    parser.add_argument("--email", default="fixtures@example.com")
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {secret_key()}"}
    reference = f"subra-fixture-{uuid.uuid4().hex[:8]}"
    body = {"email": args.email, "amount": 1000, "currency": "KES", "reference": reference,
            "mobile_money": {"phone": args.phone, "provider": "mpesa"}}
    resp = requests.post(f"{API}/charge", json=body, headers=headers, timeout=30)
    print("charge:", resp.status_code, resp.text[:300])
    save("charge_request.json", body)
    save("charge_response.json", {"http_status": resp.status_code, "body": resp.json()})
    if resp.status_code >= 400 or not resp.json().get("status"):
        sys.exit("Paystack refused the charge; see charge_response.json")

    deadline = time.time() + 120
    while True:
        verify = requests.get(f"{API}/transaction/verify/{reference}", headers=headers, timeout=30).json()
        status = (verify.get("data") or {}).get("status")
        print("verify:", status)
        if status not in UNSETTLED or time.time() > deadline:
            break
        time.sleep(5)
    save(f"verify_{status}.json", verify)
    if status == "success":
        save("webhook_charge_success.json", {"event": "charge.success", "data": verify["data"]})


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Record the fixtures.**
  1. Find Paystack's test M-Pesa number on its test-payments page (paystack.com/docs/payments/test-payments, under mobile money for Kenya). If you can't open the page, ask Joe to copy the number from it. Don't guess a number.
  2. Run it in E.164 format first, e.g. `+2547…`:
     ```bash
     .venv/bin/python scripts/record_paystack_fixtures.py --phone '+2547XXXXXXXX'
     ```
  3. If Paystack rejects the phone format, run it again with the `07…` form.
  4. Stop when `billing/tests/fixtures/paystack/` contains `charge_request.json`, `charge_response.json`, `verify_success.json` and `webhook_charge_success.json`.
  5. If the charge never reaches `success`, or Paystack refuses every format, **stop and report the exact output to Joe.** Don't write the provider against guessed payloads.

- [ ] **Step 3: Answer the spec's three open questions from the fixtures.** Write the answers into the docstring of `billing/providers/paystack.py` (Step 5):
  1. Which phone format worked: `+254…` or `07…`. If `07…` worked, use `"0" + phone[3:]` in `paystack_phone` and `"0712345678"` in its test.
  2. Which key holds the M-Pesa receipt in `verify_success.json` `data`. The code below reads `receipt_number`. If the receipt (a 10-character code like `QJK1ABC2DE`) is under a different key, use that key in `_event()` and in the test's expected value. If test mode has no receipt at all, say so in the docstring and keep `receipt_number`.
  3. What `fees` holds. The code assumes cents, like `amount`. If it's missing or in another unit, adjust `_event()` and the test.

- [ ] **Step 4: Write the failing tests.** Create `billing/tests/test_paystack.py`:

```python
import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from billing.models import Payment
from billing.providers import CallbackRejected, ProviderError, get_provider
from billing.providers.paystack import PaystackProvider, paystack_phone

FIXTURES = Path(__file__).parent / "fixtures" / "paystack"
SECRET = "sk_test_subra"  # the key in subra.test_settings


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider(session=None, secret=SECRET):
    return PaystackProvider(secret_key=secret, session=session or FakeSession([]))


ORDER = SimpleNamespace(pk=42, price_kes=200, public_id="5b7f0c1e-0000-4000-8000-000000000042",
                        user=SimpleNamespace(email="wanjiku@example.co.ke"))


def signed(payload, secret=SECRET):
    body = json.dumps(payload).encode()
    return body, {"X-Paystack-Signature": hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()}


# --- starting a charge ------------------------------------------------------

def test_initiate_sends_an_mpesa_charge():
    session = FakeSession([FakeResponse(200, load("charge_response.json")["body"])])
    result = provider(session).initiate(ORDER, "254712345678")
    method, url, kw = session.requests[0]
    assert (method, url) == ("POST", "https://api.paystack.co/charge")
    assert kw["headers"]["Authorization"] == f"Bearer {SECRET}"
    body = kw["json"]
    assert body["amount"] == 20000 and body["currency"] == "KES" and body["email"] == "wanjiku@example.co.ke"
    assert body["mobile_money"] == {"phone": paystack_phone("254712345678"), "provider": "mpesa"}
    assert body["reference"].startswith("subra-42-")
    assert result.provider_ref == result.reference == body["reference"]


def test_phone_numbers_are_sent_in_the_format_paystack_accepted():
    assert paystack_phone("254712345678") == "+254712345678"


def test_initiate_error_raises_provider_error():
    session = FakeSession([FakeResponse(400, {"status": False, "message": "Invalid phone number"})])
    with pytest.raises(ProviderError) as exc:
        provider(session).initiate(ORDER, "254712345678")
    assert "Invalid phone number" in str(exc.value)


def test_an_immediately_failed_charge_raises_provider_error():
    session = FakeSession([FakeResponse(200, {"status": True, "data": {"status": "failed",
                                                                        "gateway_response": "Declined"}})])
    with pytest.raises(ProviderError) as exc:
        provider(session).initiate(ORDER, "254712345678")
    assert "Declined" in str(exc.value)


def test_network_errors_raise_provider_error():
    with pytest.raises(ProviderError):
        provider(FakeSession([requests.ConnectionError("no route")])).initiate(ORDER, "254712345678")


# --- status checks ----------------------------------------------------------

def test_check_status_reads_a_successful_charge():
    verify = load("verify_success.json")
    data = verify["data"]
    session = FakeSession([FakeResponse(200, verify)])
    event = provider(session).check_status(data["reference"])
    method, url, _ = session.requests[0]
    assert (method, url) == ("GET", f"https://api.paystack.co/transaction/verify/{data['reference']}")
    assert (event.state, event.provider_ref) == ("paid", data["reference"])
    assert event.amount == Decimal(data["amount"]) / 100
    assert event.fee == (Decimal(data["fees"]) / 100 if data.get("fees") is not None else None)
    assert event.receipt == str(data.get("receipt_number") or "")


@pytest.mark.parametrize("status, state", [("success", "paid"), ("failed", "failed"), ("abandoned", "failed"),
                                           ("reversed", "failed"), ("ongoing", "pending"), ("pending", "pending")])
def test_check_status_maps_paystack_statuses(status, state):
    session = FakeSession([FakeResponse(200, {"status": True, "data": {"status": status,
                                                                        "reference": "subra-42-x", "amount": 20000}})])
    assert provider(session).check_status("subra-42-x").state == state


def test_check_status_server_error_is_provider_error():
    with pytest.raises(ProviderError):
        provider(FakeSession([FakeResponse(500, {})])).check_status("subra-42-x")


# --- webhooks ---------------------------------------------------------------

def test_signed_charge_success_becomes_a_paid_event():
    payload = load("webhook_charge_success.json")
    body, headers = signed(payload)
    event = provider().handle_callback(body, headers)
    assert (event.state, event.provider_ref) == ("paid", payload["data"]["reference"])
    assert event.amount == Decimal(payload["data"]["amount"]) / 100


@pytest.mark.parametrize("headers", [{}, {"X-Paystack-Signature": "0" * 128}])
def test_unsigned_or_wrongly_signed_webhooks_are_rejected(headers):
    body = json.dumps(load("webhook_charge_success.json")).encode()
    with pytest.raises(CallbackRejected):
        provider().handle_callback(body, headers)


def test_webhooks_are_rejected_without_a_secret_key():
    body, headers = signed(load("webhook_charge_success.json"), secret="")
    with pytest.raises(CallbackRejected):
        provider(secret="").handle_callback(body, headers)


def test_signed_but_malformed_webhooks_are_value_errors():
    body = b"{not json"
    headers = {"X-Paystack-Signature": hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()}
    with pytest.raises(ValueError):
        provider().handle_callback(body, headers)


def test_other_events_are_ignored():
    body, headers = signed({"event": "transfer.success", "data": {"reference": "TRF-1"}})
    assert provider().handle_callback(body, headers).state == "pending"


def test_paystack_is_the_default_provider_and_knows_whether_it_has_keys(settings):
    assert get_provider().name == "paystack" and get_provider().configured
    settings.PAYSTACK_SECRET_KEY = ""
    assert not get_provider().configured


# --- the webhook endpoint, end to end ----------------------------------------

def post_webhook(client, body, signature):
    return client.post("/billing/webhooks/paystack/", data=body, content_type="application/json",
                       HTTP_X_PAYSTACK_SIGNATURE=signature)


def test_webhook_endpoint_marks_a_paystack_payment_paid(client, quoted_order):
    payload = load("webhook_charge_success.json")
    data = payload["data"]
    payment = Payment.objects.create(order=quoted_order, provider="paystack", provider_ref=data["reference"],
                                     reference=data["reference"], phone="254712345678",
                                     amount_kes=int(data["amount"]) // 100)
    body, headers = signed(payload)
    assert post_webhook(client, body, headers["X-Paystack-Signature"]).status_code == 200
    payment.refresh_from_db()
    assert payment.status == "paid" and payment.raw_callbacks[0]["event"] == "charge.success"


def test_webhook_endpoint_rejects_a_bad_signature(client, db):
    assert post_webhook(client, b'{"event": "charge.success", "data": {}}', "nope").status_code == 403


def test_webhook_for_a_charge_we_never_made_is_accepted_and_ignored(client, db):
    body, headers = signed({"event": "charge.success",
                            "data": {"reference": "someone-else-1", "status": "success", "amount": 5000}})
    assert post_webhook(client, body, headers["X-Paystack-Signature"]).status_code == 200
    assert not Payment.objects.exists()
```

Add to `orders/tests/test_views.py`:
```python
def test_simulated_payment_needs_debug_and_no_provider_keys(settings):
    from orders.views import _dev_payment_enabled

    settings.DEBUG, settings.PAYMENT_PROVIDER, settings.PAYSTACK_SECRET_KEY = True, "paystack", ""
    assert _dev_payment_enabled()
    settings.PAYSTACK_SECRET_KEY = "sk_test_x"
    assert not _dev_payment_enabled()
    settings.DEBUG, settings.PAYSTACK_SECRET_KEY = False, ""
    assert not _dev_payment_enabled()
```

- [ ] **Step 5: Run them and check they fail.**

Run: `.venv/bin/python -m pytest billing/tests/test_paystack.py orders/tests/test_views.py -q`
Expected: `ModuleNotFoundError: No module named 'billing.providers.paystack'`.

- [ ] **Step 6: Implement the provider.** Create `billing/providers/paystack.py`. Fill in the docstring's "Confirmed in test mode" line with what Step 3 found.

```python
"""Paystack M-Pesa charges (Amendment 2). https://paystack.com/docs/api/charge/

POST /charge with mobile_money {phone, provider: "mpesa"} sends the STK push. Paystack identifies a
transaction by the reference we send, so that reference is also our provider_ref. GET
/transaction/verify/<reference> reports the outcome. Webhooks are signed: X-Paystack-Signature is the
HMAC-SHA512 of the raw body, keyed with the secret key. Amounts and fees are in cents.

Confirmed in test mode on <date> (fixtures in billing/tests/fixtures/paystack/): phone format <...>;
M-Pesa receipt in data.<key> <or "not present in test mode">; fees <in cents | ...>.
"""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from . import CallbackRejected, InitResult, PaymentEvent, ProviderError

API = "https://api.paystack.co"
PAID = {"success"}
FAILED = {"failed", "abandoned", "reversed"}


def paystack_phone(phone):
    """Our 2547XXXXXXXX form, in the format Paystack's charge API accepted in test mode."""
    return "+" + phone


def _kes(cents):
    if cents in (None, ""):
        return None
    try:
        return (Decimal(str(cents)) / 100).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


class PaystackProvider:
    name = "paystack"

    def __init__(self, secret_key, session=None, timeout=30):
        self.secret_key = secret_key
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_settings(cls):
        return cls(secret_key=settings.PAYSTACK_SECRET_KEY)

    @property
    def configured(self):
        return bool(self.secret_key)

    def _request(self, method, path, body=None):
        try:
            resp = self.session.request(method, API + path, json=body, timeout=self.timeout,
                                        headers={"Authorization": f"Bearer {self.secret_key}"})
        except requests.RequestException as e:
            raise ProviderError(f"Paystack unreachable: {e}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or data.get("status") is False:
            raise ProviderError(f"Paystack error {resp.status_code}: {data.get('message') or resp.text[:200]}")
        return data

    def initiate(self, order, phone):
        reference = f"subra-{order.pk}-{uuid.uuid4().hex[:8]}"
        data = self._request("POST", "/charge", {
            "email": order.user.email,
            "amount": int(order.price_kes) * 100,
            "currency": "KES",
            "reference": reference,
            "mobile_money": {"phone": paystack_phone(phone), "provider": "mpesa"},
            "metadata": {"order": str(order.public_id)},
        })
        charge = data.get("data") or {}
        if charge.get("status") in FAILED:
            raise ProviderError(f"Paystack refused the charge: {charge.get('gateway_response') or data.get('message')}")
        return InitResult(provider_ref=reference, reference=reference, raw=data)

    def check_status(self, ref):
        data = self._request("GET", f"/transaction/verify/{ref}")
        return self._event(data.get("data") or {}, raw=data, fallback_ref=ref)

    def handle_callback(self, body, headers):
        signature = headers.get("X-Paystack-Signature") or ""
        expected = hmac.new(self.secret_key.encode(), body, hashlib.sha512).hexdigest()
        if not self.secret_key or not hmac.compare_digest(signature, expected):
            raise CallbackRejected("Paystack webhook signature mismatch")
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError("Not a Paystack event")
        data = payload["data"]
        if payload.get("event") != "charge.success":
            return PaymentEvent(provider_ref=str(data.get("reference") or ""), state="pending", raw=payload)
        return self._event(data, raw=payload)

    def _event(self, data, raw, fallback_ref=""):
        status = str(data.get("status") or "")
        state = "paid" if status in PAID else "failed" if status in FAILED else "pending"
        return PaymentEvent(
            provider_ref=str(data.get("reference") or fallback_ref),
            state=state,
            amount=_kes(data.get("amount")),
            receipt=str(data.get("receipt_number") or ""),
            fee=_kes(data.get("fees")),
            code=status,
            reason=str(data.get("gateway_response") or ""),
            reference=str(data.get("reference") or ""),
            raw=raw,
        )
```

In `billing/providers/__init__.py`, add `configured: bool` under `name: str` in `PaymentProvider`, and add Paystack first in `get_provider`:
```python
    if name == "paystack":
        from .paystack import PaystackProvider

        return PaystackProvider.from_settings()
```
Change the module docstring's "(build plan, Amendment 1A)" to "(build plan, Amendments 1A and 2)".

In `billing/providers/intasend.py`, add to `IntaSendProvider`:
```python
    @property
    def configured(self):
        return bool(self.secret_key and self.publishable_key)
```

In `billing/views.py`: `WEBHOOK_PROVIDERS = {"paystack", "intasend"}`.

In `orders/views.py`:
- Import `get_provider`: `from billing.providers import PaymentEvent, get_provider`.
- Add the helper below `_local_phone`:
  ```python
  def _dev_payment_enabled():
      """The simulated-payment button: DEBUG only, and only while the payment provider has no keys."""
      return settings.DEBUG and not get_provider().configured
  ```
- Use it in two places:
  - in `detail`: `"dev_payment": _dev_payment_enabled(),`
  - in `dev_pay`: `if not _dev_payment_enabled():`
- Change `dev_pay`'s docstring to "DEBUG only, while the payment provider has no keys: pretend M-Pesa confirmed the payment."

In `subra/settings.py`, replace the payments block and the fee line:
```python
PAYMENT_FEE_RATE = float(env("PAYMENT_FEE_RATE", "0.015"))  # estimate when the provider reports no fee
```
```python
# Payments (Amendment 2): Paystack Starter Business at launch; IntaSend and Daraja are alternatives.
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", "paystack")
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", "")  # sk_test_... or sk_live_...; also verifies webhooks
INTASEND_ENV = env("INTASEND_ENV", "sandbox")  # sandbox | live
INTASEND_PUBLISHABLE_KEY = env("INTASEND_PUBLISHABLE_KEY", "")
INTASEND_SECRET_KEY = env("INTASEND_SECRET_KEY", "")
INTASEND_WEBHOOK_SECRET = env("INTASEND_WEBHOOK_SECRET", "")  # the "challenge" set in the IntaSend dashboard
```

In `subra/test_settings.py`, add `PAYSTACK_SECRET_KEY = "sk_test_subra"`.

In `.env.example`, replace the whole payments section:
```
# --- Payments: M-Pesa via Paystack (Starter Business) ---
PAYMENT_PROVIDER=paystack
# Test key first (sk_test_...), the live key once Starter KYC is approved. It also verifies webhooks.
# Webhook URL to set in the Paystack dashboard: https://<your domain>/billing/webhooks/paystack/
PAYSTACK_SECRET_KEY=
# Used only when the provider doesn't report its fee (Paystack M-Pesa: 1.5%)
PAYMENT_FEE_RATE=0.015

# --- Alternative: IntaSend (needs a registered business); set PAYMENT_PROVIDER=intasend ---
INTASEND_ENV=sandbox
INTASEND_PUBLISHABLE_KEY=
INTASEND_SECRET_KEY=
# The "challenge" you set on the webhook in the IntaSend dashboard.
# Webhook URL to register there: https://<your domain>/billing/webhooks/intasend/
INTASEND_WEBHOOK_SECRET=
```

- [ ] **Step 7: Run the tests.**

Run: `.venv/bin/python -m pytest billing/tests/test_paystack.py -q && .venv/bin/python -m pytest -q`
Expected: all pass. If a fixture-driven assertion fails, the fixture is the truth. Fix `_event()` or `paystack_phone()`, not the fixture.

- [ ] **Step 8: Commit.** Check first that no key leaked into the fixtures:
```bash
grep -rl "sk_test_\|sk_live_" billing/tests/fixtures && echo "STOP: a key is in a fixture" || echo "fixtures clean"
```
Expected: `fixtures clean`.
```bash
git add scripts/record_paystack_fixtures.py billing subra/settings.py subra/test_settings.py .env.example orders/views.py orders/tests/test_views.py
git commit -m "$(printf 'Add Paystack as the default payment provider, built from test-mode payloads\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 4: Generate first, pay to download

This is the core switch. The status changes run through orders, billing, views and templates together. **The suite is red from Step 3 until Step 11.** That's expected, and the task ends green.

**Files:**
- Modify:
  - `subra/settings.py`, `.env.example`
  - `orders/models.py`, `orders/services.py`, `orders/processing.py`, `orders/tasks.py`, `orders/views.py`, `orders/urls.py`, `orders/admin.py`
  - `billing/models.py`, `billing/services.py`, `billing/admin.py`
  - `templates/orders/detail.html`, `templates/orders/_status.html`, `templates/orders/_order_row.html`
  - `static/css/app.css`
- Create:
  - `orders/migrations/0004_pay_after_generation.py`, `orders/migrations/0005_map_old_statuses.py`, `billing/migrations/0004_pay_after_generation.py`
  - `templates/orders/_discard.html`
  - `orders/tests/test_lifecycle.py`, `orders/tests/test_admin.py`
- Rewrite: `conftest.py`, `orders/tests/test_processing.py`, `orders/tests/test_views.py`, `billing/tests/test_services.py`

**Interfaces:**
- Consumes: `PolishResult.plan` (Task 1), `handle_callback(body, headers)` (Task 2), `_dev_payment_enabled()` and Paystack settings (Task 3).
- Produces:
  - `Order.Status`: `QUOTED`, `QUEUED`, `PROCESSING`, `READY`, `RERENDERING`, `FAILED`, `EXPIRED`.
  - Class attributes `Order.UNPAID_WORK` and `Order.IN_FLIGHT`.
  - Fields `Order.paid_at`, `Order.plan`, `Order.switch_error`.
  - `Order.log(note)`.
  - Properties `Order.is_paid`, `Order.has_pending_payment`, `Order.can_pay` and `Order.can_discard`.
  - `orders.services`: `GenerationError(message, blocking_order=None)`, `unpaid_work(user, exclude=None)`, `start_generation(order, theme=None) -> Order`, `discard_order(order) -> Order`, `set_theme(order, theme)`.
  - `orders.processing`:
    - `run_polish(order)`
    - `fetch_original(order, tmp) -> Path`
    - `make_previews(order, out, tmp) -> list[Path]`
    - `store_result(order, out, previews) -> list[str]` (the replaced names)
    - `delete_files(names)`
    - `unpaid_expiry()`
    - `fail(order, reason)`
    - `log_attempt(order, record)`
  - `orders.tasks.IN_FLIGHT`.
  - `Payment.Status.REFUNDED`, `Payment.refunded_at`.
  - Views `orders:polish` and `orders:discard`; `orders.views._poll_key(order, last_payment)`.
  - Conftest fixtures: `order_factory`, `quoted_order`, `fake_previews`, `ready_order`, `paid_order`, `confirm_payment`.

- [ ] **Step 1: Settings and shared fixtures.**

In `subra/settings.py`, below `FILE_RETENTION_DAYS`:
```python
UNPAID_RETENTION_HOURS = int(env("UNPAID_RETENTION_HOURS", "48"))  # quoted, unpaid and failed decks (Amendment 2)
```
In `.env.example`, below `FILE_RETENTION_DAYS=7`, add `UNPAID_RETENTION_HOURS=48`. Change the line above them to `# Paid decks are kept FILE_RETENTION_DAYS after payment; unpaid ones UNPAID_RETENTION_HOURS.`

Replace `conftest.py`:
```python
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "tests"))  # deckbuilder, lockedbuilder


@pytest.fixture(autouse=True)
def _clear_cache():
    """Rate-limit counters live in the cache; don't let them leak between tests."""
    from django.core.cache import cache

    cache.clear()
    yield


@pytest.fixture
def user(db):
    from accounts.models import User

    return User.objects.create_user("wanjiku@example.co.ke", "a-strong-password-1")


@pytest.fixture
def theme(db):
    from orders.models import Theme

    return Theme.objects.get(slug="navy")


@pytest.fixture
def order_factory(db, user, theme):
    """Orders without files (KES 350, 20 slides), for status and payment rules. status= starts elsewhere."""
    from billing.models import PriceTier
    from orders.models import Order

    tier = PriceTier.objects.get(name="Standard")

    def make(name="Q3 review.pptx", status=Order.Status.QUOTED, owner=None, **fields):
        fields.setdefault("price_kes", tier.price_kes)
        order = Order.objects.create(user=owner or user, original_name=name, slide_count=20, tier=tier, theme=theme,
                                     **fields)
        if status != Order.Status.QUOTED:
            Order.objects.filter(pk=order.pk).update(status=status)
            order.refresh_from_db()
        return order

    return make


@pytest.fixture
def quoted_order(order_factory):
    return order_factory()


@pytest.fixture
def fake_previews(monkeypatch):
    """LibreOffice is slow; fake two thumbnails per render."""
    from orders import processing

    def fake(pptx, out_dir, dpi=60):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in (1, 2):
            p = out_dir / f"slide-{i}.png"
            p.write_bytes(b"\x89PNG fake")
            paths.append(p)
        return paths

    monkeypatch.setattr(processing, "render_previews", fake)


def _deck_upload(path, name="Q3 review.pptx"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, Path(path).read_bytes(),
                              content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@pytest.fixture
def ready_order(user, tmp_path, fake_previews):
    """A rich 7-slide deck (KES 200), generated with the heuristic planner and not yet paid for."""
    from deckbuilder import build_rich_deck
    from orders import services, tasks
    from orders.models import Order

    order = services.create_order(user, _deck_upload(build_rich_deck(tmp_path / "rich.pptx")))
    services.start_generation(order)
    tasks.process_order(order.pk)
    order.refresh_from_db()
    assert order.status == Order.Status.READY, order.failure_reason
    return order


def _confirm_payment(order, ref="INV-1", fee="3.00"):
    """Record a confirmed M-Pesa payment for the order, the way a provider webhook would."""
    from billing import services
    from billing.models import Payment
    from billing.providers import PaymentEvent

    Payment.objects.create(order=order, provider="intasend", provider_ref=ref, phone="254712345678",
                           amount_kes=order.price_kes)
    services.apply_event("intasend", PaymentEvent(provider_ref=ref, state="paid", amount=Decimal(order.price_kes),
                                                  receipt="SJK1ABC2DE", fee=Decimal(fee), raw={}), source="callback")
    order.refresh_from_db()
    return order


@pytest.fixture
def confirm_payment(db):
    return _confirm_payment


@pytest.fixture
def paid_order(ready_order):
    return _confirm_payment(ready_order)
```

- [ ] **Step 2: Write the lifecycle tests.** Create `orders/tests/test_lifecycle.py`:

```python
from datetime import timedelta

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone

from accounts.models import User
from billing.models import Payment
from orders import services
from orders.models import InvalidTransition, Order, Theme

S = Order.Status

ALLOWED = {
    (S.QUOTED, S.QUEUED), (S.QUOTED, S.EXPIRED),
    (S.QUEUED, S.PROCESSING),
    (S.PROCESSING, S.READY), (S.PROCESSING, S.FAILED), (S.PROCESSING, S.QUEUED),
    (S.READY, S.RERENDERING), (S.READY, S.EXPIRED),
    (S.RERENDERING, S.READY),
    (S.FAILED, S.EXPIRED),
}


@pytest.fixture
def no_enqueue(monkeypatch):
    queued = []
    monkeypatch.setattr(services, "_enqueue", lambda order: queued.append(order.pk))
    return queued


@pytest.mark.parametrize("start", list(S))
@pytest.mark.parametrize("end", list(S))
def test_only_the_designed_transitions_are_allowed(order_factory, start, end):
    order = order_factory(status=start)
    if (start, end) in ALLOWED:
        order.transition(end)
        assert order.status == end
    else:
        with pytest.raises(InvalidTransition):
            order.transition(end)


def test_a_new_order_is_quoted_unpaid_and_kept_48_hours(quoted_order):
    assert quoted_order.status == S.QUOTED and not quoted_order.is_paid
    assert timedelta(hours=47) < quoted_order.expires_at - timezone.now() <= timedelta(hours=48)


def test_generation_queues_the_deck_in_the_chosen_theme(quoted_order, no_enqueue,
                                                         django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        services.start_generation(quoted_order, theme=Theme.objects.get(slug="slate"))
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.QUEUED and quoted_order.theme.slug == "slate"
    assert no_enqueue == [quoted_order.pk]


def test_generation_starts_once(quoted_order, no_enqueue):
    services.start_generation(quoted_order)
    with pytest.raises(services.GenerationError):
        services.start_generation(quoted_order)


@pytest.mark.parametrize("status", [S.QUEUED, S.PROCESSING, S.READY, S.RERENDERING])
def test_an_unpaid_deck_blocks_a_second_one(order_factory, no_enqueue, status):
    first = order_factory("First.pptx", status=status)
    second = order_factory("Second.pptx")
    with pytest.raises(services.GenerationError) as exc:
        services.start_generation(second)
    assert exc.value.message == "Pay for or discard First.pptx first."
    assert exc.value.blocking_order == first
    second.refresh_from_db()
    assert second.status == S.QUOTED


def test_paying_for_the_first_deck_unblocks_the_next(order_factory, no_enqueue):
    order_factory("First.pptx", status=S.READY, paid_at=timezone.now())
    services.start_generation(order_factory("Second.pptx"))


@pytest.mark.parametrize("status", [S.QUOTED, S.FAILED, S.EXPIRED])
def test_decks_without_unpaid_ai_work_dont_block(order_factory, no_enqueue, status):
    order_factory("Other.pptx", status=status)
    services.start_generation(order_factory("New.pptx"))


def test_another_customers_unpaid_deck_doesnt_block(order_factory, no_enqueue):
    other = User.objects.create_user("other@example.com", "another-password-9")
    order_factory("Theirs.pptx", status=S.READY, owner=other)
    services.start_generation(order_factory("Mine.pptx"))


def test_discarding_deletes_the_files_at_once(ready_order):
    names = [ready_order.original_file.name, ready_order.result_file.name, *ready_order.preview_images]
    assert all(default_storage.exists(n) for n in names)
    services.discard_order(ready_order)
    ready_order.refresh_from_db()
    assert ready_order.status == S.EXPIRED and ready_order.files_deleted_at
    assert not any(default_storage.exists(n) for n in names)
    assert ready_order.events.last().note == "Discarded by customer"


@pytest.mark.parametrize("status", [S.QUOTED, S.FAILED])
def test_quoted_and_failed_decks_can_be_discarded(order_factory, status):
    assert services.discard_order(order_factory(status=status)).status == S.EXPIRED


@pytest.mark.parametrize("status", [S.QUEUED, S.PROCESSING, S.RERENDERING, S.EXPIRED])
def test_decks_in_flight_or_gone_cant_be_discarded(order_factory, status):
    with pytest.raises(ValueError):
        services.discard_order(order_factory(status=status))


def test_paid_decks_cant_be_discarded(order_factory):
    with pytest.raises(ValueError):
        services.discard_order(order_factory(status=S.READY, paid_at=timezone.now()))


def test_a_deck_with_a_pending_payment_cant_be_discarded(order_factory):
    order = order_factory(status=S.READY)
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350)
    with pytest.raises(ValueError):
        services.discard_order(order)
```

Run: `.venv/bin/python -m pytest orders/tests/test_lifecycle.py -q`
Expected: errors such as `AttributeError: QUEUED` and missing `GenerationError`.

- [ ] **Step 3: Models and migrations.**

In `billing/models.py`, add `REFUNDED = "refunded", "Refunded"` to `Payment.Status`. Add the field below `status`:
```python
    refunded_at = models.DateTimeField(null=True, blank=True)
```
and change the class docstring to "One payment attempt through a provider (Paystack at launch). Provider-neutral fields."

In `orders/models.py`:
- Change the billing import to `from billing.models import Payment, PriceTier, ProductType`.
- Change `AIJob.run`'s help text to `"1 = the generation; theme switches make no AI calls"`.
- Replace the `Order` class from `class Order(models.Model):` down to (not including) `LOCKED_WORDS = {` with:

```python
class Order(models.Model):
    """Processing status and payment are separate tracks (Amendment 2): customers see the deck, then pay."""

    class Status(models.TextChoices):
        QUOTED = "quoted", "Ready to polish"
        QUEUED = "queued", "In queue"
        PROCESSING = "processing", "Polishing"
        READY = "ready", "Ready"
        RERENDERING = "rerendering", "Switching theme"
        FAILED = "failed", "Couldn't polish"
        EXPIRED = "expired", "Expired (files deleted)"

    S = Status
    TRANSITIONS = {
        S.QUOTED: {S.QUEUED, S.EXPIRED},
        S.QUEUED: {S.PROCESSING},
        S.PROCESSING: {S.READY, S.FAILED, S.QUEUED},  # back to QUEUED = spend cap or AI outage
        S.READY: {S.RERENDERING, S.EXPIRED},
        S.RERENDERING: {S.READY},
        S.FAILED: {S.EXPIRED},
        S.EXPIRED: set(),
    }
    UNPAID_WORK = (S.QUEUED, S.PROCESSING, S.READY, S.RERENDERING)  # counts toward one unpaid deck per account
    IN_FLIGHT = (S.QUEUED, S.PROCESSING, S.RERENDERING)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    product_type = models.CharField(max_length=20, choices=ProductType.choices, default=ProductType.DECK_POLISH)
    original_file = models.FileField(upload_to=original_upload_path, blank=True)
    original_name = models.CharField(max_length=255)
    slide_count = models.PositiveIntegerField(default=0)
    tier = models.ForeignKey(PriceTier, on_delete=models.PROTECT, null=True, blank=True)
    price_kes = models.PositiveIntegerField(default=0)
    theme = models.ForeignKey(Theme, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUOTED)
    paid_at = models.DateTimeField(null=True, blank=True)
    plan = models.JSONField(null=True, blank=True)  # the verified layout plan; theme switches re-render it
    result_file = models.FileField(upload_to=result_upload_path, blank=True)
    preview_images = models.JSONField(default=list, blank=True)  # storage names, in slide order
    overflow_slides = models.JSONField(default=list, blank=True)
    locked_summary = models.JSONField(default=dict, blank=True)  # locked objects found at upload (Amendment 1C)
    switch_error = models.CharField(max_length=200, blank=True)
    waiting_for_capacity = models.BooleanField(default=False)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    files_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "expires_at"])]

    def __str__(self):
        return f"{self.original_name} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = timezone.now() + timedelta(hours=settings.UNPAID_RETENTION_HOURS)
        super().save(*args, **kwargs)

    def can_transition(self, to):
        return to in self.TRANSITIONS.get(self.status, set())

    def transition(self, to, note="", **fields):
        """Change status (and any extra fields) and log it. Raises InvalidTransition."""
        if not self.can_transition(to):
            raise InvalidTransition(f"{self.status} -> {to} is not allowed")
        from_status = self.status
        with transaction.atomic():
            self.status = to
            for k, v in fields.items():
                setattr(self, k, v)
            self.save()
            OrderEvent.objects.create(order=self, from_status=from_status, to_status=to, note=note[:500])

    def log(self, note):
        """Record an event that doesn't change the status, such as a payment."""
        OrderEvent.objects.create(order=self, from_status=self.status, to_status=self.status, note=note[:500])

    # --- payment ------------------------------------------------------------

    @property
    def is_paid(self):
        return self.paid_at is not None

    @property
    def has_pending_payment(self):
        return self.payments.filter(status=Payment.Status.PENDING).exists()

    @property
    def can_pay(self):
        return (self.status == self.Status.READY and not self.is_paid and not self.files_deleted_at
                and not self.has_pending_payment)

    @property
    def can_discard(self):
        if self.is_paid or self.files_deleted_at:
            return False
        if self.status in (self.Status.QUOTED, self.Status.FAILED):
            return True
        return self.status == self.Status.READY and not self.has_pending_payment

    # --- money --------------------------------------------------------------

    @property
    def ai_cost_usd(self):
        return sum((j.cost_usd for j in self.ai_jobs.all()), Decimal("0"))

    @property
    def ai_cost_kes(self):
        return sum((j.cost_kes for j in self.ai_jobs.all()), Decimal("0"))

    @property
    def payment_fees_kes(self):
        return sum((p.fee_kes for p in self.payments.filter(status="paid")), Decimal("0"))

    @property
    def margin_kes(self):
        """Price minus AI cost and payment provider fees (only meaningful for paid orders)."""
        return Decimal(self.price_kes) - self.ai_cost_kes - self.payment_fees_kes

```
Then delete the old `can_rerun` property at the end of the class. Keep `LOCKED_WORDS`, `locked_description`, `has_linked_objects` and `preview_caveats` as they are.

Generate the schema migrations. Use `--skip-checks`: at this point the admin and views still name the old statuses and `rerun_used`, and Django's system checks would refuse to run. Step 11 fixes them, and Step 12's test run covers them.
```bash
.venv/bin/python manage.py makemigrations --skip-checks --settings=subra.test_settings orders billing -n pay_after_generation
```
Expected: `orders/migrations/0004_pay_after_generation.py` and `billing/migrations/0004_pay_after_generation.py`. Between them they add `paid_at`, `plan` and `switch_error`, remove `rerun_used`, alter `status` and `AIJob.run`, add `refunded_at`, and alter `Payment.status`.

Create `orders/migrations/0005_map_old_statuses.py`:
```python
"""Amendment 2: map pay-first statuses onto the generate-first lifecycle, and fill in paid_at."""

from django.db import migrations

OLD_TO_NEW = {
    "uploaded": "quoted", "quoted": "quoted", "payment_pending": "quoted", "payment_failed": "quoted",
    "paid": "queued", "processing": "queued", "done": "ready",
    "failed": "failed", "refund_pending": "failed", "refunded": "failed", "expired": "expired",
}


def forwards(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Payment = apps.get_model("billing", "Payment")
    for order in Order.objects.all():
        old = order.status
        order.status = OLD_TO_NEW.get(old, old)
        first_paid = Payment.objects.filter(order_id=order.pk, status="paid").order_by("updated_at").first()
        if first_paid is not None:
            order.paid_at = first_paid.updated_at
        order.save(update_fields=["status", "paid_at"])
        if old == "refunded":
            Payment.objects.filter(order_id=order.pk, status="paid").update(status="refunded")


class Migration(migrations.Migration):
    dependencies = [("orders", "0004_pay_after_generation"), ("billing", "0004_pay_after_generation")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
```

- [ ] **Step 4: Order services.** Replace `orders/services.py`. The `create_order` body is unchanged apart from the event at the end.

```python
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.db import transaction

from billing.models import PriceTier, ProductType
from polisher.extract import extract_deck
from polisher.pipeline import UnsupportedContentError
from polisher.validate import MESSAGES, ValidationError, validate_file

from . import processing
from .models import Order, OrderEvent, Theme

S = Order.Status


class UploadError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class GenerationError(Exception):
    """Why a deck can't be sent for polishing, in words the customer sees."""

    def __init__(self, message, blocking_order=None):
        super().__init__(message)
        self.message = message
        self.blocking_order = blocking_order


def default_theme():
    return Theme.objects.filter(active=True).first()


def create_order(user, uploaded_file, product_type=ProductType.DECK_POLISH):
    """Validate an upload and create a QUOTED order. Nothing is charged or sent to AI here."""
    name = Path(uploaded_file.name).name[:255]
    hard_limit = max(settings.MAX_UPLOAD_BYTES, settings.MAX_UPLOAD_BYTES_MEDIA)
    if uploaded_file.size > hard_limit:
        raise UploadError(
            f"This file is {uploaded_file.size / 1024 / 1024:.1f} MB. The limit is "
            f"{settings.MAX_UPLOAD_BYTES // 1024 // 1024} MB, or {hard_limit // 1024 // 1024} MB for decks "
            "with video or audio."
        )
    with tempfile.TemporaryDirectory(prefix="subra-upload-") as tmp:
        path = Path(tmp) / ("upload" + Path(name).suffix.lower())
        with open(path, "wb") as out:
            for chunk in uploaded_file.chunks():
                out.write(chunk)
        try:
            info = validate_file(path, max_bytes=settings.MAX_UPLOAD_BYTES, max_slides=settings.MAX_SLIDES,
                                 max_bytes_media=settings.MAX_UPLOAD_BYTES_MEDIA)
            deck = extract_deck(path)
        except ValidationError as e:
            raise UploadError(e.message)
        except Exception:
            raise UploadError(MESSAGES["corrupt"])
        if deck.unsupported:
            raise UploadError(str(UnsupportedContentError(deck.unsupported)))
        tier = PriceTier.for_units(product_type, info.slide_count)
        if tier is None:
            raise UploadError("We don't have a price for a deck of this size yet.")
        with transaction.atomic():
            order = Order.objects.create(
                user=user, product_type=product_type, original_name=name, slide_count=info.slide_count,
                tier=tier, price_kes=tier.price_kes, theme=default_theme(),
                locked_summary=deck.locked_summary(),
            )
            with open(path, "rb") as f:
                order.original_file.save("original.pptx", File(f), save=True)
            OrderEvent.objects.create(order=order, to_status=S.QUOTED,
                                      note=f"{info.slide_count} slides, {tier.name} tier, KES {tier.price_kes}")
    return order


def set_theme(order, theme):
    """Before polishing only. Afterwards a theme change is a re-render."""
    if order.status != S.QUOTED:
        raise ValueError("The theme can only be set here before polishing")
    order.theme = theme
    order.save(update_fields=["theme", "updated_at"])


def unpaid_work(user, exclude=None):
    """The customer's deck that has used AI but isn't paid for yet, if any (one at a time)."""
    orders = Order.objects.filter(user=user, status__in=Order.UNPAID_WORK, paid_at__isnull=True)
    if exclude is not None:
        orders = orders.exclude(pk=exclude.pk)
    return orders.order_by("created_at").first()


def start_generation(order, theme=None):
    """Queue a quoted deck for polishing. Refused while the customer has another unpaid deck."""
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=order.user_id)  # one check at a time per customer
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != S.QUOTED:
            raise GenerationError("This deck has already been sent for polishing.")
        blocking = unpaid_work(order.user, exclude=order)
        if blocking is not None:
            raise GenerationError(f"Pay for or discard {blocking.original_name} first.", blocking_order=blocking)
        theme = theme or order.theme
        order.transition(S.QUEUED, note=f"Polishing in {theme.slug}", theme=theme)
        transaction.on_commit(lambda: _enqueue(order))
    return order


def discard_order(order):
    """Delete an unpaid deck's files now. Raises ValueError if it can't be discarded."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if not order.can_discard:
            raise ValueError("This deck can't be discarded")
        processing.delete_order_files(order)
        order.transition(S.EXPIRED, note="Discarded by customer")
    return order


def _enqueue(order):
    from .tasks import process_order

    process_order.delay(order.pk)
```

- [ ] **Step 5: Run the lifecycle tests.**

Run: `.venv/bin/python -m pytest orders/tests/test_lifecycle.py -q`
Expected: the tests that use `ready_order` still fail, because processing isn't updated yet. All the others pass.

- [ ] **Step 6: Rewrite the processing tests.** Replace `orders/tests/test_processing.py`:

```python
import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.core import mail
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from pptx import Presentation

from billing.models import Payment
from deckbuilder import build_rich_deck, build_simple_deck
from orders import processing, services, spend, tasks
from orders.models import AIJob, DailySpend, Order
from polisher.ai import Usage
from polisher.heuristic import plan_deck
from polisher.planner import PlanAttempt

S = Order.Status


@pytest.fixture(autouse=True)
def _previews(fake_previews):
    """Every test here fakes LibreOffice."""


def upload(path, name="Q3 review.pptx"):
    return SimpleUploadedFile(name, Path(path).read_bytes(),
                              content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@pytest.fixture
def queued_order(user, tmp_path):
    return services.start_generation(services.create_order(user, upload(build_rich_deck(tmp_path / "rich.pptx"))))


class CostlyPlanner:
    """Heuristic plans, but each attempt 'costs' like a Claude call."""

    def __init__(self, outcomes, model="claude-sonnet-5", cost=0.05):
        self.outcomes, self.model, self.cost = list(outcomes), model, cost

    def plan(self, deck, feedback=None):
        good = self.outcomes.pop(0) == "good"
        return PlanAttempt("claude", self.model, plan_deck(deck) if good else None, Usage(5000, 2000),
                           self.cost, 10, error=None if good else "unusable plan")


def use_planners(monkeypatch, planner, escalation=None):
    monkeypatch.setattr(processing, "build_planners", lambda: (planner, escalation))


# --- upload and quote -------------------------------------------------------

def test_upload_creates_a_quoted_order(user, tmp_path):
    order = services.create_order(user, upload(build_rich_deck(tmp_path / "d.pptx")))
    assert order.status == S.QUOTED
    assert (order.slide_count, order.tier.name, order.price_kes) == (7, "Small", 200)
    assert order.original_name == "Q3 review.pptx"
    assert default_storage.exists(order.original_file.name)
    assert order.theme.slug == "navy"
    assert order.events.get().note == "7 slides, Small tier, KES 200"


def test_upload_rejects_non_pptx(user, tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4")
    with pytest.raises(services.UploadError) as exc:
        services.create_order(user, upload(bad, "x.pdf"))
    assert ".pptx" in exc.value.message
    assert not Order.objects.exists()


def test_upload_rejects_decks_over_the_slide_limit(user, tmp_path, settings):
    settings.MAX_SLIDES = 5
    with pytest.raises(services.UploadError) as exc:
        services.create_order(user, upload(build_simple_deck(tmp_path / "d.pptx", n_slides=6)))
    assert "6 slides" in exc.value.message


def test_deck_with_locked_objects_is_quoted_with_a_summary(user, tmp_path):
    from lockedbuilder import build_locked_deck

    order = services.create_order(user, upload(build_locked_deck(tmp_path / "locked.pptx")))
    assert order.status == S.QUOTED
    summary = order.locked_summary
    assert summary["slides"] == 9 and summary["linked"] == 1 and summary["counts"]["video"] == 1
    assert order.locked_description.startswith("9 slides contain ")
    assert "embedded objects" in order.locked_description and "equations" in order.locked_description


def test_media_decks_get_the_larger_upload_limit(user, tmp_path, settings):
    from lockedbuilder import build_locked_deck

    settings.MAX_UPLOAD_BYTES, settings.MAX_UPLOAD_BYTES_MEDIA = 1000, 10_000_000
    order = services.create_order(user, upload(build_locked_deck(tmp_path / "locked.pptx")))
    assert order.status == S.QUOTED
    with pytest.raises(services.UploadError):
        services.create_order(user, upload(build_rich_deck(tmp_path / "rich.pptx")))


# --- generation -------------------------------------------------------------

def test_generation_produces_a_ready_deck_that_isnt_paid_for(queued_order):
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.READY, order.failure_reason
    assert not order.is_paid
    with default_storage.open(order.result_file.name, "rb") as f:
        assert len(Presentation(f).slides) == 7
    assert len(order.preview_images) == 2 and all(default_storage.exists(n) for n in order.preview_images)
    assert order.plan and len(order.plan["slides"]) == 7
    assert timedelta(hours=47) < order.expires_at - timezone.now() <= timedelta(hours=48)
    assert [e.to_status for e in order.events.all()][-2:] == [S.PROCESSING, S.READY]


def test_ai_cost_is_logged_per_attempt_with_fx(queued_order, monkeypatch, settings):
    settings.USD_KES_RATE, settings.CARD_FX_MARKUP = 100.0, 0.03
    use_planners(monkeypatch, CostlyPlanner(["bad", "good"]))
    tasks.process_order(queued_order.pk)
    jobs = list(AIJob.objects.filter(order=queued_order))
    assert [(j.attempt_number, j.model, j.input_tokens) for j in jobs] == [(1, "claude-sonnet-5", 5000),
                                                                          (2, "claude-sonnet-5", 5000)]
    assert float(jobs[0].cost_kes) == pytest.approx(0.05 * 100 * 1.03)
    assert jobs[0].verification_passed is None and jobs[1].verification_passed is True
    assert float(DailySpend.objects.get().total_cost_usd) == pytest.approx(0.10)
    assert float(Order.objects.get(pk=queued_order.pk).ai_cost_kes) == pytest.approx(0.10 * 103, abs=0.01)


def test_failed_generation_costs_the_customer_nothing_and_alerts_admins(queued_order, monkeypatch, settings):
    settings.USD_KES_RATE, settings.CARD_FX_MARKUP = 100.0, 0.03
    use_planners(monkeypatch, CostlyPlanner(["bad", "bad"]), CostlyPlanner(["bad"], model="claude-opus-5-5"))
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.FAILED and order.failure_reason
    assert not Payment.objects.exists()
    assert timedelta(hours=47) < order.expires_at - timezone.now() <= timedelta(hours=48)
    alert = next(m for m in mail.outbox if "failed to generate" in m.subject)
    assert "wanjiku@example.co.ke" in alert.body and "3 attempt(s)" in alert.body
    assert "AI cost: KES 15.45" in alert.body and "no refund" in alert.body


def test_spend_cap_keeps_orders_queued(queued_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    tasks.process_order(queued_order.pk)
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.QUEUED and order.waiting_for_capacity
    assert len([m for m in mail.outbox if "cap" in m.subject.lower()]) == 1  # alert once a day


def test_spend_cap_hit_mid_job_requeues(queued_order, monkeypatch, settings):
    settings.AI_DAILY_CAP_USD = 0.06
    use_planners(monkeypatch, CostlyPlanner(["bad", "good"], cost=0.07))
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.QUEUED and order.waiting_for_capacity
    assert AIJob.objects.filter(order=queued_order).count() == 1


def test_queued_orders_are_released_when_capacity_returns(queued_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    tasks.process_order(queued_order.pk)
    settings.AI_DAILY_CAP_USD = 5.0
    tasks.release_queued_orders()
    assert Order.objects.get(pk=queued_order.pk).status == S.READY


# --- file retention ---------------------------------------------------------

def test_expired_decks_are_really_deleted(ready_order):
    paths = [default_storage.path(n) for n in
             [ready_order.original_file.name, ready_order.result_file.name, *ready_order.preview_images]]
    assert all(os.path.exists(p) for p in paths)
    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert not any(os.path.exists(p) for p in paths)
    assert ready_order.status == S.EXPIRED and ready_order.files_deleted_at
    assert ready_order.preview_images == [] and not ready_order.result_file and not ready_order.original_file


@pytest.mark.parametrize("status", [S.QUEUED, S.PROCESSING, S.RERENDERING])
def test_decks_being_worked_on_keep_their_files(ready_order, status):
    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1), status=status)
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert default_storage.exists(ready_order.original_file.name) and not ready_order.files_deleted_at


def test_a_pending_payment_holds_off_deletion(ready_order):
    Payment.objects.create(order=ready_order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=200)
    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert ready_order.status == S.READY and not ready_order.files_deleted_at


def test_unexpired_decks_keep_their_files(ready_order):
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert not ready_order.files_deleted_at
```

Run: `.venv/bin/python -m pytest orders/tests/test_processing.py -q`
Expected: failures, because `process_order` still looks for `paid`.

- [ ] **Step 7: Processing and tasks.**

Replace everything in `orders/processing.py` from `def log_attempt` to the end of the file. Keep the imports, adding `import uuid`. Keep `build_planners` and `usd_to_kes` as they are.

```python
def log_attempt(order, record):
    AIJob.objects.create(
        order=order, attempt_number=record.number, planner=record.planner, model=record.model or "",
        input_tokens=record.usage.input_tokens, output_tokens=record.usage.output_tokens,
        cache_read_tokens=record.usage.cache_read_tokens, cache_write_tokens=record.usage.cache_write_tokens,
        cost_usd=Decimal(str(record.cost_usd)).quantize(Decimal("0.000001")), cost_kes=usd_to_kes(record.cost_usd),
        duration_ms=record.duration_ms, verification_passed=record.verification_passed,
        problems=record.problems[:50], repairs=len(record.repairs), error=record.error or "",
    )
    if record.cost_usd:
        spend.record_spend(record.cost_usd)


def download_name(order):
    stem = re.sub(r"[^\w\- ]+", "", Path(order.original_name).stem).strip() or "deck"
    return f"{stem} (polished).pptx"


def unpaid_expiry():
    return timezone.now() + timedelta(hours=settings.UNPAID_RETENTION_HOURS)


def run_polish(order):
    """Generate the deck. Returns the PolishResult; on success the order is READY and unpaid."""
    planner, escalation = build_planners()
    with tempfile.TemporaryDirectory(prefix="subra-job-") as tmp:
        tmp = Path(tmp)
        src, out = fetch_original(order, tmp), tmp / "polished.pptx"
        result = polish(
            src, out, theme=order.theme.slug, planner=planner, escalation=escalation,
            before_attempt=lambda number, p: spend.ensure_capacity() if p.model else None,
            on_attempt=lambda record: log_attempt(order, record),
        )
        if not result.ok:
            return result
        replaced = store_result(order, out, make_previews(order, out, tmp))
        order.transition(
            S.READY, note=f"Verified, {len(result.attempts)} attempt(s), ${result.total_cost_usd:.4f}",
            plan=result.plan, overflow_slides=result.overflow_slides, failure_reason="", expires_at=unpaid_expiry(),
        )
        delete_files(replaced)
    return result


def fetch_original(order, tmp):
    src = Path(tmp) / "original.pptx"
    with order.original_file.open("rb") as f, open(src, "wb") as dst:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            dst.write(chunk)
    return src


def make_previews(order, out, tmp):
    try:
        return render_previews(out, Path(tmp) / "previews", dpi=60)
    except PreviewError:
        log.exception("Preview rendering failed for order %s", order.pk)
        return []


def store_result(order, out, previews):
    """Put the polished file and its previews on the order; the caller's transition saves them.
    Returns the storage names they replace, to delete once the new ones are saved."""
    replaced = [n for n in [order.result_file.name, *order.preview_images] if n]
    with open(out, "rb") as f:
        order.result_file.save("polished.pptx", File(f), save=False)
    batch = uuid.uuid4().hex[:8]  # new names on every render, so a switch never overwrites the files it replaces
    names = []
    for i, png in enumerate(previews, start=1):
        with open(png, "rb") as f:
            names.append(default_storage.save(f"orders/{order.public_id}/preview-{batch}-{i:02d}.png", File(f)))
    order.preview_images = names
    return replaced


def delete_files(names):
    for name in names:
        if name:
            default_storage.delete(name)


def fail(order, reason):
    """Generation failed. Nothing was charged, so there's no refund; the admins hear about it anyway."""
    order.transition(S.FAILED, note=reason[:500], failure_reason=reason, expires_at=unpaid_expiry())
    alert_admins(
        f"Order {order.pk} failed to generate",
        f"Order {order.pk} ({order.original_name}, {order.slide_count} slides, {order.user.email}) could not be "
        f"polished after {order.ai_jobs.count()} attempt(s).\nReason: {reason}\n"
        f"AI cost: KES {order.ai_cost_kes:.2f}. The customer wasn't charged, so no refund is needed.",
    )


def delete_order_files(order):
    for name in [order.original_file.name, order.result_file.name, *order.preview_images]:
        if name:
            default_storage.delete(name)
    order.original_file = ""
    order.result_file = ""
    order.preview_images = []
    order.files_deleted_at = timezone.now()
```

Replace `orders/tasks.py`:
```python
import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from billing.models import Payment
from polisher.pipeline import PlannerUnavailable

from . import processing, spend
from .models import Order

log = logging.getLogger(__name__)
S = Order.Status
IN_FLIGHT = set(Order.IN_FLIGHT)  # never expired: work is waiting or running


@shared_task(bind=True, max_retries=12)
def process_order(self, order_id):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None or order.status != S.QUEUED:
            return "skipped"
        if spend.cap_reached():
            order.waiting_for_capacity = True
            order.save(update_fields=["waiting_for_capacity", "updated_at"])
            try:
                spend.ensure_capacity()  # sends the once-a-day alert
            except spend.SpendCapReached:
                pass
            return "queued"
        order.transition(S.PROCESSING, note="Generating", waiting_for_capacity=False)

    try:
        result = processing.run_polish(order)
    except spend.SpendCapReached:
        order.transition(S.QUEUED, note="Daily AI spend cap reached mid-job", waiting_for_capacity=True)
        return "queued"
    except PlannerUnavailable as e:
        order.transition(S.QUEUED, note=f"AI provider unavailable: {e}"[:500], waiting_for_capacity=True)
        if self.request.called_directly or self.request.is_eager:
            return "deferred"
        raise self.retry(exc=e, countdown=300)
    except Exception as e:
        log.exception("Processing crashed for order %s", order_id)
        order.refresh_from_db()
        processing.fail(order, f"Internal error: {type(e).__name__}: {e}")
        return "failed"

    if not result.ok:
        processing.fail(order, result.failure or "Verification failed")
        return "failed"
    return "done"


@shared_task
def release_queued_orders():
    """Start queued orders that were waiting (spend cap, AI outage) or whose task was lost."""
    if spend.cap_reached():
        return 0
    stale = timezone.now() - timedelta(minutes=10)
    waiting = Q(waiting_for_capacity=True) | Q(updated_at__lt=stale)
    ids = list(Order.objects.filter(waiting, status=S.QUEUED).values_list("pk", flat=True))
    for pk in ids:
        process_order.delay(pk)
    return len(ids)


@shared_task
def delete_expired_files():
    """Delete originals, results and previews once expires_at passes (48 hours unpaid, 7 days after payment)."""
    count = 0
    expired = (
        Order.objects.filter(expires_at__lte=timezone.now(), files_deleted_at__isnull=True)
        .exclude(status__in=IN_FLIGHT)
        .exclude(payments__status=Payment.Status.PENDING)  # wait for the payment sweep to settle it
        .distinct()
    )
    for order in expired:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            processing.delete_order_files(order)
            if order.can_transition(S.EXPIRED):
                order.transition(S.EXPIRED, note="Files deleted after retention period")
            else:
                order.save()
        count += 1
    return count
```

Run: `.venv/bin/python -m pytest orders/tests/test_processing.py orders/tests/test_lifecycle.py -q`
Expected: all pass.

- [ ] **Step 8: Rewrite the billing service tests.** Replace `billing/tests/test_services.py`:

```python
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from billing import services
from billing.models import Payment
from billing.providers import InitResult, PaymentEvent, ProviderError
from orders.models import Order

S = Order.Status


class FakeProvider:
    name = "intasend"

    def __init__(self, ref="INV-1", status=None, fail=False):
        self.ref, self.status, self.fail = ref, status, fail
        self.initiated = []

    def initiate(self, order, phone):
        if self.fail:
            raise ProviderError("down")
        self.initiated.append((order.pk, phone, order.price_kes))
        return InitResult(provider_ref=self.ref, reference=f"subra-{order.pk}-t",
                          raw={"invoice": {"invoice_id": self.ref}})

    def handle_callback(self, body, headers):
        return event(**json.loads(body))

    def check_status(self, ref):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


def event(ref="INV-1", state="paid", amount="350", receipt="SJK1ABC2DE", fee="15.75", code="", reason="",
          reference=""):
    return PaymentEvent(provider_ref=ref, state=state, amount=None if amount is None else Decimal(amount),
                        receipt=receipt, fee=None if fee is None else Decimal(fee), code=code, reason=reason,
                        reference=reference, raw={"state": state, "ref": ref})


@pytest.fixture(autouse=True)
def no_status_checks(monkeypatch):
    monkeypatch.setattr(services, "schedule_status_check", lambda payment: None)


@pytest.fixture
def ready(order_factory):
    """A generated, unpaid KES 350 deck (payment rules don't need its files)."""
    return order_factory(status=S.READY)


def pending_payment(order, ref="INV-1"):
    return services.start_payment(order, "0712 345 678", provider=FakeProvider(ref=ref))


def callback(**kw):
    return services.handle_callback("intasend", json.dumps(kw).encode(), {}, provider=FakeProvider())


# --- starting a payment -----------------------------------------------------

def test_start_payment_sends_stk_push_and_waits(ready):
    provider = FakeProvider()
    payment = services.start_payment(ready, "0712 345 678", provider=provider)
    assert provider.initiated == [(ready.pk, "254712345678", 350)]
    assert (payment.provider, payment.provider_ref, payment.reference) == ("intasend", "INV-1", f"subra-{ready.pk}-t")
    assert (payment.status, payment.amount_kes) == ("pending", 350)
    ready.refresh_from_db()
    assert ready.status == S.READY and not ready.is_paid
    assert ready.events.last().note == "M-Pesa prompt sent to 254712345678 via intasend"
    assert ready.user.mpesa_phone == "254712345678"


def test_start_payment_rejects_bad_phone(ready):
    with pytest.raises(services.PaymentError):
        services.start_payment(ready, "12345", provider=FakeProvider())


@pytest.mark.parametrize("status", [S.QUOTED, S.QUEUED, S.PROCESSING, S.RERENDERING, S.FAILED, S.EXPIRED])
def test_only_a_ready_deck_can_be_paid_for(order_factory, status):
    with pytest.raises(services.PaymentError):
        pending_payment(order_factory(status=status))


def test_a_paid_deck_cant_be_paid_for_again(order_factory):
    with pytest.raises(services.PaymentError):
        pending_payment(order_factory(status=S.READY, paid_at=timezone.now()))


def test_one_payment_at_a_time(ready):
    pending_payment(ready)
    with pytest.raises(services.PaymentError):
        pending_payment(ready, ref="INV-2")


def test_failed_push_creates_no_payment(ready):
    with pytest.raises(services.PaymentError):
        services.start_payment(ready, "0712345678", provider=FakeProvider(fail=True))
    assert not Payment.objects.exists()


# --- callbacks --------------------------------------------------------------

def test_paid_callback_unlocks_the_download_for_7_days(ready):
    pending_payment(ready)
    payment = callback(ref="INV-1")
    assert (payment.status, payment.provider_receipt) == ("paid", "SJK1ABC2DE")
    assert payment.fee_kes == Decimal("15.75") and not payment.fee_estimated
    assert payment.raw_callbacks == [{"state": "paid", "ref": "INV-1"}]
    ready.refresh_from_db()
    assert ready.is_paid and ready.status == S.READY
    assert timedelta(days=6, hours=23) < ready.expires_at - timezone.now() <= timedelta(days=7)
    assert ready.events.last().note == "Paid, M-Pesa receipt SJK1ABC2DE"


def test_missing_fee_is_estimated(ready, settings):
    settings.PAYMENT_FEE_RATE = 0.015
    pending_payment(ready)
    payment = callback(ref="INV-1", fee="0.00")
    assert payment.fee_kes == Decimal("5.25") and payment.fee_estimated


def test_duplicate_callback_changes_nothing(ready):
    pending_payment(ready)
    callback(ref="INV-1")
    ready.refresh_from_db()
    paid_at = ready.paid_at
    payment = callback(ref="INV-1", receipt="OTHER")
    assert payment.provider_receipt == "SJK1ABC2DE"
    assert payment.callback_count == 2 and len(payment.raw_callbacks) == 2
    ready.refresh_from_db()
    assert ready.paid_at == paid_at
    assert ready.events.filter(note__startswith="Paid").count() == 1
    assert mail.outbox == []


def test_pending_callback_changes_nothing(ready):
    pending_payment(ready)
    assert callback(ref="INV-1", state="pending").status == "pending"
    ready.refresh_from_db()
    assert not ready.is_paid


def test_failed_callback_leaves_the_deck_ready_to_pay_again(ready):
    pending_payment(ready)
    payment = callback(ref="INV-1", state="failed", code="1032", reason="Request cancelled by user")
    assert payment.status == "failed" and payment.result_code == "1032"
    ready.refresh_from_db()
    assert ready.status == S.READY and not ready.is_paid and ready.can_pay


def test_amount_mismatch_is_not_paid_and_alerts_admin(ready):
    pending_payment(ready)
    payment = callback(ref="INV-1", amount="1")
    assert payment.status == "failed"
    ready.refresh_from_db()
    assert not ready.is_paid
    assert "amount" in mail.outbox[0].body.lower()


def test_paid_event_without_an_amount_is_not_trusted(ready):
    pending_payment(ready)
    assert callback(ref="INV-1", amount=None).status == "pending"


def test_reference_mismatch_is_ignored(ready):
    pending_payment(ready)
    assert callback(ref="INV-1", reference="subra-999-x").status == "pending"


def test_unknown_ref_is_ignored(db):
    assert callback(ref="nope") is None


def test_late_success_after_a_timeout_still_pays(ready):
    payment = pending_payment(ready)
    services.check_payment(payment, provider=FakeProvider(status=event(state="failed", code="1037", reason="timeout")))
    payment = callback(ref="INV-1")
    ready.refresh_from_db()
    assert payment.status == "paid" and ready.is_paid


def test_second_successful_payment_is_flagged_for_refund(ready):
    first = pending_payment(ready, ref="INV-1")
    services.check_payment(first, provider=FakeProvider(status=event(state="failed", code="1037")))
    services.start_payment(ready, "0712345678", provider=FakeProvider(ref="INV-2"))
    callback(ref="INV-2", receipt="R2")
    dup = callback(ref="INV-1", receipt="R1")
    assert dup.status == "paid" and "refund" in dup.result_desc.lower()
    assert any("Duplicate" in m.subject and "refund" in m.subject.lower() for m in mail.outbox)


def test_payment_for_a_deleted_deck_is_flagged_for_refund(ready):
    pending_payment(ready)
    Order.objects.filter(pk=ready.pk).update(status=S.EXPIRED, files_deleted_at=timezone.now())
    payment = callback(ref="INV-1")
    assert payment.status == "paid" and "refund" in payment.result_desc.lower()
    ready.refresh_from_db()
    assert not ready.is_paid
    assert any("deleted deck" in m.subject for m in mail.outbox)


# --- status checks ----------------------------------------------------------

def test_status_check_pending_changes_nothing(ready):
    payment = pending_payment(ready)
    assert services.check_payment(payment, provider=FakeProvider(status=event(state="pending"))) == "pending"
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.status_checks == 1 and payment.callback_count == 0


def test_status_check_success_marks_paid(ready):
    payment = pending_payment(ready)
    assert services.check_payment(payment, provider=FakeProvider(status=event())) == "paid"
    ready.refresh_from_db()
    assert ready.is_paid


def test_status_check_outage_keeps_waiting(ready):
    payment = pending_payment(ready)
    assert services.check_payment(payment, provider=FakeProvider(status=ProviderError("down"))) == "pending"


# --- IntaSend webhook endpoint ----------------------------------------------

def intasend_payload(payment, **extra):
    data = {"invoice_id": payment.provider_ref, "state": "COMPLETE", "provider": "M-PESA", "charges": "15.75",
            "net_amount": "334.25", "currency": "KES", "value": "350.00", "api_ref": payment.reference,
            "mpesa_reference": "SJK1ABC2DE", "failed_reason": None, "failed_code": None,
            "challenge": "challenge-123"}
    data.update(extra)
    return data


def test_webhook_rejects_a_wrong_challenge(client, ready):
    payment = pending_payment(ready)
    resp = client.post("/billing/webhooks/intasend/", data=json.dumps(intasend_payload(payment, challenge="x")),
                       content_type="application/json")
    assert resp.status_code == 403
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.raw_callbacks == []


def test_webhook_marks_paid(client, ready):
    payment = pending_payment(ready)
    resp = client.post("/billing/webhooks/intasend/", data=json.dumps(intasend_payload(payment)),
                       content_type="application/json")
    assert resp.status_code == 200
    payment.refresh_from_db()
    assert (payment.status, payment.provider_receipt, payment.fee_kes) == ("paid", "SJK1ABC2DE", Decimal("15.75"))
    ready.refresh_from_db()
    assert ready.is_paid


def test_webhook_rejects_malformed_json(client, db):
    resp = client.post("/billing/webhooks/intasend/", data="{not json", content_type="application/json")
    assert resp.status_code == 400


def test_webhook_for_an_unknown_provider_is_404(client, db):
    assert client.post("/billing/webhooks/stripe/", data="{}", content_type="application/json").status_code == 404
```

Run: `.venv/bin/python -m pytest billing/tests/test_services.py -q`
Expected: failures, because `start_payment` still checks the old statuses and `_mark_paid` still moves the order to `paid`.

- [ ] **Step 9: Billing services.** In `billing/services.py`:
- Add the imports `from datetime import timedelta` and `from django.utils import timezone`.
- Delete `PAYABLE` and `enqueue_processing`.
- Replace `start_payment`, `_mark_failed` and `_mark_paid`:

```python
def start_payment(order, raw_phone, provider=None):
    try:
        phone = normalize_phone(raw_phone)
    except ValueError as e:
        raise PaymentError(str(e))
    provider = provider or get_provider()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if not order.can_pay:
            raise PaymentError("This deck can't be paid for right now.")
        try:
            result = provider.initiate(order, phone)
        except ProviderError as e:
            log.warning("Payment request failed for order %s: %s", order.pk, e)
            raise PaymentError("We couldn't reach M-Pesa. Please try again in a minute.")
        payment = Payment.objects.create(
            order=order, provider=provider.name, provider_ref=result.provider_ref, reference=result.reference,
            phone=phone, amount_kes=order.price_kes, raw_init=result.raw,
        )
        order.log(f"M-Pesa prompt sent to {phone} via {provider.name}")
        order.user.mpesa_phone = phone
        order.user.save(update_fields=["mpesa_phone"])
        transaction.on_commit(lambda: schedule_status_check(payment))
    return payment
```

```python
def _mark_failed(payment, code, desc):
    """Only the payment changes: the deck stays ready, and the customer can pay again."""
    payment.status = Payment.Status.FAILED
    payment.result_code, payment.result_desc = (code or "")[:32], (desc or "")[:255]
    payment.save()


def _mark_paid(payment, event):
    payment.status = Payment.Status.PAID
    payment.result_code, payment.result_desc = (event.code or "")[:32], (event.reason or "")[:255]
    if event.receipt:
        payment.provider_receipt = event.receipt
    payment.fee_kes, payment.fee_estimated = _fee(payment, event)
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    receipt = event.receipt or "unknown"
    if order.is_paid:
        payment.result_desc = f"Duplicate payment, refund needed. {event.reason}"[:255]
        payment.save()
        alert_admins(
            f"Duplicate M-Pesa payment on order {order.pk}: refund needed",
            f"Payment {payment.pk} from {payment.phone} (receipt {receipt}, KES {payment.amount_kes}) arrived for "
            f"order {order.pk}, which is already paid. Refund it from the provider dashboard, then mark the "
            "payment refunded in the admin.",
        )
        return
    if order.status not in (S.READY, S.RERENDERING) or order.files_deleted_at:
        payment.result_desc = f"Deck no longer exists, refund needed. {event.reason}"[:255]
        payment.save()
        alert_admins(
            f"Payment for a deleted deck on order {order.pk}: refund needed",
            f"Payment {payment.pk} from {payment.phone} (receipt {receipt}, KES {payment.amount_kes}) arrived for "
            f"order {order.pk}, which is {order.get_status_display().lower()} and has no deck to download. "
            "Refund it from the provider dashboard, then mark the payment refunded in the admin.",
        )
        return
    payment.save()
    now = timezone.now()
    order.paid_at = now
    order.expires_at = now + timedelta(days=settings.FILE_RETENTION_DAYS)
    order.save(update_fields=["paid_at", "expires_at", "updated_at"])
    order.log(f"Paid, M-Pesa receipt {event.receipt or '(from status check)'}")
```

In `billing/admin.py`, add `from django.contrib import admin, messages` and `from django.utils import timezone`. Add to `PaymentAdmin`:
```python
    actions = ["mark_refunded"]

    @admin.action(description="Mark refunded (after refunding via M-Pesa)")
    def mark_refunded(self, request, queryset):
        now = timezone.now()
        done = queryset.filter(status=Payment.Status.PAID).update(status=Payment.Status.REFUNDED, refunded_at=now,
                                                                  updated_at=now)
        self.message_user(request, f"{done} payment(s) marked refunded.",
                          messages.SUCCESS if done else messages.WARNING)
```

Run: `.venv/bin/python -m pytest billing/tests -q -k "not webhook"`
Expected: all pass. The webhook-endpoint tests are left out because they go through the URL config, which imports `orders/views.py`, and that still names the old statuses until Step 11. They pass at Step 12. `test_webhook_endpoint_marks_a_paystack_payment_paid` will pass then too: its quoted order takes the "deck no longer exists" branch, and the payment is still marked paid.

- [ ] **Step 10: Rewrite the view tests and add the admin tests.**

Replace `orders/tests/test_views.py`:
```python
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.models import User
from billing.models import Payment
from deckbuilder import build_rich_deck
from orders import services, tasks
from orders.models import Order

S = Order.Status


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def deck_bytes(tmp_path):
    return build_rich_deck(tmp_path / "rich.pptx").read_bytes()


def pptx(data, name="Board pack.pptx"):
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def page(client, order):
    return client.get(reverse("orders:detail", args=[order.public_id])).content.decode()


# --- home and upload --------------------------------------------------------

def test_home_for_visitors_shows_prices_and_signup(client, db):
    html = client.get("/").content.decode()
    assert "Upload a rough deck" in html and "KES 350" in html and reverse("signup") in html
    assert 'name="deck"' not in html


def test_home_for_customers_shows_the_upload_form(logged_in):
    assert 'name="deck"' in logged_in.get("/").content.decode()


def test_upload_requires_login(client, db, deck_bytes):
    resp = client.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)})
    assert resp.status_code == 302 and reverse("login") in resp["Location"]


def test_upload_leads_to_a_quote(logged_in, deck_bytes):
    html = logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)}, follow=True).content.decode()
    assert "Board pack.pptx" in html and "KES 200" in html and "Polish my deck" in html
    assert "You pay only after you see your deck" in html


def test_bad_upload_explains_the_problem(logged_in):
    resp = logged_in.post(reverse("orders:upload"), {"deck": pptx(b"%PDF", "notes.pdf")}, follow=True)
    assert "Please upload a PowerPoint file saved as .pptx." in resp.content.decode()
    assert not Order.objects.exists()


def test_uploads_are_rate_limited(logged_in, deck_bytes, settings):
    settings.UPLOAD_RATE_LIMIT = (1, 3600)
    logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)})
    resp = logged_in.post(reverse("orders:upload"), {"deck": pptx(deck_bytes)}, follow=True)
    assert "try again later" in resp.content.decode()
    assert Order.objects.count() == 1


def test_other_customers_orders_are_hidden(client, quoted_order):
    client.force_login(User.objects.create_user("other@example.com", "another-password-9"))
    assert client.get(reverse("orders:detail", args=[quoted_order.public_id])).status_code == 404


# --- polishing --------------------------------------------------------------

def test_polish_queues_the_deck_in_the_chosen_theme(logged_in, quoted_order):
    logged_in.post(reverse("orders:polish", args=[quoted_order.public_id]), {"theme": "slate"})
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.QUEUED and quoted_order.theme.slug == "slate"


def test_polish_needs_a_theme(logged_in, quoted_order):
    resp = logged_in.post(reverse("orders:polish", args=[quoted_order.public_id]), {}, follow=True)
    assert "Choose a theme." in resp.content.decode()


def test_one_unpaid_deck_at_a_time(logged_in, order_factory):
    first = order_factory("First.pptx", status=S.READY)
    second = order_factory("Second.pptx")
    html = page(logged_in, second)
    assert "Pay for or discard" in html and reverse("orders:detail", args=[first.public_id]) in html
    resp = logged_in.post(reverse("orders:polish", args=[second.public_id]), {"theme": "navy"}, follow=True)
    assert "Pay for or discard First.pptx first." in resp.content.decode()
    second.refresh_from_db()
    assert second.status == S.QUOTED


# --- paying and downloading ---------------------------------------------------

def test_a_ready_deck_shows_previews_and_a_pay_button_but_no_download(logged_in, ready_order):
    html = page(logged_in, ready_order)
    assert "Pay KES 200 to download" in html and "Unpaid decks are deleted after 48 hours" in html
    assert html.count('alt="Slide ') == 2
    assert "Discard this deck" in html
    assert reverse("orders:download", args=[ready_order.public_id]) not in html


def test_a_deck_without_previews_says_so(logged_in, user, tmp_path, monkeypatch):
    from orders import processing
    from polisher.preview import PreviewError

    def broken(*args, **kwargs):
        raise PreviewError("LibreOffice crashed")

    monkeypatch.setattr(processing, "render_previews", broken)
    order = services.create_order(user, pptx(build_rich_deck(tmp_path / "d.pptx").read_bytes()))
    services.start_generation(order)
    tasks.process_order(order.pk)
    html = page(logged_in, order)
    assert "We couldn't make preview images for this deck" in html
    assert "Pay KES 200 to download" in html


def test_pay_starts_mpesa(logged_in, ready_order, monkeypatch):
    from billing import services as billing

    calls = []
    monkeypatch.setattr(billing, "start_payment", lambda order, phone: calls.append((order.pk, phone)))
    logged_in.post(reverse("orders:pay", args=[ready_order.public_id]), {"phone": "0712345678"})
    assert calls == [(ready_order.pk, "0712345678")]


def test_pay_shows_phone_errors(logged_in, order_factory):
    order = order_factory(status=S.READY)
    resp = logged_in.post(reverse("orders:pay", args=[order.public_id]), {"phone": "123"}, follow=True)
    assert "Enter a phone number like 0712 345 678" in resp.content.decode()


def test_a_pending_payment_shows_check_your_phone(logged_in, order_factory):
    order = order_factory(status=S.READY)
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350)
    html = page(logged_in, order)
    assert "Check your phone" in html and "Pay KES 350 to download" not in html


def test_a_failed_payment_explains_and_offers_a_retry(logged_in, order_factory):
    order = order_factory(status=S.READY)
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350, status="failed", result_code="1032")
    html = page(logged_in, order)
    assert "You cancelled the M-Pesa prompt." in html and "Pay KES 350 to download" in html


def test_status_poll_reloads_when_either_track_changes(logged_in, order_factory):
    order = order_factory(status=S.READY)
    url = reverse("orders:status", args=[order.public_id])
    assert logged_in.get(url, {"from": "ready:0:-"}).status_code == 200
    Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=350)
    changed = logged_in.get(url, {"from": "ready:0:-"})
    assert changed.status_code == 204 and changed["HX-Refresh"] == "true"
    assert logged_in.get(url, {"from": "ready:0:pending"}).status_code == 200


def test_download_is_locked_until_paid(logged_in, ready_order):
    assert logged_in.get(reverse("orders:download", args=[ready_order.public_id])).status_code == 404


def test_a_paid_deck_downloads_with_a_friendly_name(logged_in, paid_order):
    assert "Download the .pptx" in page(logged_in, paid_order)
    resp = logged_in.get(reverse("orders:download", args=[paid_order.public_id]))
    assert resp.status_code == 302
    file_resp = logged_in.get(resp["Location"])
    assert file_resp.status_code == 200
    assert 'filename="Q3 review (polished).pptx"' in file_resp["Content-Disposition"]


def test_tampered_file_links_are_rejected(logged_in, paid_order):
    resp = logged_in.get(reverse("orders:download", args=[paid_order.public_id]))
    assert logged_in.get(resp["Location"][:-3] + "abc/").status_code == 404


def test_discard_deletes_the_deck_and_returns_to_the_list(logged_in, ready_order):
    resp = logged_in.post(reverse("orders:discard", args=[ready_order.public_id]), follow=True)
    assert "was discarded and its files deleted" in resp.content.decode()
    ready_order.refresh_from_db()
    assert ready_order.status == S.EXPIRED


def test_a_failed_deck_says_nothing_was_charged(logged_in, order_factory):
    html = page(logged_in, order_factory(status=S.FAILED))
    assert "We couldn't polish this deck" in html and "You haven't been charged." in html


def test_order_history_shows_paid_and_unpaid(logged_in, paid_order, order_factory):
    order_factory("Waiting.pptx", status=S.READY)
    html = logged_in.get(reverse("orders:list")).content.decode()
    assert ">Paid<" in html and ">Unpaid<" in html


# --- odds and ends ----------------------------------------------------------

def test_simulated_payment_is_unavailable_outside_debug(logged_in, order_factory):
    order = order_factory(status=S.READY)
    assert logged_in.post(reverse("orders:dev_pay", args=[order.public_id])).status_code == 404


def test_simulated_payment_needs_debug_and_no_provider_keys(settings):
    from orders.views import _dev_payment_enabled

    settings.DEBUG, settings.PAYMENT_PROVIDER, settings.PAYSTACK_SECRET_KEY = True, "paystack", ""
    assert _dev_payment_enabled()
    settings.PAYSTACK_SECRET_KEY = "sk_test_x"
    assert not _dev_payment_enabled()
    settings.DEBUG, settings.PAYSTACK_SECRET_KEY = False, ""
    assert not _dev_payment_enabled()


def test_legal_pages_show_a_draft_banner_until_reviewed(client, db, settings):
    assert "Draft" in client.get(reverse("privacy")).content.decode()
    settings.LEGAL_PAGES_REVIEWED = True
    assert "Draft" not in client.get(reverse("terms")).content.decode()


def test_signup_logs_in(client, db):
    resp = client.post(reverse("signup"), {"email": "New@Example.com", "password1": "kahawa-tamu-2026",
                                           "password2": "kahawa-tamu-2026"})
    assert resp.status_code == 302
    assert User.objects.get().email == "new@example.com"


def test_quote_lists_locked_objects_and_warns_about_links(logged_in, tmp_path):
    from lockedbuilder import build_locked_deck

    data = build_locked_deck(tmp_path / "locked.pptx").read_bytes()
    html = logged_in.post(reverse("orders:upload"), {"deck": pptx(data)}, follow=True).content.decode()
    assert "These are kept exactly as they are, and the slides are restyled around them." in html
    assert "links to external files that may not open on other computers" in html


def test_preview_page_notes_equation_rendering(logged_in, user, tmp_path, fake_previews):
    from lockedbuilder import build_locked_deck

    order = services.create_order(user, pptx(build_locked_deck(tmp_path / "locked.pptx").read_bytes()))
    services.start_generation(order)
    tasks.process_order(order.pk)
    order.refresh_from_db()
    assert order.status == S.READY, order.failure_reason
    html = page(logged_in, order)
    assert "equations" in html and "doesn't affect your download" in html
```

Create `orders/tests/test_admin.py`:
```python
import pytest
from django.contrib import admin

from accounts.models import User
from orders import spend
from orders.admin import OrderAdmin
from orders.models import Order


@pytest.fixture
def staff_client(client, db):
    client.force_login(User.objects.create_superuser("admin@example.com", "admin-password-7"))
    return client


def test_order_list_shows_paid_cost_and_margin(staff_client, paid_order):
    html = staff_client.get("/admin/orders/order/").content.decode()
    assert "AI cost (KES)" in html and "Margin (KES)" in html and "Q3 review.pptx" in html
    assert "197.00" in html  # KES 200 - AI 0.00 (heuristic planner) - fee 3.00


def test_margin_is_only_shown_for_paid_orders(ready_order):
    assert OrderAdmin(Order, admin.site).margin(ready_order) == "-"


def test_orders_can_be_filtered_by_payment(staff_client, paid_order, order_factory):
    order_factory("Unpaid.pptx", status=Order.Status.READY)
    paid = staff_client.get("/admin/orders/order/?paid=yes").content.decode()
    unpaid = staff_client.get("/admin/orders/order/?paid=no").content.decode()
    assert "Q3 review.pptx" in paid and "Unpaid.pptx" not in paid
    assert "Unpaid.pptx" in unpaid and "Q3 review.pptx" not in unpaid


def test_daily_spend_page_shows_the_cap(staff_client):
    spend.record_spend(0.5)
    resp = staff_client.get("/admin/orders/dailyspend/")
    assert resp.status_code == 200 and "Cap (USD)" in resp.content.decode()


def test_payments_can_be_marked_refunded(staff_client, paid_order):
    payment = paid_order.payments.get()
    staff_client.post("/admin/billing/payment/", {"action": "mark_refunded", "_selected_action": [payment.pk]})
    payment.refresh_from_db()
    assert payment.status == "refunded" and payment.refunded_at
```

Run: `.venv/bin/python -m pytest orders/tests/test_views.py orders/tests/test_admin.py -q`
Expected: failures, because there are no `orders:polish` or `orders:discard` URLs yet and the templates are old.

- [ ] **Step 11: Views, URLs, admin and templates.**

Replace `orders/views.py`:
```python
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing import services as billing
from billing.models import Payment, PriceTier, ProductType
from billing.providers import PaymentEvent, get_provider
from core.ratelimit import allow

from . import services
from .files import read_token, signed_url
from .models import Order, Theme
from .processing import download_name

S = Order.Status
IN_FLIGHT = set(Order.IN_FLIGHT)


def home(request):
    context = {
        "tiers": PriceTier.objects.filter(active=True, product_type=ProductType.DECK_POLISH),
        "themes": Theme.objects.filter(active=True),
        "max_mb": settings.MAX_UPLOAD_BYTES // 1024 // 1024,
        "max_media_mb": settings.MAX_UPLOAD_BYTES_MEDIA // 1024 // 1024,
        "max_slides": settings.MAX_SLIDES,
        "retention_days": settings.FILE_RETENTION_DAYS,
    }
    if request.user.is_authenticated:
        context["recent"] = request.user.orders.all()[:3]
    return render(request, "orders/home.html", context)


@login_required
@require_POST
def upload(request):
    if not allow(request, "upload", *settings.UPLOAD_RATE_LIMIT):
        messages.error(request, "You've uploaded a lot of decks in the last hour. Please try again later.")
        return redirect("home")
    deck = request.FILES.get("deck")
    if deck is None:
        messages.error(request, "Choose a .pptx file to upload.")
        return redirect("home")
    try:
        order = services.create_order(request.user, deck)
    except services.UploadError as e:
        messages.error(request, e.message)
        return redirect("home")
    return redirect("orders:detail", public_id=order.public_id)


def _own_order(request, public_id):
    return get_object_or_404(Order, public_id=public_id, user=request.user)


def _local_phone(mpesa_phone):
    return "0" + mpesa_phone[3:] if mpesa_phone.startswith("254") and len(mpesa_phone) == 12 else ""


def _dev_payment_enabled():
    """The simulated-payment button: DEBUG only, and only while the payment provider has no keys."""
    return settings.DEBUG and not get_provider().configured


def _poll_key(order, last_payment):
    """Changes whenever the page should reload: processing status, payment, or the latest payment's status."""
    return f"{order.status}:{int(order.is_paid)}:{last_payment.status if last_payment else '-'}"


def _polling(order, last_payment):
    return order.status in IN_FLIGHT or (last_payment is not None and last_payment.status == Payment.Status.PENDING)


@login_required
def order_list(request):
    return render(request, "orders/list.html", {"orders": request.user.orders.select_related("theme")})


@login_required
def detail(request, public_id):
    order = _own_order(request, public_id)
    last_payment = order.payments.first()
    context = {
        "order": order,
        "themes": Theme.objects.filter(active=True),
        "phone": _local_phone(last_payment.phone if last_payment else request.user.mpesa_phone),
        "last_payment": last_payment,
        "polling": _polling(order, last_payment),
        "poll_key": _poll_key(order, last_payment),
        "blocking": services.unpaid_work(request.user, exclude=order) if order.status == S.QUOTED else None,
        "dev_payment": _dev_payment_enabled() and order.can_pay,
        "retention_days": settings.FILE_RETENTION_DAYS,
        "unpaid_hours": settings.UNPAID_RETENTION_HOURS,
    }
    if order.status in (S.READY, S.RERENDERING) and order.result_file:
        context["previews"] = [signed_url(name) for name in order.preview_images]
        context["overflow"] = order.overflow_slides
    return render(request, "orders/detail.html", context)


@login_required
@require_POST
def polish(request, public_id):
    order = _own_order(request, public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme is None:
        messages.error(request, "Choose a theme.")
        return redirect("orders:detail", public_id=public_id)
    try:
        services.start_generation(order, theme=theme)
    except services.GenerationError as e:
        messages.error(request, e.message)
    return redirect("orders:detail", public_id=public_id)


@login_required
@require_POST
def pay(request, public_id):
    order = _own_order(request, public_id)
    if not allow(request, "pay", *settings.PAYMENT_RATE_LIMIT):
        messages.error(request, "Too many payment attempts. Please wait a few minutes and try again.")
        return redirect("orders:detail", public_id=public_id)
    try:
        billing.start_payment(order, request.POST.get("phone", ""))
    except billing.PaymentError as e:
        messages.error(request, str(e))
    return redirect("orders:detail", public_id=public_id)


@login_required
def status(request, public_id):
    """HTMX poll target: reload the page when the processing status or the payment changes."""
    order = _own_order(request, public_id)
    last_payment = order.payments.first()
    key = _poll_key(order, last_payment)
    if key != request.GET.get("from"):
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    paying = last_payment is not None and last_payment.status == Payment.Status.PENDING
    waited = (timezone.now() - (last_payment.created_at if paying else order.updated_at)).total_seconds()
    return render(request, "orders/_status.html",
                  {"order": order, "last_payment": last_payment, "poll_key": key, "waited": waited})


@login_required
@require_POST
def discard(request, public_id):
    order = _own_order(request, public_id)
    try:
        services.discard_order(order)
    except ValueError:
        messages.error(request, "This deck can't be discarded right now.")
        return redirect("orders:detail", public_id=public_id)
    messages.success(request, f"{order.original_name} was discarded and its files deleted.")
    return redirect("orders:list")


@login_required
def download(request, public_id):
    order = _own_order(request, public_id)
    if order.status != S.READY or not order.result_file or not order.is_paid:
        raise Http404
    return redirect(signed_url(order.result_file.name, download_name(order)))


def serve_file(request, token):
    """Local-storage stand-in for signed Spaces URLs (development only)."""
    try:
        data = read_token(token)
    except signing.BadSignature:
        raise Http404
    if not default_storage.exists(data["n"]):
        raise Http404
    return FileResponse(default_storage.open(data["n"], "rb"), as_attachment=bool(data["d"]), filename=data["d"])


@login_required
@require_POST
def dev_pay(request, public_id):
    """DEBUG only, while the payment provider has no keys: pretend M-Pesa confirmed the payment."""
    if not _dev_payment_enabled():
        raise Http404
    order = _own_order(request, public_id)
    if not order.can_pay:
        messages.error(request, "This deck can't be paid for right now.")
        return redirect("orders:detail", public_id=public_id)
    ref = f"dev-{order.public_id.hex[:12]}-{order.payments.count()}"
    Payment.objects.create(order=order, provider=settings.PAYMENT_PROVIDER, provider_ref=ref,
                           phone="254700000000", amount_kes=order.price_kes)
    billing.apply_event(settings.PAYMENT_PROVIDER, PaymentEvent(
        provider_ref=ref, state="paid", amount=Decimal(order.price_kes), receipt="DEVPAY", fee=Decimal("0.01"),
        raw={"simulated": True}), source="callback")
    return redirect("orders:detail", public_id=public_id)
```

Replace `urlpatterns` in `orders/urls.py`:
```python
urlpatterns = [
    path("upload/", views.upload, name="upload"),
    path("", views.order_list, name="list"),
    path("<uuid:public_id>/", views.detail, name="detail"),
    path("<uuid:public_id>/polish/", views.polish, name="polish"),
    path("<uuid:public_id>/pay/", views.pay, name="pay"),
    path("<uuid:public_id>/status/", views.status, name="status"),
    path("<uuid:public_id>/discard/", views.discard, name="discard"),
    path("<uuid:public_id>/download/", views.download, name="download"),
    path("<uuid:public_id>/dev-pay/", views.dev_pay, name="dev_pay"),
    path("files/<str:token>/", views.serve_file, name="file"),
]
```

In `orders/admin.py`, change the imports to `from django.contrib import admin` (drop `messages`). Add this class above `OrderAdmin`:
```python
class PaidFilter(admin.SimpleListFilter):
    title = "payment"
    parameter_name = "paid"

    def lookups(self, request, model_admin):
        return [("yes", "Paid"), ("no", "Unpaid")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(paid_at__isnull=False)
        if self.value() == "no":
            return queryset.filter(paid_at__isnull=True)
        return queryset
```
In `OrderAdmin`:
- `list_display = ("original_name", "user", "status", "paid", "slide_count", "price_kes", "ai_cost", "payment_fees", "margin", "theme", "created_at")`
- `list_filter = ("status", PaidFilter, "product_type", "theme", "waiting_for_capacity")`
- In `readonly_fields`, replace `"rerun_used"` with `"paid_at", "switch_error"`.
- Delete `actions` and `mark_refunded`.

Then add:
```python
    @admin.display(description="Paid", boolean=True, ordering="paid_at")
    def paid(self, obj):
        return obj.paid_at is not None
```
and replace `margin`:
```python
    @admin.display(description="Margin (KES)")
    def margin(self, obj):
        if obj.paid_at is None:
            return "-"
        cost = getattr(obj, "_ai_cost_kes", obj.ai_cost_kes)
        fees = getattr(obj, "_fees_kes", obj.payment_fees_kes)
        return f"{Decimal(obj.price_kes) - cost - fees:.2f}"
```

Create `templates/orders/_discard.html`:
```html
{% if order.can_discard %}
<form method="post" action="{% url 'orders:discard' order.public_id %}" class="mt-8 text-center"
      onsubmit="return confirm('Discard this deck? Its files are deleted straight away.')">
  {% csrf_token %}
  <button class="btn-quiet text-sm">Discard this deck</button>
</form>
{% endif %}
```

Replace `templates/orders/_status.html`:
```html
<div hx-get="{% url 'orders:status' order.public_id %}?from={{ poll_key|urlencode }}" hx-trigger="every 3s" hx-swap="outerHTML">
  {% if last_payment.status == 'pending' %}
    <h2 class="text-2xl font-bold tracking-tight">Check your phone</h2>
    <p class="mt-2 text-muted">Enter your M-Pesa PIN to pay KES {{ order.price_kes }}. This page updates when the payment goes through.</p>
    <div class="progress-bar mt-6" aria-hidden="true"></div>
    {% if waited > 90 %}
      <p class="mt-4 text-sm text-muted">No prompt? It may have timed out. We'll confirm with M-Pesa within a few minutes, and you can try again once it's cancelled.</p>
    {% endif %}
  {% elif order.waiting_for_capacity %}
    <h2 class="text-2xl font-bold tracking-tight">Your deck is in the queue</h2>
    <p class="mt-2 text-muted">We're busy right now. Your deck will start soon, and you can leave this page.</p>
    <div class="progress-bar mt-6" aria-hidden="true"></div>
  {% else %}
    <h2 class="text-2xl font-bold tracking-tight">Polishing your deck</h2>
    <p class="mt-2 text-muted">Planning layouts, rebuilding slides, then checking that every word and number made it across. This usually takes one to three minutes. You can leave this page.</p>
    <div class="progress-bar mt-6" aria-hidden="true"></div>
  {% endif %}
</div>
```

Replace `templates/orders/_order_row.html`:
```html
<li>
  <a href="{% url 'orders:detail' order.public_id %}" class="flex items-center justify-between gap-4 px-5 py-4 hover:bg-page">
    <span class="min-w-0">
      <span class="block truncate font-bold">{{ order.original_name }}</span>
      <span class="block text-sm text-muted">{{ order.slide_count }} slides, {{ order.created_at|date:"j M Y" }}</span>
    </span>
    <span class="flex shrink-0 flex-wrap justify-end gap-2">
      {% if order.is_paid %}<span class="rounded-full bg-ok-soft px-3 py-1 text-sm font-bold text-ok">Paid</span>{% elif order.status == 'ready' %}<span class="rounded-full bg-marigold-soft px-3 py-1 text-sm font-bold text-ink">Unpaid</span>{% endif %}
      <span class="rounded-full px-3 py-1 text-sm font-bold
        {% if order.status == 'ready' %}bg-ok-soft text-ok{% elif order.status == 'failed' %}bg-danger-soft text-danger{% elif order.status == 'expired' %}bg-page text-muted{% else %}bg-marigold-soft text-ink{% endif %}">
        {{ order.get_status_display }}
      </span>
    </span>
  </a>
</li>
```

Replace `templates/orders/detail.html`:
```html
{% extends "base.html" %}
{% block title %}{{ order.original_name }}: SUBRA{% endblock %}
{% block content %}
<div class="mx-auto max-w-3xl">
  <a href="{% url 'orders:list' %}" class="text-sm font-bold text-muted hover:text-ink">My decks</a>
  <h1 class="mt-2 break-words text-3xl font-extrabold tracking-tight">{{ order.original_name }}</h1>
  <p class="mt-1 text-muted">{{ order.slide_count }} slides</p>

  {% if order.locked_description and order.status != 'expired' %}
    <p class="mt-4 rounded-lg bg-page px-4 py-3 text-sm">{{ order.locked_description }} These are kept exactly as they are, and the slides are restyled around them.</p>
  {% endif %}
  {% if order.has_linked_objects and order.status != 'expired' %}
    <p class="mt-3 rounded-lg bg-marigold-soft px-4 py-3 text-sm">This deck links to external files that may not open on other computers. We keep the links as they are.</p>
  {% endif %}

  {% if order.status == 'quoted' %}
    <form method="post" action="{% url 'orders:polish' order.public_id %}" class="mt-8"
          onsubmit="this.querySelector('[data-polish]').disabled = true; this.querySelector('[data-polish]').textContent = 'Starting…'">
      {% csrf_token %}
      <fieldset>
        <legend class="text-lg font-bold">Choose a theme</legend>
        <div class="mt-3 grid grid-cols-3 gap-2 sm:gap-3">
          {% for theme in themes %}
          <label class="block cursor-pointer rounded-xl border-2 border-line bg-white p-1.5 hover:border-ink/40 has-checked:border-ink has-checked:bg-marigold-soft has-focus-visible:outline-3 has-focus-visible:outline-marigold sm:p-2">
            <input type="radio" name="theme" value="{{ theme.slug }}" class="sr-only" {% if theme == order.theme %}checked{% endif %}>
            <img src="{{ theme.preview_url }}" alt="" class="aspect-video w-full rounded-md object-cover" width="640" height="360">
            <span class="mt-1.5 block px-0.5 text-sm font-bold leading-tight sm:px-1 sm:text-base">{{ theme.name }}</span>
            <span class="hidden px-1 pb-1 text-sm text-muted sm:block">{{ theme.description }}</span>
          </label>
          {% endfor %}
        </div>
      </fieldset>

      <div class="panel mt-8 p-5 sm:p-6">
        <div class="flex items-baseline justify-between gap-4">
          <span class="text-lg font-bold">Price</span>
          <span class="text-3xl font-extrabold tracking-tight">KES {{ order.price_kes }}</span>
        </div>
        <p class="mt-1 text-sm text-muted">{{ order.tier.name }} deck. You pay only after you see your deck.</p>
        {% if blocking %}
          <p class="mt-5 rounded-lg bg-marigold-soft px-4 py-3 text-sm">Pay for or discard <a href="{% url 'orders:detail' blocking.public_id %}" class="font-bold underline underline-offset-2">{{ blocking.original_name }}</a> first. You can have one unpaid deck at a time.</p>
        {% endif %}
        <button class="btn-primary mt-5 w-full" data-polish {% if blocking %}disabled{% endif %}>Polish my deck</button>
        <p class="mt-3 text-sm text-muted">This takes one to three minutes. You'll see every slide before you decide to pay.</p>
      </div>
    </form>
    {% include "orders/_discard.html" %}

  {% elif order.status == 'ready' or order.status == 'rerendering' %}
    {% if polling %}
      <div class="panel mt-8 p-6">{% include "orders/_status.html" with waited=0 %}</div>
    {% elif order.is_paid %}
      <div class="panel mt-8 p-5 sm:p-6">
        <h2 class="text-2xl font-bold tracking-tight">Your deck is ready</h2>
        <p class="mt-1 text-muted">{{ order.theme.name }} theme. Every word and number was checked against your original.</p>
        <a href="{% url 'orders:download' order.public_id %}" class="btn-primary mt-5 w-full sm:w-auto">Download the .pptx</a>
        <p class="mt-3 text-sm text-muted">Files are deleted on {{ order.expires_at|date:"j M Y" }}.</p>
      </div>
    {% else %}
      <form method="post" action="{% url 'orders:pay' order.public_id %}" class="panel mt-8 p-5 sm:p-6"
            onsubmit="this.querySelector('[data-pay]').disabled = true; this.querySelector('[data-pay]').textContent = 'Sending the M-Pesa prompt…'">
        {% csrf_token %}
        {% if last_payment.status == 'failed' %}
          <p class="mb-5 rounded-lg bg-danger-soft px-4 py-3 font-bold text-danger">{{ last_payment.customer_message }} You haven't been charged. You can try again.</p>
        {% endif %}
        <h2 class="text-2xl font-bold tracking-tight">Your redesigned deck is ready</h2>
        <p class="mt-1 text-muted">{{ order.theme.name }} theme. Every word and number was checked against your original. Look through the slides below, then pay to download the editable .pptx.</p>
        <div class="mt-5 flex items-baseline justify-between gap-4">
          <span class="text-lg font-bold">Total</span>
          <span class="text-3xl font-extrabold tracking-tight">KES {{ order.price_kes }}</span>
        </div>
        <label for="phone" class="mt-5 block font-bold">M-Pesa number</label>
        <input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" required
               value="{{ phone }}" placeholder="0712 345 678" class="field mt-2">
        <button class="btn-primary mt-4 w-full" data-pay>Pay KES {{ order.price_kes }} to download</button>
        <p class="mt-3 text-sm text-muted">Unpaid decks are deleted after {{ unpaid_hours }} hours. This one will be deleted in {{ order.expires_at|timeuntil }}.</p>
      </form>
      {% if dev_payment %}
        <form method="post" action="{% url 'orders:dev_pay' order.public_id %}" class="mt-4">
          {% csrf_token %}
          <button class="btn-quiet w-full text-sm">Development: simulate a successful payment</button>
        </form>
      {% endif %}
    {% endif %}

    {% if overflow %}
      <p class="mt-4 rounded-lg bg-marigold-soft px-4 py-3 text-sm">Slide{{ overflow|length|pluralize }} {{ overflow|join:", " }} {{ overflow|length|pluralize:"has,have" }} a lot of text, so the type is small. Consider splitting {{ overflow|length|pluralize:"it,them" }} in PowerPoint.</p>
    {% endif %}

    <section class="mt-10">
      <h2 class="text-lg font-bold">Preview</h2>
      {% if previews %}
        {% if order.preview_caveats %}
          <p class="mt-1 text-sm text-muted">In these previews, {{ order.preview_caveats|join:" and " }}. This doesn't affect your download.</p>
        {% endif %}
        <div class="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
          {% for url in previews %}
            <a href="{{ url }}" target="_blank" rel="noopener" class="block overflow-hidden rounded-lg border border-line bg-white hover:border-ink">
              <img src="{{ url }}" alt="Slide {{ forloop.counter }}" loading="lazy" class="w-full">
            </a>
          {% endfor %}
        </div>
      {% else %}
        <p class="mt-1 text-muted">We couldn't make preview images for this deck. The file itself passed every check.</p>
      {% endif %}
    </section>
    {% include "orders/_discard.html" %}

  {% elif polling %}
    <div class="panel mt-8 p-6">{% include "orders/_status.html" with waited=0 %}</div>

  {% elif order.status == 'failed' %}
    <div class="panel mt-8 p-6">
      <h2 class="text-2xl font-bold tracking-tight">We couldn't polish this deck</h2>
      <p class="mt-2 text-muted">You haven't been charged. Our checks found that the redesign could lose some of your content, so we stopped. We've been told about it and will look into it.</p>
    </div>
    {% include "orders/_discard.html" %}

  {% elif order.status == 'expired' %}
    <div class="panel mt-8 p-6">
      <h2 class="text-2xl font-bold tracking-tight">Files deleted</h2>
      <p class="mt-2 text-muted">We delete unpaid decks after {{ unpaid_hours }} hours and paid decks {{ retention_days }} days after payment, or straight away if you discard them. Upload the deck again if you need it polished.</p>
    </div>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 12: Rebuild the CSS and run everything.**

Run: `npm run build:css && .venv/bin/python -m pytest -q`
Expected: all pass. Also confirm no old status names are left:
```bash
grep -rn "payment_pending\|payment_failed\|refund_pending\|rerun\|S\.PAID\|S\.DONE\|UPLOADED" --include=*.py --include=*.html orders billing templates | grep -v migrations
```
Expected: no output.

- [ ] **Step 13: Commit.**

```bash
git add conftest.py subra/settings.py .env.example orders billing templates static/css/app.css
git commit -m "$(printf 'Generate decks before payment; pay with M-Pesa to download\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 5: Switch themes from the saved plan

**Files:**
- Modify:
  - `orders/models.py`, `orders/services.py`, `orders/processing.py`, `orders/tasks.py`, `orders/views.py`, `orders/urls.py`
  - `subra/settings.py`
  - `templates/orders/detail.html`, `templates/orders/_status.html`
  - `static/css/app.css`
- Create: `orders/migrations/0006_theme_switching.py` (generated), `orders/tests/test_theme_switch.py`

**Interfaces:**
- Consumes:
  - `rerender()` and `RerenderResult` (Task 1)
  - `Order.plan`, `fetch_original`, `make_previews`, `store_result` and `delete_files` (Task 4)
  - the `confirm_payment`, `ready_order`, `paid_order` and `fake_previews` fixtures (Task 4)
- Produces:
  - `Order.switching_to` (FK to `Theme`, nullable) and `Order.can_switch_theme`.
  - `orders.services.ThemeSwitchError(message)` and `start_theme_switch(order, theme) -> Order`.
  - `orders.processing.run_rerender(order) -> bool` and `finish_switch_failed(order, theme, reason)`.
  - Task `orders.tasks.rerender_order(order_id)`.
  - View `orders:theme`.
  - Setting `THEME_SWITCH_RATE_LIMIT = (10, 3600)`.

- [ ] **Step 1: Write the failing tests.** Create `orders/tests/test_theme_switch.py`:

```python
import re
from datetime import timedelta

import pytest
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from billing.models import Payment
from orders import processing, services, spend, tasks
from orders.models import AIJob, Order, Theme
from polisher.pipeline import RerenderResult
from polisher.pipeline import rerender as real_rerender

S = Order.Status
ERROR = "We couldn't switch to Clean Slate; your deck is unchanged."


@pytest.fixture(autouse=True)
def _previews(fake_previews):
    """Every test here fakes LibreOffice."""


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    return django_capture_on_commit_callbacks


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def slate():
    return Theme.objects.get(slug="slate")


def switch(order, capture, slug="slate"):
    """Start a switch and run its task (Celery runs inline in tests)."""
    with capture(execute=True):
        services.start_theme_switch(order, Theme.objects.get(slug=slug))
    order.refresh_from_db()
    return order


def theme_name(order):
    with default_storage.open(order.result_file.name, "rb") as f:
        part = Presentation(f).slide_master.part.part_related_by(RT.THEME)
    return re.search(rb'<a:clrScheme name="([^"]*)"', part.blob).group(1).decode()


def failing_rerender(*args, **kwargs):
    return RerenderResult(ok=False, output_path=None, failure="Verification failed: Image [s5-i1] changed")


def test_switching_rerenders_the_saved_plan_without_ai(ready_order, capture):
    old_files = [ready_order.result_file.name, *ready_order.preview_images]
    jobs = AIJob.objects.count()
    order = switch(ready_order, capture)
    assert order.status == S.READY and order.theme.slug == "slate"
    assert order.switching_to is None and order.switch_error == ""
    assert theme_name(order) == "SUBRA Clean Slate"
    assert AIJob.objects.count() == jobs
    assert len(order.preview_images) == 2 and order.result_file.name not in old_files
    assert not any(default_storage.exists(n) for n in old_files)
    assert not order.is_paid


def test_switching_after_paying_keeps_the_deck_paid(paid_order, capture):
    order = switch(paid_order, capture)
    assert order.theme.slug == "slate" and order.is_paid and order.status == S.READY


def test_a_failed_switch_keeps_the_previous_deck(ready_order, capture, monkeypatch):
    monkeypatch.setattr(processing, "rerender", failing_rerender)
    result_file = ready_order.result_file.name
    order = switch(ready_order, capture)
    assert order.status == S.READY and order.theme.slug == "navy" and order.switching_to is None
    assert order.result_file.name == result_file and default_storage.exists(result_file)
    assert order.switch_error == ERROR


def test_a_crashed_switch_keeps_the_previous_deck(ready_order, capture, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("LibreOffice fell over")

    monkeypatch.setattr(processing, "rerender", crash)
    order = switch(ready_order, capture)
    assert order.status == S.READY and order.theme.slug == "navy" and order.switch_error == ERROR


def test_the_next_successful_switch_clears_the_error(ready_order, capture, monkeypatch):
    monkeypatch.setattr(processing, "rerender", failing_rerender)
    switch(ready_order, capture)
    monkeypatch.setattr(processing, "rerender", real_rerender)
    order = switch(ready_order, capture)
    assert order.theme.slug == "slate" and order.switch_error == ""


@pytest.mark.parametrize("why", ["same theme", "pending payment", "not generated", "no plan"])
def test_switches_that_dont_make_sense_are_refused(ready_order, order_factory, why):
    order, theme = ready_order, slate()
    if why == "same theme":
        theme = ready_order.theme
    elif why == "pending payment":
        Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                               amount_kes=200)
    elif why == "not generated":
        order = order_factory("Quoted.pptx")
    elif why == "no plan":
        Order.objects.filter(pk=order.pk).update(plan=None)
    with pytest.raises(services.ThemeSwitchError):
        services.start_theme_switch(order, theme)


def test_a_stuck_switch_is_released_after_10_minutes(ready_order):
    Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING, switching_to=slate(),
                                                   updated_at=timezone.now() - timedelta(minutes=11))
    tasks.release_queued_orders()
    ready_order.refresh_from_db()
    assert ready_order.status == S.READY and ready_order.switching_to is None
    assert ready_order.switch_error == ERROR


def test_stuck_switches_are_released_even_when_the_spend_cap_is_reached(ready_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING, switching_to=slate(),
                                                   updated_at=timezone.now() - timedelta(minutes=11))
    tasks.release_queued_orders()
    ready_order.refresh_from_db()
    assert ready_order.status == S.READY


def test_a_recent_switch_is_left_alone(ready_order):
    Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING, switching_to=slate())
    tasks.release_queued_orders()
    ready_order.refresh_from_db()
    assert ready_order.status == S.RERENDERING


def test_a_switch_that_finishes_after_being_released_is_discarded(ready_order):
    services.start_theme_switch(ready_order, slate())  # on_commit doesn't fire here, so the task hasn't run
    started = Order.objects.select_related("switching_to").get(pk=ready_order.pk)
    processing.finish_switch_failed(started, started.switching_to, "Timed out")  # what the release does
    result_file = Order.objects.get(pk=ready_order.pk).result_file.name
    assert processing.run_rerender(started) is False
    order = Order.objects.get(pk=ready_order.pk)
    assert order.status == S.READY and order.theme.slug == "navy" and order.result_file.name == result_file


def test_a_payment_that_arrives_during_a_switch_is_accepted(ready_order, confirm_payment):
    services.start_theme_switch(ready_order, slate())
    order = confirm_payment(Order.objects.get(pk=ready_order.pk))
    assert order.status == S.RERENDERING and order.is_paid


# --- the page ---------------------------------------------------------------

def test_theme_buttons_start_a_switch(logged_in, ready_order):
    html = logged_in.get(reverse("orders:detail", args=[ready_order.public_id])).content.decode()
    assert "Switch to Clean Slate" in html and "Switch to Navy Executive" not in html
    resp = logged_in.post(reverse("orders:theme", args=[ready_order.public_id]), {"theme": "slate"}, follow=True)
    assert "Switching to Clean Slate" in resp.content.decode()
    ready_order.refresh_from_db()
    assert ready_order.status == S.RERENDERING


def test_theme_switches_are_rate_limited(logged_in, ready_order, settings):
    settings.THEME_SWITCH_RATE_LIMIT = (1, 3600)
    logged_in.post(reverse("orders:theme", args=[ready_order.public_id]), {"theme": "slate"})
    Order.objects.filter(pk=ready_order.pk).update(status=S.READY, switching_to=None)
    resp = logged_in.post(reverse("orders:theme", args=[ready_order.public_id]), {"theme": "savanna"}, follow=True)
    assert "switched themes a lot in the last hour" in resp.content.decode()


def test_theme_buttons_wait_for_a_pending_payment(logged_in, ready_order):
    Payment.objects.create(order=ready_order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=200)
    html = logged_in.get(reverse("orders:detail", args=[ready_order.public_id])).content.decode()
    assert "You can switch themes once the payment has gone through." in html


def test_a_failed_switch_is_explained_on_the_page(logged_in, ready_order):
    Order.objects.filter(pk=ready_order.pk).update(switch_error=ERROR)
    html = logged_in.get(reverse("orders:detail", args=[ready_order.public_id])).content.decode()
    assert "switch to Clean Slate; your deck is unchanged." in html
```

Run: `.venv/bin/python -m pytest orders/tests/test_theme_switch.py -q`
Expected: failures with `AttributeError: ... 'start_theme_switch'` and a missing `switching_to` field.

- [ ] **Step 2: Model, migration and setting.**

In `orders/models.py`, add to `Order` below `switch_error`:
```python
    switching_to = models.ForeignKey(Theme, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
```
and add to the payment properties:
```python
    @property
    def can_switch_theme(self):
        return (self.status == self.Status.READY and bool(self.plan) and not self.files_deleted_at
                and not self.has_pending_payment)
```
Run:
```bash
.venv/bin/python manage.py makemigrations --settings=subra.test_settings orders -n theme_switching
```
Expected: `orders/migrations/0006_theme_switching.py`.

In `subra/settings.py`, below `PAYMENT_RATE_LIMIT`:
```python
THEME_SWITCH_RATE_LIMIT = (10, 3600)  # theme switches per hour, per user and per IP (each one runs LibreOffice)
```

- [ ] **Step 3: Services, processing, tasks.**

In `orders/services.py`, add below `GenerationError`:
```python
class ThemeSwitchError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message
```
and below `discard_order`:
```python
def start_theme_switch(order, theme):
    """Re-render a generated deck in another theme from its saved plan: no AI call, free, any number of times."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if not theme.active or theme.pk == order.theme_id:
            raise ThemeSwitchError("Choose a different theme.")
        if not order.can_switch_theme:
            raise ThemeSwitchError("The theme can't be changed right now.")
        order.transition(S.RERENDERING, note=f"Switching to {theme.slug}", switching_to=theme, switch_error="")
        transaction.on_commit(lambda: _enqueue_rerender(order))
    return order


def _enqueue_rerender(order):
    from .tasks import rerender_order

    rerender_order.delay(order.pk)
```

In `orders/processing.py`:
- Add `from django.db import transaction`.
- Change the pipeline import to `from polisher.pipeline import polish, rerender`.
- Add below `run_polish`:
```python
def run_rerender(order):
    """Re-render the saved plan in order.switching_to. Returns True if the switch took effect.

    A switch released as stuck (or overtaken by a newer one) may still finish; its output is discarded.
    """
    theme = order.switching_to
    with tempfile.TemporaryDirectory(prefix="subra-switch-") as tmp:
        tmp = Path(tmp)
        src, out = fetch_original(order, tmp), tmp / "polished.pptx"
        result = rerender(src, out, theme.slug, order.plan)
        if not result.ok:
            finish_switch_failed(order, theme, result.failure)
            return False
        previews = make_previews(order, out, tmp)
        with transaction.atomic():
            current = Order.objects.select_for_update().get(pk=order.pk)
            if current.status != S.RERENDERING or current.switching_to_id != theme.pk:
                log.warning("Theme switch for order %s finished after it was released; discarding it", order.pk)
                return False
            replaced = store_result(current, out, previews)
            current.transition(S.READY, note=f"Switched to {theme.slug}", theme=theme, switching_to=None,
                               switch_error="", overflow_slides=result.overflow_slides)
        delete_files(replaced)
    return True


def finish_switch_failed(order, theme, reason):
    """Back to READY with the previous deck untouched, unless a newer switch has taken over."""
    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        if current.status != S.RERENDERING or current.switching_to_id != theme.pk:
            return
        current.transition(S.READY, note=f"Theme switch failed: {reason}"[:500], switching_to=None,
                           switch_error=f"We couldn't switch to {theme.name}; your deck is unchanged.")
```

In `orders/tasks.py`, add below `process_order`:
```python
@shared_task
def rerender_order(order_id):
    order = Order.objects.select_related("switching_to").filter(pk=order_id).first()
    if order is None or order.status != S.RERENDERING or order.switching_to is None:
        return "skipped"
    try:
        return "done" if processing.run_rerender(order) else "unchanged"
    except Exception as e:
        log.exception("Theme switch crashed for order %s", order_id)
        processing.finish_switch_failed(order, order.switching_to, f"Internal error: {type(e).__name__}: {e}")
        return "failed"
```
Replace the start of `release_queued_orders`, so stuck switches are released before the spend-cap check:
```python
@shared_task
def release_queued_orders():
    """Release theme switches stuck for 10 minutes, then start queued orders that were waiting (spend cap,
    AI outage) or whose task was lost."""
    stale = timezone.now() - timedelta(minutes=10)
    for order in Order.objects.filter(status=S.RERENDERING, updated_at__lt=stale).select_related("switching_to"):
        processing.finish_switch_failed(order, order.switching_to, "Timed out")
    if spend.cap_reached():
        return 0
    waiting = Q(waiting_for_capacity=True) | Q(updated_at__lt=stale)
    ids = list(Order.objects.filter(waiting, status=S.QUEUED).values_list("pk", flat=True))
    for pk in ids:
        process_order.delay(pk)
    return len(ids)
```

- [ ] **Step 4: View, URL, templates.**

In `orders/views.py`, add below `polish`:
```python
@login_required
@require_POST
def switch_theme(request, public_id):
    order = _own_order(request, public_id)
    theme = Theme.objects.filter(slug=request.POST.get("theme"), active=True).first()
    if theme is None:
        messages.error(request, "Choose a theme.")
    elif not allow(request, "theme", *settings.THEME_SWITCH_RATE_LIMIT):
        messages.error(request, "You've switched themes a lot in the last hour. Please try again later.")
    else:
        try:
            services.start_theme_switch(order, theme)
        except services.ThemeSwitchError as e:
            messages.error(request, e.message)
    return redirect("orders:detail", public_id=public_id)
```
In `orders/urls.py`, add after the `polish` path:
```python
    path("<uuid:public_id>/theme/", views.switch_theme, name="theme"),
```

In `templates/orders/_status.html`, insert before `{% elif order.waiting_for_capacity %}`:
```html
  {% elif order.status == 'rerendering' %}
    <h2 class="text-2xl font-bold tracking-tight">Switching to {{ order.switching_to.name }}…</h2>
    <p class="mt-2 text-muted">Restyling your slides, then checking every word and number again. This takes under a minute.</p>
    <div class="progress-bar mt-6" aria-hidden="true"></div>
```

In `templates/orders/detail.html`, insert straight after the `{% if overflow %}…{% endif %}` block in the ready section:
```html
    {% if order.switch_error %}
      <p class="mt-4 rounded-lg bg-danger-soft px-4 py-3 text-sm font-bold text-danger">{{ order.switch_error }}</p>
    {% endif %}
    {% if order.status == 'ready' and order.plan %}
      <form method="post" action="{% url 'orders:theme' order.public_id %}" class="panel mt-8 p-5 sm:p-6">
        {% csrf_token %}
        <h2 class="text-lg font-bold">Try another theme</h2>
        {% if last_payment.status == 'pending' %}
          <p class="mt-1 text-muted">You can switch themes once the payment has gone through.</p>
        {% else %}
          <p class="mt-1 text-muted">Free, as often as you like. Your words stay exactly as they are.</p>
        {% endif %}
        <div class="mt-4 flex flex-wrap gap-2">
          {% for theme in themes %}{% if theme != order.theme %}
            <button name="theme" value="{{ theme.slug }}" class="btn-quiet" {% if not order.can_switch_theme %}disabled{% endif %}>Switch to {{ theme.name }}</button>
          {% endif %}{% endfor %}
        </div>
      </form>
    {% endif %}
```

- [ ] **Step 5: Run the tests.**

Run: `npm run build:css && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit.**

```bash
git add orders subra/settings.py templates static/css/app.css
git commit -m "$(printf 'Switch themes for free by re-rendering the saved plan\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 6: Paystack Starter-limit alerts and the monthly spend summary

**Files:**
- Modify: `billing/services.py`, `orders/spend.py`, `orders/admin.py`, `subra/settings.py`, `.env.example`
- Create: `templates/admin/orders/dailyspend/change_list.html`, `billing/tests/test_starter_limit.py`
- Test: `orders/tests/test_admin.py`

**Interfaces:**
- Consumes:
  - `_mark_paid` and `apply_event` (Task 4)
  - `Payment.Status.REFUNDED` (Task 4)
  - `order_factory`, `paid_order` and `staff_client` (Task 4)
- Produces:
  - `billing.services._check_starter_limit(payment)`.
  - `orders.spend.month_summary(today=None) -> dict` with keys `month`, `paid_cost_kes`, `unpaid_cost_kes`, `generated`, `paid` and `paid_share`.
  - Setting `PAYSTACK_STARTER_LIMIT_KES`.

- [ ] **Step 1: Write the failing tests.** Create `billing/tests/test_starter_limit.py`:

```python
from decimal import Decimal

import pytest
from django.core import mail

from billing import services
from billing.models import Payment
from billing.providers import PaymentEvent
from orders.models import Order


@pytest.fixture
def limit(settings):
    settings.PAYSTACK_STARTER_LIMIT_KES = 1000
    return 1000


def collect(order_factory, amount, ref, provider="paystack"):
    order = order_factory(f"{ref}.pptx", status=Order.Status.READY, price_kes=amount)
    Payment.objects.create(order=order, provider=provider, provider_ref=ref, phone="254712345678", amount_kes=amount)
    services.apply_event(provider, PaymentEvent(provider_ref=ref, state="paid", amount=Decimal(amount),
                                                fee=Decimal("1"), raw={}), source="callback")


def limit_alerts():
    return [m.subject for m in mail.outbox if "Starter limit" in m.subject]


def test_alerts_once_at_80_and_95_percent(order_factory, limit):
    collect(order_factory, 700, "A")
    assert limit_alerts() == []
    collect(order_factory, 150, "B")  # 850 of 1000
    assert limit_alerts() == ["[SUBRA] Paystack Starter limit 80% used"]
    collect(order_factory, 50, "C")  # 900
    collect(order_factory, 60, "D")  # 960
    assert limit_alerts() == ["[SUBRA] Paystack Starter limit 80% used", "[SUBRA] Paystack Starter limit 95% used"]


def test_one_payment_can_cross_both_thresholds(order_factory, limit):
    collect(order_factory, 990, "A")
    assert len(limit_alerts()) == 2


def test_refunds_dont_reset_the_count(order_factory, limit):
    collect(order_factory, 850, "A")
    Payment.objects.filter(provider_ref="A").update(status="refunded")
    collect(order_factory, 10, "B")
    assert limit_alerts() == ["[SUBRA] Paystack Starter limit 80% used"]


def test_no_alerts_once_the_business_is_registered(order_factory, settings):
    settings.PAYSTACK_STARTER_LIMIT_KES = 0
    collect(order_factory, 990, "A")
    assert limit_alerts() == []


def test_other_providers_dont_count(order_factory, limit):
    collect(order_factory, 990, "X", provider="intasend")
    assert limit_alerts() == []
```

Append to `orders/tests/test_admin.py`:
```python
def test_month_summary_splits_ai_cost_by_payment(paid_order, order_factory):
    from decimal import Decimal

    from orders.models import AIJob, OrderEvent

    unpaid = order_factory("Unpaid.pptx", status=Order.Status.READY)
    OrderEvent.objects.create(order=unpaid, from_status="processing", to_status="ready")
    AIJob.objects.create(order=paid_order, attempt_number=2, cost_kes=Decimal("20.00"))
    AIJob.objects.create(order=unpaid, attempt_number=1, cost_kes=Decimal("30.00"))
    summary = spend.month_summary()
    assert (summary["paid_cost_kes"], summary["unpaid_cost_kes"]) == (Decimal("20.00"), Decimal("30.00"))
    assert (summary["generated"], summary["paid"], summary["paid_share"]) == (2, 1, 50)


def test_daily_spend_page_shows_the_month(staff_client, paid_order):
    html = staff_client.get("/admin/orders/dailyspend/").content.decode()
    assert "This month" in html and "Decks generated: 1, paid for: 1 (100%)" in html
```

Run: `.venv/bin/python -m pytest billing/tests/test_starter_limit.py orders/tests/test_admin.py -q`
Expected: no limit alerts are sent, and `month_summary` raises `AttributeError`.

- [ ] **Step 2: Implement.**

In `subra/settings.py`, below `PAYSTACK_SECRET_KEY`:
```python
PAYSTACK_STARTER_LIMIT_KES = int(env("PAYSTACK_STARTER_LIMIT_KES", "250000"))  # 0 once the business is registered
```
In `.env.example`, below `PAYSTACK_SECRET_KEY=`:
```
# Paystack Starter Businesses stop collecting at this lifetime total; alerts at 80% and 95%. 0 once registered.
PAYSTACK_STARTER_LIMIT_KES=250000
```

In `billing/services.py`, add `from django.db.models import Sum`, then the constant and function below `MAX_STORED_CALLBACKS`:
```python
STARTER_THRESHOLDS = (0.8, 0.95)


def _check_starter_limit(payment):
    """Paystack stops a Starter Business at a lifetime limit until it's registered. Warn before that."""
    limit = settings.PAYSTACK_STARTER_LIMIT_KES
    if payment.provider != "paystack" or not limit:
        return
    collected = Payment.objects.filter(provider="paystack",
                                       status__in=[Payment.Status.PAID, Payment.Status.REFUNDED])
    total = collected.aggregate(t=Sum("amount_kes"))["t"] or 0
    before = total - payment.amount_kes  # refunds don't lower the total, so each threshold is crossed once
    for share in STARTER_THRESHOLDS:
        if before < limit * share <= total:
            alert_admins(
                f"Paystack Starter limit {round(share * 100)}% used",
                f"Paystack has collected KES {total:,} of the KES {limit:,} Starter Business limit. Payments stop "
                "at the limit until the business is registered. Register it, upgrade the Paystack account, then "
                "set PAYSTACK_STARTER_LIMIT_KES=0.",
            )
```
In `apply_event`, replace the final `_mark_paid(payment, event)` with:
```python
        _mark_paid(payment, event)
        _check_starter_limit(payment)
```

In `orders/spend.py`, add `from django.db.models import Sum`, and change the models import to `from .models import AIJob, DailySpend, Order`. Then add:
```python
def month_summary(today=None):
    """This month: AI spent on decks that were paid for and on decks that weren't, and the share paid for."""
    start = (today or timezone.localdate()).replace(day=1)
    jobs = AIJob.objects.filter(created_at__date__gte=start)

    def cost(qs):
        return qs.aggregate(t=Sum("cost_kes"))["t"] or Decimal("0")

    generated = Order.objects.filter(events__to_status=Order.Status.READY,
                                     events__created_at__date__gte=start).distinct()
    count = generated.count()
    paid = generated.filter(paid_at__isnull=False).count()
    return {
        "month": start,
        "paid_cost_kes": cost(jobs.filter(order__paid_at__isnull=False)),
        "unpaid_cost_kes": cost(jobs.filter(order__paid_at__isnull=True)),
        "generated": count,
        "paid": paid,
        "paid_share": round(100 * paid / count) if count else None,
    }
```

In `orders/admin.py`, add `from . import spend`, and add to `DailySpendAdmin`:
```python
    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "month": spend.month_summary()}
        return super().changelist_view(request, extra_context=extra_context)
```

Create `templates/admin/orders/dailyspend/change_list.html`:
```html
{% extends "admin/change_list.html" %}
{% block content_title %}{{ block.super }}
<div class="module" style="margin-bottom: 20px; padding: 10px 15px;">
  <h2>This month ({{ month.month|date:"F Y" }})</h2>
  <p>AI spent on decks that were paid for: <strong>KES {{ month.paid_cost_kes|floatformat:2 }}</strong></p>
  <p>AI spent on decks that weren't: <strong>KES {{ month.unpaid_cost_kes|floatformat:2 }}</strong></p>
  <p>Decks generated: {{ month.generated }}, paid for: {{ month.paid }}{% if month.paid_share is not None %} ({{ month.paid_share }}%){% endif %}</p>
</div>
{% endblock %}
```

- [ ] **Step 3: Run the tests.**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 4: Commit.**

```bash
git add billing orders subra/settings.py .env.example templates/admin
git commit -m "$(printf 'Warn before the Paystack Starter limit and show paid vs unpaid AI spend\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 7: Wording on the home, terms and privacy pages

**Files:**
- Modify: `templates/orders/home.html`, `templates/core/terms.html`, `templates/core/privacy.html`, `core/views.py`, `orders/views.py`
- Test: `orders/tests/test_views.py`

**Interfaces:**
- Consumes: `UNPAID_RETENTION_HOURS` (Task 4).
- Produces: `unpaid_hours` in the home and legal page contexts.

- [ ] **Step 1: Write the failing tests.** Append to `orders/tests/test_views.py`:

```python
def test_home_says_you_pay_after_seeing_the_deck(client, db):
    html = client.get("/").content.decode()
    assert "only after you see your redesigned deck" in html
    assert "Switch between themes as often as you like" in html and "If it still can't, you don't pay." in html
    assert "re-run" not in html and "full refund" not in html


def test_terms_and_privacy_describe_paying_after_the_preview(client, db):
    terms = client.get(reverse("terms")).content.decode()
    privacy = client.get(reverse("privacy")).content.decode()
    assert "re-run" not in terms and "pay by M-Pesa only if you want to download it" in terms
    assert "48 hours" in terms and "48 hours" in privacy
    assert "Paystack" in privacy and "IntaSend" not in privacy and "your email address" in privacy
```

Run: `.venv/bin/python -m pytest orders/tests/test_views.py -q -k "home_says or terms_and_privacy"`
Expected: 2 failures.

- [ ] **Step 2: Implement.**

In `core/views.py`, add `"unpaid_hours": settings.UNPAID_RETENTION_HOURS,` to `_legal_context()`. In `orders/views.py`, add the same key to `home`'s context.

In `templates/orders/home.html`:
- Line 19 becomes:
  ```html
  <p class="mt-4 text-sm text-muted">From KES {{ tiers.0.price_kes|default:"200" }} per deck. Pay with M-Pesa only after you see your redesigned deck.</p>
  ```
- Replace the second and third "How it works" items:
  ```html
      <li class="border-t-4 border-ink pt-4">
        <span class="text-4xl font-extrabold text-marigold">2</span>
        <h3 class="mt-2 text-lg font-bold">See it redesigned</h3>
        <p class="mt-1 text-muted">Pick a theme. In a few minutes you can look through every slide and try the other themes for free.</p>
      </li>
      <li class="border-t-4 border-ink pt-4">
        <span class="text-4xl font-extrabold text-marigold">3</span>
        <h3 class="mt-2 text-lg font-bold">Pay and download</h3>
        <p class="mt-1 text-muted">Pay with M-Pesa only if you like it. Open the .pptx in PowerPoint and keep editing.</p>
      </li>
  ```
- The "Prices" paragraph becomes:
  ```html
  <p class="mt-2 text-muted">One price per deck, set by its number of slides. Switch between themes as often as you like.</p>
  ```
- In the "Your content is checked" panel, replace "If it still can't, you get a full refund." with "If it still can't, you don't pay."
- Replace the "Your files are deleted" panel's heading and first sentence:
  ```html
    <h2 class="text-lg font-bold">Your files are deleted automatically</h2>
    <p class="mt-2 text-muted">Unpaid decks go after {{ unpaid_hours }} hours, paid ones {{ retention_days }} days after payment. Nobody at SUBRA opens your deck unless you ask for help. Slide text is sent to Anthropic's Claude to plan the layout. <a href="{% url 'privacy' %}" class="font-bold text-ink underline underline-offset-2">How we handle your files</a></p>
  ```

In `templates/core/terms.html`:
- Replace `<p>Each paid order includes one free re-run in a different theme.</p>` with:
  ```html
  <p>While your files are kept, you can switch between our themes as often as you like, at no charge.</p>
  ```
- The "Prices and payment" paragraph becomes:
  ```html
  <p>You see the price before we start. We redesign your deck first; you look through every slide, and pay by M-Pesa only if you want to download it. Payment unlocks the download of the editable file.</p>
  ```
- The "Refunds" paragraph becomes:
  ```html
  <p>You only pay for a deck you've seen. If you pay twice, or pay for a deck that has already been deleted, we refund the extra payment to the M-Pesa number you paid from, usually within one working day. If you're unhappy with a deck you paid for, email <a href="mailto:{{ support_email }}" class="underline underline-offset-2">{{ support_email }}</a> within {{ retention_days }} days.</p>
  ```
- In "Your content", replace "Files are deleted after {{ retention_days }} days, so download your deck promptly." with "Unpaid decks are deleted after {{ unpaid_hours }} hours and paid decks {{ retention_days }} days after payment, so download your deck promptly."

In `templates/core/privacy.html`:
- The payments bullet becomes:
  ```html
  <li>Payments: the M-Pesa number you pay from, the amount, the M-Pesa receipt number, and the status messages our payment provider sends us.</li>
  ```
- The first "How long we keep it" paragraph becomes:
  ```html
  <p>Unpaid decks and their previews are deleted automatically {{ unpaid_hours }} hours after they're ready, or after upload if you never polish them. Paid decks are deleted {{ retention_days }} days after payment. You can also discard an unpaid deck yourself at any time. Deletion is permanent: we can't recover a deck after that.</p>
  ```
- The IntaSend bullet becomes:
  ```html
  <li><strong>Paystack</strong> and <strong>Safaricom</strong> (M-Pesa): process your payment. Paystack receives your phone number, your email address and the amount.</li>
  ```

- [ ] **Step 3: Run the tests.**

Run: `npm run build:css && .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 4: Commit.**

```bash
git add templates core/views.py orders/views.py orders/tests/test_views.py static/css/app.css
git commit -m "$(printf 'Say pay-after-preview on the home, terms and privacy pages\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```

---

### Task 8: Migrate a copy of the dev database, check it in the browser, update the docs

**Files:**
- Modify: `README.md`, `PROGRESS.md`
- No code changes unless a check below finds a bug. If one does, write a failing test for it first.

- [ ] **Step 1: Migrate a copy of the dev database.** Before migrating, `db.sqlite3` has `done: 1`, `quoted: 3`.

```bash
cp db.sqlite3 "$TMPDIR/subra-migrate-check.sqlite3"
DATABASE_URL="sqlite:///$TMPDIR/subra-migrate-check.sqlite3" DJANGO_DEBUG=1 .venv/bin/python manage.py migrate
.venv/bin/python -c "import sqlite3,os; c=sqlite3.connect(os.environ['TMPDIR']+'/subra-migrate-check.sqlite3'); print(c.execute('select status, paid_at is not null, count(*) from orders_order group by 1,2').fetchall())"
```
Use the session scratchpad directory for `$TMPDIR`. Expected: `[('quoted', 0, 3), ('ready', 1, 1)]`, i.e. the `done` order maps to `ready` with `paid_at` set from its payment. If that order has no paid payment, `ready, 0`. Leave the real `db.sqlite3` alone; Joe migrates it himself.

- [ ] **Step 2: Check it in the browser.** Start the app with no payment keys, so the simulated-payment button shows:

```bash
DJANGO_DEBUG=1 POLISHER_PLANNER=heuristic PAYSTACK_SECRET_KEY= DATABASE_URL="sqlite:///$TMPDIR/subra-migrate-check.sqlite3" .venv/bin/python manage.py runserver 8765
```
Use the Playwright tools at widths 390 and 1280:
1. Sign up, upload `tests`' rich deck (build it with `deckbuilder.build_rich_deck`), and polish it.
2. On the ready page, check that the previews show, that "Pay KES 200 to download" shows, and that the delete-after-48-hours note shows.
3. Switch to Clean Slate and wait for the page to reload. Check the theme changed and the previews changed.
4. Upload a second deck and click Polish. Check the "Pay for or discard … first." message.
5. Back on the first deck, use the simulated payment, then download. Check the file opens with python-pptx.
6. Discard the second deck.

Take a screenshot of the ready page at each width, and look for overflow or broken layout. Stop the server afterwards.

- [ ] **Step 3: Update `README.md`.**
- Line 3: "pay per deck with M-Pesa" becomes "see it redesigned, then pay per deck with M-Pesa".
- Line 8: "M-Pesa payments via IntaSend (behind a provider interface)" becomes "M-Pesa payments via Paystack (behind a provider interface; IntaSend and Daraja are alternatives)". "orders with a logged state machine" becomes "orders with a logged processing lifecycle and a separate paid flag".
- Line 35 becomes: "Without `REDIS_URL`, Celery tasks run inline. Without payment keys, DEBUG mode shows a "simulate a successful payment" button on a ready deck, so you can try the whole flow."
- Line 43: "payments (IntaSend webhooks, …)" becomes "payments (Paystack and IntaSend webhooks, signatures, idempotency, amount mismatches, late and duplicate payments, the Starter-limit alerts), generation before payment, theme switching from the saved plan".
- Line 53: replace the IntaSend sentences with "Point the domain's DNS at the droplet before starting, because Paystack webhooks need HTTPS. In the Paystack dashboard, set the webhook URL to `https://<domain>/billing/webhooks/paystack/`. Webhooks are verified with `PAYSTACK_SECRET_KEY`, so there's no separate secret."
- Under "Tests", add one line: "`scripts/record_paystack_fixtures.py` re-records the Paystack test-mode fixtures (test keys only)."

- [ ] **Step 4: Update `PROGRESS.md`.**
- Change "Last updated" to today's date.
- Add to the top of "Done":
  ```markdown
  **Amendment 2 (added 5 Oct 2026)**
  - **Pay after generation:** customers polish first, look through every slide, then pay with M-Pesa to download. Failed decks cost them nothing. `Order.status` covers processing only; payment is `paid_at` plus the `Payment` records. One unpaid deck per account. Unpaid and failed decks are deleted after 48 hours, paid ones 7 days after payment, and customers can discard an unpaid deck.
  - **Free theme switching:** the verified plan is saved, so a switch re-renders it with no Claude call (verified again, rate-limited to 10 an hour). A failed or stuck switch leaves the previous deck in place.
  - **Paystack (Starter Business)** is the default provider: M-Pesa charge API, HMAC-SHA512 webhooks, status checks, coded against recorded test-mode payloads. Admin alerts at 80% and 95% of the KES 250,000 Starter limit. IntaSend and Daraja remain as alternatives.
  - Admin: paid column and filter, payments can be marked refunded, failed generations are emailed, and the Daily spend page shows this month's AI cost on paid vs unpaid decks and the share paid for.
  ```
- In "Needs Joe", replace the IntaSend item with:
  ```markdown
  - [ ] **Paystack:** submit Starter KYC for live keys, set the webhook URL `https://<domain>/billing/webhooks/paystack/` in the dashboard, and put the live key in `PAYSTACK_SECRET_KEY` on the server. Register the business before collections reach KES 250,000 (the 80% alert is the reminder), then set `PAYSTACK_STARTER_LIMIT_KES=0`.
  ```
- In "Next", item 2 becomes "Deploy to a droplet with Paystack test keys, then run a real end-to-end payment."
- Add to "Known issues and gaps":
  ```markdown
  - Every generation now costs AI money whether or not it's paid for. Watch the paid share on the Daily spend admin page; if it's low, the levers are a lifetime cap on unpaid generations, email verification, or a deposit.
  - The Paystack provider was built from test-mode payloads. Check the first live `charge.success` webhooks (a payment's raw callbacks in admin) for the M-Pesa receipt field.
  ```

- [ ] **Step 5: Run the whole suite one last time and commit.**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.
```bash
git add README.md PROGRESS.md
git commit -m "$(printf 'Update README and progress notes for Amendment 2\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')"
```
