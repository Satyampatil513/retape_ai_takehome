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
class Sim:
    ok: bool
    rows: list[ScheduleRow]
    # Cumulative fee collected as of each cadence date. Always one entry per
    # cadence date, so vectors from different candidates compare element-wise.
    cum_fee: tuple[int, ...]


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


def simulate(
    client: Client,
    cadence: Sequence[date],
    payments: Sequence[int],
    rules: CreditorRules,
    fee_total: int,
    extra_credits: Iterable[tuple[date, int]] = (),
) -> Sim:
    """Simulate one candidate payment vector and place the fee optimally."""
    credits, debits = future_entries(client, extra_credits)
    pay_on = {cadence[i]: payments[i] for i in range(len(payments))}
    all_dates = sorted(set(credits) | set(debits) | set(cadence))

    # Pass A -- no fee. Credits before debits on each date (S3); the balance is
    # checked once per date, after everything that day has landed.
    balance = client.current_balance_cents
    balances: dict[date, int] = {}
    for d in all_dates:
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        if pay_on.get(d, 0):
            balance -= pay_on[d] + rules.bank_fee_cents
        if balance < 0:
            return INFEASIBLE
        balances[d] = balance

    # Suffix minima over *all* dates, not just cadence dates: a fixed ledger
    # debit between two cadence dates constrains how much fee we may pull early.
    suffix_min: dict[date, int] = {}
    running: int | None = None
    for d in reversed(all_dates):
        running = balances[d] if running is None else min(running, balances[d])
        suffix_min[d] = running

    # Pass B -- fee, front-loaded as hard as the suffix minima allow. The first
    # cadence date IS first_payment_date, so S5.6a (no fee before the first
    # creditor payment) holds by construction.
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
        return INFEASIBLE  # fee not fully collected by the horizon (S5.6b)

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
                # A fee-only date carries no bank fee (S5.5).
                bank_fee_cents=rules.bank_fee_cents if payment else 0,
                balance_cents=balances[d] - taken,
            )
        )
    return Sim(True, rows, tuple(cum_fee))
