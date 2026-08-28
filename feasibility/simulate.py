"""Date-by-date ledger simulation and optimal program-fee placement.

The whole objective -- "collect the program fee as early as possible" -- is
resolved here, exactly, in two passes:

1.  Walk the ledger with the creditor payments and bank fees but **no** fee, and
    record the balance after every date's full activity.
2.  Take suffix minima of those balances. Fee collected at cadence date `d`
    depresses the balance at *every* later date, so the most we may take at `d`
    is ``min(balance at any date >= d) - (fee already taken)``. Taking that much
    at every cadence date in order is the lexicographic maximum of the
    cumulative-fee vector, and if it cannot finish the fee by the horizon, no
    allocation can.

A forward greedy without step 2 is wrong: it minimises the balance at every
future date and can starve a later creditor payment.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from feasibility.models import Client, CreditorRules
from feasibility.results import ScheduleRow


@dataclass(frozen=True)
class Timeline:
    """The dated activity a candidate is simulated against.

    Independent of the payment vector, so it is built once per (client,
    extra_credits) and reused across every candidate rather than rebuilt and
    re-sorted on each call.
    """

    credits: dict[date, int]
    debits: dict[date, int]
    dates: list[date]


@dataclass(frozen=True)
class Trace:
    """Intermediate state of a run, for inspection. See tools/trace.py."""

    dates: list[date]
    credits: dict[date, int]
    debits: dict[date, int]
    # Pass A: the trajectory the fee is then carved out of.
    balances: dict[date, int]
    # Pass B: min balance from d onward, so fee pulled at d cannot starve a later date.
    suffix_min: dict[date, int]
    fee_on: dict[date, int]
    reason: str  # empty when the run succeeded


@dataclass(frozen=True)
class Sim:
    ok: bool
    rows: list[ScheduleRow]
    # One entry per cadence date, so candidates compare element-wise.
    cum_fee: tuple[int, ...]
    # None on the hot path; only built under trace=True.
    trace: Trace | None = None


INFEASIBLE = Sim(False, [], ())


def future_entries(
    client: Client, extra_credits: Iterable[tuple[date, int]] = ()
) -> tuple[dict[date, int], dict[date, int]]:
    """Credits and debits per date, for the modifiable future only.

    Entries dated on or before ``as_of_date`` are already baked into
    ``current_balance_cents`` (S3) and must not be applied twice.
    """
    credits: dict[date, int] = defaultdict(int)
    debits: dict[date, int] = defaultdict(int)
    for e in client.ledger:
        if e.date <= client.as_of_date:
            continue
        if e.type == "credit":
            credits[e.date] += e.amount_cents
        else:
            debits[e.date] += e.amount_cents
    for d, amount in extra_credits:
        credits[d] += amount
    return credits, debits


def build_timeline(
    client: Client,
    cadence: Sequence[date],
    extra_credits: Iterable[tuple[date, int]] = (),
) -> Timeline:
    """Aggregate the future ledger and the cadence into one dated view."""
    credits, debits = future_entries(client, extra_credits)
    return Timeline(credits, debits, sorted(set(credits) | set(debits) | set(cadence)))


def simulate(
    client: Client,
    cadence: Sequence[date],
    payments: Sequence[int],
    rules: CreditorRules,
    fee_total: int,
    extra_credits: Iterable[tuple[date, int]] = (),
    trace: bool = False,
    timeline: Timeline | None = None,
) -> Sim:
    """Simulate one candidate payment vector and place the fee optimally.

    ``trace=True`` attaches the intermediate pass-A balances, suffix minima and
    fee placement to the result, for tools/trace.py. It changes nothing about
    the answer.

    Pass ``timeline`` to reuse a prebuilt Timeline across candidates. It already
    carries ``extra_credits``, which is then ignored.
    """
    if timeline is None:
        timeline = build_timeline(client, cadence, extra_credits)
    credits, debits, all_dates = timeline.credits, timeline.debits, timeline.dates
    pay_on = {cadence[i]: payments[i] for i in range(len(payments))}

    # Credits before debits (S3); checked once per date, after the day has landed.
    balance = client.current_balance_cents
    balances: dict[date, int] = {}
    for d in all_dates:
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        if pay_on.get(d, 0):
            balance -= pay_on[d] + rules.bank_fee_cents
        if balance < 0:
            balances[d] = balance
            return _fail(
                f"balance {balance} < 0 on {d}",
                trace, all_dates, credits, debits, balances, {}, {},
            )
        balances[d] = balance

    # Over *all* dates: a debit between cadence dates also limits an early pull.
    suffix_min: dict[date, int] = {}
    running: int | None = None
    for d in reversed(all_dates):
        running = balances[d] if running is None else min(running, balances[d])
        suffix_min[d] = running

    # Starting at cadence index 0 (= first_payment_date) satisfies S5.6a by construction.
    remaining = fee_total
    taken = 0
    fee_on: dict[date, int] = {}
    cum_fee: list[int] = []
    for d in cadence:
        amount = max(0, min(remaining, suffix_min[d] - taken))
        fee_on[d] = amount
        taken += amount
        remaining -= amount
        cum_fee.append(taken)
    if remaining > 0:
        # Greedy already took the most possible, so nothing else could finish it (S5.6b).
        return _fail(
            f"{remaining} of the {fee_total} program fee is still uncollected at the horizon",
            trace, all_dates, credits, debits, balances, suffix_min, fee_on,
        )

    rows: list[ScheduleRow] = []
    taken = 0
    for d in cadence:
        fee = fee_on[d]
        taken += fee
        payment = pay_on.get(d, 0)
        if payment == 0 and fee == 0:
            continue  # unused cadence date
        rows.append(
            ScheduleRow(
                date=d,
                creditor_payment_cents=payment,
                program_fee_cents=fee,
                # S5.5: no bank fee on a fee-only date.
                bank_fee_cents=rules.bank_fee_cents if payment else 0,
                balance_cents=balances[d] - taken,
            )
        )
    detail = (
        Trace(all_dates, dict(credits), dict(debits), balances, suffix_min, fee_on, "")
        if trace
        else None
    )
    return Sim(True, rows, tuple(cum_fee), detail)


def _fail(reason, trace, dates, credits, debits, balances, suffix_min, fee_on) -> Sim:
    if not trace:
        return INFEASIBLE
    return Sim(
        False,
        [],
        (),
        Trace(dates, dict(credits), dict(debits), balances, suffix_min, fee_on, reason),
    )
