"""Part 2: the minimum extra money that would make an infeasible offer fit.

Two independent questions (ASSIGNMENT.md S8):

  lump sum           the smallest single extra credit L, on a date we choose
  monthly increment  the smallest uniform X added to every future draft

Both are found by binary search over cents. Feasibility is monotone in each --
extra money only ever raises the running balance, and it never removes a
schedule from the candidate set -- so once an amount works, every larger amount
works, which is exactly what bisection needs.

The two answers can imply very different totals, and that is expected: an
increment lands spread across the remaining drafts, and cash that arrives after
the last usable cadence date buys nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

from feasibility.models import Client, CreditorRules, Offer, offer_total_cents, program_fee_cents
from feasibility.money import pct_of_cents
from feasibility.results import AdditionalFunds, FundsOption
from feasibility.shapes import cadence_dates
from feasibility.solver import is_feasible

# S8 guardrails.
INCREMENT_FLOOR_CENTS = 10000  # X may always reach $100 regardless of draft size
INCREMENT_PCT_OF_DRAFT = 0.40
LUMP_PCT_OF_OFFER = 0.65


def _search_ceiling(client: Client, offer: Offer, rules: CreditorRules) -> int:
    """An amount certainly large enough, if anything is.

    Everything we could ever debit (the settlement, our fee, every bank fee)
    plus every fixed debit already on the books. Sitting in the account from
    the start, that cannot leave the balance negative on any date.
    """
    cadence = cadence_dates(client, offer)
    k_max = max(1, min(rules.max_payments, rules.max_terms, len(cadence)))
    fixed_debits = sum(
        e.amount_cents
        for e in client.ledger
        if e.date > client.as_of_date and e.type == "debit"
    )
    return (
        offer_total_cents(offer)
        + program_fee_cents(offer, rules)
        + rules.bank_fee_cents * k_max
        + fixed_debits
        + 1
    )


def _bisect(predicate, hi: int) -> int | None:
    """Smallest v in [1, hi] with predicate(v), or None if even hi fails."""
    if not predicate(hi):
        return None
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        if predicate(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def future_draft_dates(client: Client) -> list[date]:
    """Dates of the drafts we can still modify -- the credits after as_of_date.

    S3: "the credits in the ledger **are** the drafts". One entry per draft, so
    two credits on one date count as two drafts.
    """
    return [
        e.date for e in client.ledger if e.date > client.as_of_date and e.type == "credit"
    ]


def minimum_lump_sum(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """Smallest single extra credit that makes some valid schedule fit.

    Placed on the earliest date we are allowed to touch (the day after
    as_of_date). An earlier lump is weakly more useful -- it is available to
    every subsequent date -- so placing it first is what minimises L.
    """
    when = client.as_of_date + timedelta(days=1)
    if when > client.last_draft_date:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no date at or before the horizon is still modifiable",
            date=None,
        )

    amount = _bisect(
        lambda v: is_feasible(client, offer, rules, [(when, v)]),
        _search_ceiling(client, offer, rules),
    )
    if amount is None:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no lump sum makes this offer schedulable",
            date=None,
        )

    cap = pct_of_cents(LUMP_PCT_OF_OFFER, offer_total_cents(offer))
    within = amount <= cap
    reason = "" if within else f"lump sum {amount} exceeds {cap} (65% of the offer total)"
    return FundsOption(amount_cents=amount, within_guardrail=within, reason=reason, date=when)


def minimum_monthly_increment(
    client: Client, offer: Offer, rules: CreditorRules
) -> FundsOption:
    """Smallest uniform amount added to every future draft that makes it fit."""
    dates = future_draft_dates(client)
    n = len(dates)
    if n == 0:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no future drafts remain to increase",
            num_drafts=0,
        )

    amount = _bisect(
        lambda v: is_feasible(client, offer, rules, [(d, v) for d in dates]),
        _search_ceiling(client, offer, rules),
    )
    if amount is None:
        # Every remaining draft can land after the last usable cadence date.
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no monthly increment makes this offer schedulable",
            num_drafts=n,
        )

    cap = max(
        INCREMENT_FLOOR_CENTS,
        pct_of_cents(INCREMENT_PCT_OF_DRAFT, client.draft_amount_cents),
    )
    within = amount <= cap
    reason = "" if within else f"increment {amount} exceeds {cap} (max of $100 and 40% of the draft)"
    return FundsOption(
        amount_cents=amount, within_guardrail=within, reason=reason, num_drafts=n
    )


def additional_funds(
    client: Client, offer: Offer, rules: CreditorRules, structurally_possible: bool = True
) -> AdditionalFunds:
    """Both minima. When the offer is unschedulable on structure alone -- the
    creditor's own rules admit no valid payment vector -- no amount of money
    helps, and we say so rather than bisecting toward the ceiling."""
    if not structurally_possible:
        blocked = "no schedule satisfies the creditor rules at any funding level"
        return AdditionalFunds(
            lump_sum=FundsOption(0, False, blocked, date=None),
            monthly_increment=FundsOption(0, False, blocked, num_drafts=None),
        )
    return AdditionalFunds(
        lump_sum=minimum_lump_sum(client, offer, rules),
        monthly_increment=minimum_monthly_increment(client, offer, rules),
    )
