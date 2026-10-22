#!/usr/bin/env bash
# Run SUBRA on your laptop for testing:
#   scripts/dev.sh                 # http://127.0.0.1:8000, Paystack test mode
#   scripts/dev.sh 8989            # another port
#   ALLOW_LIVE=1 scripts/dev.sh    # REAL M-Pesa payments with PAYSTACK_LIVE_SECRET_KEY from .env
#   POLISHER_PLANNER=claude scripts/dev.sh   # plan layouts with Claude (ANTHROPIC_API_KEY from .env)
#
# .env holds the server's settings (debug off, the real hostname, SMTP), and VS Code loads it into every
# terminal, so this overrides the values that would break a local run. A live key is used only when you
# ask for it with ALLOW_LIVE=1.
set -euo pipefail
cd "$(dirname "$0")/.."

export DJANGO_DEBUG=1
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
export DJANGO_CSRF_TRUSTED_ORIGINS=
export EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend  # alert emails print in this terminal
export POLISHER_PLANNER="${POLISHER_PLANNER:-heuristic}"  # no Claude calls unless you ask for them
unset DATABASE_URL REDIS_URL SPACES_BUCKET CELERY_TASK_ALWAYS_EAGER  # SQLite, jobs run inline, files in media/

env_value() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }

if [[ "$POLISHER_PLANNER" == claude ]]; then
    key="$(env_value ANTHROPIC_API_KEY)"
    if [[ "$key" != sk-ant-* ]]; then
        echo "POLISHER_PLANNER=claude needs ANTHROPIC_API_KEY=sk-ant-... in .env." >&2
        exit 1
    fi
    export ANTHROPIC_API_KEY="$key"
    echo "Claude planner on: every deck you polish uses real API credit (roughly KES 20-90 each)."
fi

if [[ "${ALLOW_LIVE:-}" == 1 ]]; then
    key="$(env_value PAYSTACK_LIVE_SECRET_KEY)"
    if [[ "$key" != sk_live_* ]]; then
        echo "ALLOW_LIVE=1 needs PAYSTACK_LIVE_SECRET_KEY=sk_live_... in .env." >&2
        exit 1
    fi
    export PAYSTACK_SECRET_KEY="$key"
    echo "!!! LIVE MODE: payments take REAL money from the M-Pesa number you enter. !!!"
    # Without a worker the app checks a payment the moment it starts, before you can enter your PIN, and no
    # webhook reaches a laptop. So keep asking Paystack about pending payments until they settle.
    .venv/bin/python manage.py shell -c "
import time
from billing.models import Payment
from billing.services import check_payment
while True:
    for payment in Payment.objects.filter(status=Payment.Status.PENDING):
        if check_payment(payment) != Payment.Status.PENDING:
            print(f'Payment {payment.pk}: {Payment.objects.get(pk=payment.pk).status}', flush=True)
    time.sleep(15)
" &
    checker=$!
    trap 'kill "$checker" 2>/dev/null' EXIT
else
    key="$(env_value PAYSTACK_SECRET_KEY)"
    if [[ "$key" == sk_test_* ]]; then
        export PAYSTACK_SECRET_KEY="$key"
        echo "Paystack test mode: pay with M-Pesa number 0710 000 000."
    else
        export PAYSTACK_SECRET_KEY=
        echo "No Paystack test key in .env: ready decks show a simulated payment button instead."
    fi
fi

.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py runserver "127.0.0.1:${1:-8000}"
