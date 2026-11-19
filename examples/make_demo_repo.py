#!/usr/bin/env python3
"""Build "ledgerline", a small fictional billing service with five years of history.

    python examples/make_demo_repo.py /tmp/ledgerline
    pr-archaeologist dig --repo /tmp/ledgerline

The history is invented but shaped like a real one: a retry wrapper that double-charged
customers, an optimisation that was reverted two days later, a module rename, a deleted
legacy client and three ADRs. Dates and authors are fixed, so the generated repository
(and therefore the sample output in examples/sample-output) is reproducible.
"""

from __future__ import annotations

import os
import subprocess
import sys

MAYA = ("Maya Okafor", "maya@ledgerline.example")
TOMAS = ("Tomás Reyes", "tomas@ledgerline.example")
PRIYA = ("Priya Nair", "priya@ledgerline.example")
LENA = ("Lena Fischer", "lena@ledgerline.example")
DEV = ("Dev Patel", "dev@ledgerline.example")


def retry_src(v: int) -> str:
    """payments/retry.py as it looked after its v-th change."""
    lines = ['"""Retry wrapper around gateway calls."""', "", "import time"]
    if v >= 5:
        lines.append("import random")
    lines.append("")
    lines.append("from payments import gateway")
    if v >= 4:
        lines.append("from payments.idempotency import key_for")
    if v == 6:
        lines += ["", "_TOKEN = None"]
    lines += ["", "MAX_ATTEMPTS = 3"]
    lines.append("BASE_DELAY = 0.2")
    if v >= 9:
        lines.append("MAX_DELAY = 30.0")
    lines += ["", ""]
    if v >= 5:
        lines += [
            "def backoff(attempt):",
            '    """Exponential backoff with full jitter."""',
            "    ceiling = BASE_DELAY * (2 ** attempt)",
        ]
        if v >= 9:
            lines.append("    ceiling = min(ceiling, MAX_DELAY)")
        lines += ["    return random.uniform(0, ceiling)", "", ""]
    lines.append("def charge_with_retry(order, card):")
    lines.append('    """Charge a card, retrying transient gateway failures."""')
    if v >= 4:
        lines.append("    key = key_for(order)")
    elif v == 3:
        lines.append("    key = 'order-%s' % order.id")
    if v == 6:
        lines += ["    global _TOKEN", "    if _TOKEN is None:", "        _TOKEN = gateway.fetch_token()"]
    loop = "for attempt in range(MAX_ATTEMPTS):" if v >= 7 else "for attempt in range(MAX_ATTEMPTS + 1):"
    lines.append("    " + loop)
    lines.append("        try:")
    call = "gateway.charge(order, card"
    if v >= 3:
        call += ", idempotency_key=key"
    if v == 6:
        call += ", token=_TOKEN"
    lines.append(f"            result = {call})")
    if v >= 8:
        lines.append("            return record_result(order, result, key)")
    else:
        lines.append("            return result")
    lines.append("        except gateway.Timeout:")
    lines.append("            time.sleep(backoff(attempt))" if v >= 5 else "            time.sleep(BASE_DELAY)")
    if v >= 2:
        lines += [
            "        except gateway.Declined:",
            "            # WARNING: do not retry responses the gateway actually answered (see #19)",
            "            raise",
        ]
    else:
        lines += ["        except gateway.GatewayError:", "            time.sleep(BASE_DELAY)"]
    lines.append("    raise gateway.Timeout('gateway unavailable after %d attempts' % MAX_ATTEMPTS)")
    if v >= 8:
        lines += [
            "", "",
            "def record_result(order, result, key):",
            '    """Write the charge to the ledger exactly once (webhooks race with us)."""',
            "    from payments import ledger",
            "    ledger.record_once(key, order, result)",
            "    return result",
        ]
    return "\n".join(lines) + "\n"


def gateway_src(v: int) -> str:
    lines = ['"""HTTP client for the card gateway."""', "", "import logging", "import urllib.request", "",
             "log = logging.getLogger(__name__)", "", "",
             "class GatewayError(Exception):", "    pass", "", "",
             "class Timeout(GatewayError):", "    pass", "", ""]
    if v >= 2:
        lines += ["class Declined(GatewayError):", "    pass", "", ""]
    sig = "def charge(order, card"
    if v >= 3:
        sig += ", idempotency_key=None"
    if v == 5:
        sig += ", token=None"
    lines.append(sig + "):")
    lines.append('    """Submit one charge. Raises Timeout when the gateway does not answer."""')
    lines.append("    headers = {'Content-Type': 'application/json'}")
    if v >= 3:
        lines += ["    if idempotency_key:", "        headers['Idempotency-Key'] = idempotency_key"]
    if v == 5:
        lines.append("    headers['Authorization'] = 'Bearer ' + (token or fetch_token())")
    else:
        lines.append("    headers['Authorization'] = 'Bearer ' + fetch_token()")
    lines.append("    # HACK: the gateway answers 200 with an error body when it rate-limits us")
    lines.append("    response = _post('/charges', order.to_payload(card), headers)")
    lines.append("    if response.get('error'):")
    if v >= 6:
        lines.append("        log.error('charge failed for card ending %s', card.number[-4:])")
    else:
        lines.append("        log.error('charge failed for card %s', card.number)")
    lines.append("        raise GatewayError(response['error'])")
    lines.append("    return response")
    lines += ["", "", "def fetch_token():", "    return _post('/token', {}, {})['token']",
              "", "", "def _post(path, payload, headers):",
              "    request = urllib.request.Request('https://gateway.example' + path, headers=headers)",
              "    raise NotImplementedError(request)"]
    return "\n".join(lines) + "\n"


def ledger_src(v: int) -> str:
    amount = "int" if v >= 3 else "float"
    lines = ['"""Double-entry ledger for charges and refunds."""', "", "ENTRIES = []"]
    if v >= 5:
        lines.append("SEEN_KEYS = set()")
    lines += ["", "", "def record(order, amount):", f"    ENTRIES.append((order.id, {amount}(amount)))", "", "",
              "def total(currency):"]
    if v >= 2:
        lines.append("    return sum(amount for _id, amount in ENTRIES)  # minor units, see currency.py")
    else:
        lines.append("    return round(sum(amount for _id, amount in ENTRIES), 2)")
    if v >= 4:
        lines += ["", "", "def entries(include_refunds=True):",
                  "    # Refunds are negative entries: never filter on amount > 0 here.",
                  "    return [e for e in ENTRIES if include_refunds or e[1] > 0]"]
    if v >= 5:
        lines += ["", "", "def record_once(key, order, result):",
                  '    """Idempotent write: the webhook and the retry loop both call this."""',
                  "    if key in SEEN_KEYS:", "        return False", "    SEEN_KEYS.add(key)",
                  "    record(order, result['amount'])", "    return True"]
    return "\n".join(lines) + "\n"


def idempotency_src(v: int) -> str:
    body = "    return 'charge-%s' % order.id" if v == 1 else \
        "    return 'charge-%s-%s-%s' % (order.id, order.amount_minor, order.currency)"
    return '"""Idempotency keys for gateway writes (ADR-0002)."""\n\n\ndef key_for(order):\n' \
           '    """Stable key: the same logical charge must always produce the same key."""\n' + body + "\n"


def currency_src(v: int) -> str:
    lines = ['"""Money helpers. All amounts are integer minor units (ADR-0003)."""', "",
             "MINOR_UNITS = {'EUR': 100, 'USD': 100, 'JPY': 1}", "", "",
             "def to_minor(amount, currency):",
             "    return int(round(amount * MINOR_UNITS[currency]))"]
    if v >= 2:
        lines += ["", "", "def format_minor(minor, currency):",
                  "    units = MINOR_UNITS[currency]",
                  "    return '%d.%02d %s' % (minor // units, minor % units, currency) if units > 1 else '%d %s' % (minor, currency)"]
    return "\n".join(lines) + "\n"


def handlers_src(v: int) -> str:
    lines = ['"""HTTP handlers for checkout."""', ""]
    lines.append("from payments import gateway" if v == 1 else "from payments.retry import charge_with_retry")
    if v >= 3:
        lines.append("from payments.currency import to_minor")
    lines += ["", "", "def checkout(request):", "    order = request.order"]
    if v >= 3:
        lines.append("    order.amount_minor = to_minor(order.amount, order.currency)")
    lines.append("    return gateway.charge(order, request.card)" if v == 1 else "    return charge_with_retry(order, request.card)")
    if v >= 4:
        lines += ["", "", "def register_routes(app):", "    from api import webhooks",
                  "    app.post('/checkout', checkout)", "    app.post('/webhooks/gateway', webhooks.gateway_event)"]
    return "\n".join(lines) + "\n"


def reconcile_src(v: int) -> str:
    module = "ledger" if v >= 2 else "books"
    lines = ['"""Nightly reconciliation between our ledger and the gateway settlement file."""', "",
             f"from payments import {module}", "", "",
             "def reconcile(settlement):"]
    if v >= 3:
        lines.append(f"    ours = sum(amount for _id, amount in {module}.entries(include_refunds=True))")
    else:
        lines.append(f"    ours = sum(amount for _id, amount in {module}.ENTRIES if amount > 0)")
    lines += ["    theirs = sum(row.amount for row in settlement)", "    return ours - theirs"]
    return "\n".join(lines) + "\n"


def webhooks_src(v: int) -> str:
    lines = ['"""Asynchronous results pushed by the gateway."""', "", "from payments import ledger"]
    if v >= 2:
        lines.append("from payments.idempotency import key_for")
    lines += ["", "", "def gateway_event(request):", "    event = request.json"]
    if v >= 2:
        lines.append("    ledger.record_once(key_for(event.order), event.order, event.result)")
    else:
        lines.append("    ledger.record(event.order, event.result['amount'])")
    lines.append("    return {'ok': True}")
    return "\n".join(lines) + "\n"


SOAP = '"""SOAP client for the pre-2019 gateway. Only merchant #7 still uses it."""\n\n\ndef charge_soap(order, card):\n    raise NotImplementedError\n'

ADR1 = """# 1. Record architecture decisions

Date: 2019-05-20

## Status

Accepted

## Context

Billing decisions keep getting re-litigated because nobody remembers why they were made.

## Decision

We will record significant decisions as short ADRs in `docs/adr`.

## Consequences

A change that contradicts an ADR needs a new ADR that supersedes it.
"""

ADR2 = """# 2. Idempotency keys on every gateway write

Date: 2019-06-25

## Status

Accepted

## Context

On 2019-06-08 a timed-out charge was retried by `payments/retry.py` although the gateway had
already processed it. Fourteen customers were charged twice. A timeout tells us nothing about
whether the charge happened.

## Decision

Every write to the gateway carries an `Idempotency-Key` header generated by
`payments/idempotency.py`. The key is derived from the logical charge, never from the attempt.

## Consequences

Retries are safe only while the key is stable. Anything that changes how the key is built, or
calls `payments/gateway.py` without one, can double-charge customers again.
"""

ADR3 = """# 3. Money is integer minor units

Date: 2020-02-24

## Status

Accepted

## Context

Float arithmetic in the ledger accumulated a 37 EUR mismatch against settlement in January 2020.

## Decision

All amounts are stored and computed as integer minor units. Conversion happens once, at the API
boundary, in `payments/currency.py`.

## Consequences

Never introduce a float amount into `payments/ledger.py`. Currencies without minor units (JPY)
must go through the `MINOR_UNITS` table.
"""

RUNBOOK = """# Payment incident runbook

1. Check the gateway status page.
2. If charges are timing out, `payments/retry.py` will back off on its own. Do not raise
   `MAX_ATTEMPTS` during an incident: it multiplies load on a struggling gateway.
3. After the incident, run `workers/reconcile.py` by hand and compare against settlement.
"""

README1 = "# ledgerline\n\nBilling service extracted from the storefront monolith.\n"
README2 = README1 + "\nRequires Python 3.12.\n"
TEST_RETRY = "from payments import retry\n\n\ndef test_budget():\n    assert retry.MAX_ATTEMPTS == 3\n\n\ndef test_backoff_is_bounded():\n    assert retry.backoff(50) >= 0\n"

# (author, date, message, {path: content | None to delete}, [(old, new) renames])
STEPS = [
    (MAYA, "2019-03-04T10:12:00", "Initial import of billing service\n\nExtracted from the storefront monolith so that billing can deploy independently of the storefront release train.",
     {"README.md": README1, "payments/__init__.py": "", "payments/gateway.py": gateway_src(1), "payments/books.py": ledger_src(1),
      "api/__init__.py": "", "api/handlers.py": handlers_src(1), "legacy/soap_client.py": SOAP}, []),
    (MAYA, "2019-03-18T15:40:00", "Add retry wrapper for gateway timeouts (#12)\n\nThe gateway drops roughly 1 in 100 calls under load. Retry up to 3 times with a fixed 200ms delay so that checkout does not surface transient errors to customers.",
     {"payments/retry.py": retry_src(1), "api/handlers.py": handlers_src(2)}, []),
    (TOMAS, "2019-04-02T09:05:00", "fix: retry only on timeouts, not on declined cards (#19)\n\nWe were retrying 402 responses, which caused duplicate decline emails. Never retry a response the gateway actually answered.",
     {"payments/retry.py": retry_src(2), "payments/gateway.py": gateway_src(2)}, []),
    (MAYA, "2019-05-20T11:00:00", "docs: start recording architecture decisions", {"docs/adr/0001-record-architecture-decisions.md": ADR1}, []),
    (PRIYA, "2019-06-11T22:31:00", "fix: double charge when a timed-out request had succeeded (#31)\n\nRoot cause: a timeout does not mean the charge failed. The gateway had processed the first request and our retry charged the card again; 14 customers were charged twice on 2019-06-08.\n\nHotfix: send an Idempotency-Key header on every charge. Do not remove the header without reading ADR-0002.\n\nFixes #30",
     {"payments/retry.py": retry_src(3), "payments/gateway.py": gateway_src(3)}, []),
    (PRIYA, "2019-06-25T14:20:00", "Introduce idempotency keys for all gateway writes (ADR-0002) (#34)\n\nReplaces the hotfix from #31 with a proper key generator so that every caller builds keys the same way.",
     {"payments/idempotency.py": idempotency_src(1), "payments/retry.py": retry_src(4), "docs/adr/0002-idempotency-keys.md": ADR2}, []),
    (TOMAS, "2019-09-03T16:45:00", "perf: switch retry to exponential backoff with jitter (#41)\n\nFixed 200ms retries synchronised across workers and hammered the gateway during its own incidents. Backoff with full jitter spreads the load instead of amplifying it.",
     {"payments/retry.py": retry_src(5)}, []),
    (LENA, "2020-01-14T10:30:00", "Add nightly reconciliation worker (#52)\n\nSo that ledger drift is caught within a day instead of at month end.",
     {"workers/__init__.py": "", "workers/reconcile.py": reconcile_src(1)}, []),
    (LENA, "2020-02-10T13:15:00", "fix: rounding drift in multi-currency totals (#57)\n\nFloats lose cents: errors accumulated to a 37 EUR mismatch against settlement in January. Because of this, totals are now summed in minor units.",
     {"payments/currency.py": currency_src(1), "payments/books.py": ledger_src(2)}, []),
    (LENA, "2020-02-24T09:50:00", "Adopt integer minor units everywhere (ADR-0003) (#58)\n\nWe decided to convert once at the API boundary rather than in every module that touches money.",
     {"payments/currency.py": currency_src(2), "payments/books.py": ledger_src(3), "api/handlers.py": handlers_src(3),
      "docs/adr/0003-integer-minor-units.md": ADR3}, []),
    (TOMAS, "2020-06-02T11:25:00", "refactor: rename books module to ledger (#63)\n\n\"books\" confused every new joiner; accounting calls it the ledger.",
     {"workers/reconcile.py": reconcile_src(2)}, [("payments/books.py", "payments/ledger.py")]),
    (DEV, "2020-09-15T17:02:00", "Cache gateway auth token in retry loop (#71)\n\nSaves one round trip per attempt.",
     {"payments/retry.py": retry_src(6), "payments/gateway.py": gateway_src(5)}, []),
    (PRIYA, "2020-09-17T08:44:00", "REVERT_PREVIOUS",
     {"payments/retry.py": retry_src(5), "payments/gateway.py": gateway_src(3)}, []),
    (DEV, "2021-01-20T14:10:00", "fix: reconcile worker skipped refunds (#80)\n\nRefunds are stored as negative entries and the worker only summed amount > 0, which caused a false mismatch on every refund day.",
     {"workers/reconcile.py": reconcile_src(3), "payments/ledger.py": ledger_src(4)}, []),
    (LENA, "2021-03-08T10:05:00", "fix: off-by-one in retry budget (#84)\n\nMAX_ATTEMPTS=3 made 4 calls because the loop counted retries, not attempts.",
     {"payments/retry.py": retry_src(7)}, []),
    (MAYA, "2021-07-19T12:00:00", "Remove legacy SOAP client (#90)\n\nThe last merchant on the SOAP gateway migrated in June, so this code path is dead. Removing it also keeps the zeep dependency out of the image.",
     {"legacy/soap_client.py": None}, []),
    (DEV, "2021-11-02T19:30:00", "security: mask card numbers in gateway error logs (#97)\n\nThe full card number was written to logs when a charge failed. Log the last four digits only.",
     {"payments/gateway.py": gateway_src(6)}, []),
    (PRIYA, "2022-04-12T15:55:00", "fix: idempotency key reused across different amounts (#104)\n\nThe key was derived from the order id only, so a second capture for a different amount was treated as a replay and silently dropped. The key must include amount and currency.",
     {"payments/idempotency.py": idempotency_src(2)}, []),
    (LENA, "2022-08-30T09:00:00", "docs: add on-call runbook for payment incidents", {"docs/runbook.md": RUNBOOK}, []),
    (DEV, "2023-02-14T13:40:00", "Add webhook handler for async gateway results (#118)\n\nThe gateway now confirms slow charges by webhook instead of making us poll.",
     {"api/webhooks.py": webhooks_src(1), "api/handlers.py": handlers_src(4)}, []),
    (TOMAS, "2023-05-09T21:15:00", "fix: race between webhook and retry loop recording a charge twice (#123)\n\nThe webhook and the retry loop could both write the same charge to the ledger. Ledger writes now go through record_once(), keyed on the idempotency key. Always use record_once() for charges; record() is for manual adjustments only.",
     {"api/webhooks.py": webhooks_src(2), "payments/retry.py": retry_src(8), "payments/ledger.py": ledger_src(5)}, []),
    (PRIYA, "2023-10-03T10:20:00", "test: cover retry budget and backoff bounds", {"tests/test_retry.py": TEST_RETRY}, []),
    (DEV, "2024-03-18T07:35:00", "fix: cap backoff at 30s so workers do not stall (#131)\n\nUncapped exponential backoff reached 17 minutes during the March gateway outage and starved the queue.",
     {"payments/retry.py": retry_src(9)}, []),
    (LENA, "2024-06-05T16:00:00", "chore: bump python to 3.12", {"README.md": README2}, []),
]


def run(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def build(target: str) -> None:
    if os.path.exists(target) and os.listdir(target):
        raise SystemExit(f"{target} already exists and is not empty")
    os.makedirs(target, exist_ok=True)
    run(target, "init", "-q", "-b", "main")
    previous = None
    for (name, email), date, message, files, renames in STEPS:
        for old, new in renames:
            os.makedirs(os.path.dirname(os.path.join(target, new)), exist_ok=True)
            run(target, "mv", old, new)
        for path, content in files.items():
            full = os.path.join(target, path)
            if content is None:
                run(target, "rm", "-q", path)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
        if message == "REVERT_PREVIOUS":
            message = (
                'Revert "Cache gateway auth token in retry loop (#71)"\n\n'
                f"This reverts commit {previous}.\n\n"
                "The cached token expired mid-retry, which caused every attempt after the first to fail with 401 "
                "and masked real timeouts. Tokens must be fetched per attempt until the gateway supports refresh."
            )
        stamp = date + "+00:00"
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=stamp,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email, GIT_COMMITTER_DATE=stamp)
        run(target, "add", "-A")
        run(target, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=env)
        previous = run(target, "rev-parse", "HEAD")
    print(f"built demo repo with {len(STEPS)} commits at {target}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "ledgerline")
