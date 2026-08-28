"""Cadence, floors, and creditor-payment vector construction.

A "payment vector" is the list of creditor payments, one per consecutive
cadence date starting at ``first_payment_date``. Everything here is pure
arithmetic over the creditor rules -- no cash, no ledger. Whether a vector is
*affordable* is ``simulate``'s job.
"""

from __future__ import annotations

from datetime import date
from itertools import combinations
from typing import Iterator

from feasibility.models import (
    Client,
    CreditorRules,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
)

# When ballooning is disallowed, should we also forbid a staircase whose final
# segment is a single payment? Our reading of S5.9 says no -- `max_segments` is
# the only structural cap on a staircase, and with tiers present it is usually
# binding on its own (see README). Flip this to True to make
# `is_ballooning_allowed=False` structurally exclude balloon-alikes.
REQUIRE_NON_BALLOON_TAIL = False


def cadence_dates(client: Client, offer: Offer) -> list[date]:
    """Every cadence date at or before the horizon.

    This is both the window in which creditor payments may sit AND the window
    in which program fee may be collected (S5.6b) -- fee-only dates after the
    last creditor payment are still usable.
    """
    start = offer.first_payment_date or default_first_payment_date(client)
    horizon = client.last_draft_date
    if start > horizon:
        return []
    months = (horizon.year - start.year) * 12 + (horizon.month - start.month) + 2
    return [d for d in monthly_payment_dates(start, months) if d <= horizon]


def floor_at(pos: int, rules: CreditorRules) -> int:
    """Minimum legal size of the creditor payment at 1-based position ``pos``.

    The maximum of three rules (S5.4): the base minimum; the token-pay cap --
    only ``max_token_pays`` payments may sit *at* the base minimum, and since
    payments are non-decreasing those are always a prefix, so anything past
    that position must clear the base by at least a cent; and any tier step-up.
    """
    f = rules.min_payment_cents
    if pos > rules.max_token_pays:
        f = rules.min_payment_cents + 1
    for from_pos, min_cents in rules.min_payment_tiers:
        if pos >= from_pos:
            f = max(f, min_cents)
    return f


def segment_count(v: list[int]) -> int:
    """Number of distinct payment levels used.

    Levels are contiguous runs. A +/-1-cent difference inside a run does not open
    a new level: that is just the remainder distribution S5.7 explicitly blesses
    for `even`, and we apply the same waiver to a staircase's runs.
    """
    if not v:
        return 0
    n = 1
    base = v[0]
    for x in v[1:]:
        if x - base > 1:
            n += 1
            base = x
    return n


def last_segment_len(v: list[int]) -> int:
    if not v:
        return 0
    n = 1
    for i in range(len(v) - 1, 0, -1):
        if v[i] - v[i - 1] > 1:
            break
        n += 1
    return n


def validate(v: list[int], total: int, rules: CreditorRules, *, check_segments: bool) -> bool:
    """Check a vector against the hard constraints S5.2-S5.4 and S5.9."""
    if not v or sum(v) != total:
        return False
    if any(v[i] < v[i - 1] for i in range(1, len(v))):
        return False
    if any(v[i] < floor_at(i + 1, rules) for i in range(len(v))):
        return False
    if sum(1 for x in v if x == rules.min_payment_cents) > rules.max_token_pays:
        return False
    if check_segments and segment_count(v) > rules.max_segments:
        return False
    return True


def _spread(amount: int, length: int) -> list[int]:
    """Split ``amount`` over ``length`` slots, remainder cents onto the latest."""
    base, rem = divmod(amount, length)
    return [base] * (length - rem) + [base + 1] * rem


def even_vector(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """All payments equal, remainder cents onto the latest (S5.7).

    `max_segments` is ignored when `even_pays` is set.
    """
    if k <= 0:
        return None
    v = _spread(total, k)
    return v if validate(v, total, rules, check_segments=False) else None


def balloon_vector(k: int, total: int, rules: CreditorRules) -> list[int] | None:
    """Floor payments throughout, with the final payment absorbing the rest.

    `max_segments` is ignored when ballooning (S4). Needs k >= 2 -- a single
    payment is not a balloon, it is the whole settlement.
    """
    if k < 2:
        return None
    head = [floor_at(i + 1, rules) for i in range(k - 1)]
    last = total - sum(head)
    if last < head[-1] or last < floor_at(k, rules):
        return None
    v = head + [last]
    return v if validate(v, total, rules, check_segments=False) else None


def staircase_vectors(k: int, total: int, rules: CreditorRules) -> Iterator[list[int]]:
    """Every legal staircase over ``k`` payments, up to ``max_segments`` levels.

    Rather than hand-place the steps, we enumerate every way to cut ``k``
    positions into ``s`` contiguous runs and let the objective pick. Each run
    sits at the lowest level its floors allow; whatever is left over lands on
    the final run, which keeps the early payments as small as the rules permit.

    ``s = 1`` reproduces the flat/even vector, so the flattest and the most
    back-loaded shapes are both in the candidate set.

    Generating only the cheapest fill per cut set loses nothing. Every valid
    vector sums to ``total``, so the balance on the final payment date is the
    same for all of them; only the earlier dates differ, and they are better the
    smaller the running total spent so far. The cheapest fill minimises every
    prefix sum for its cut set, so it dominates every other fill of that cut set
    on both feasibility and fee capacity. Verified against exhaustive
    enumeration in tests/test_engine.py.
    """
    if k <= 0:
        return
    seen: set[tuple[int, ...]] = set()
    for s in range(1, min(max(rules.max_segments, 1), k) + 1):
        for cuts in combinations(range(1, k), s - 1):
            bounds = (0,) + cuts + (k,)
            runs = [(bounds[j], bounds[j + 1]) for j in range(s)]

            # Each leading run takes the cheapest values it can. A run is one
            # level, but the +/-1 waiver lets it hold two adjacent values, so the
            # floor is per position rather than a flat max: with floors [3, 4]
            # the run is [3, 4], not [4, 4]. Only the position carrying the
            # run's highest floor has to reach it; the rest need only stay
            # within a cent of it, and nothing may dip below the previous run.
            head: list[int] = []
            prev = 0
            for a, b in runs[:-1]:
                floors = [floor_at(i + 1, rules) for i in range(a, b)]
                top = max(floors)
                values = [max(f, top - 1, prev) for f in floors]
                head.extend(values)
                prev = values[-1]

            a, b = runs[-1]
            rest = total - sum(head)
            if rest < 0:
                continue
            # Do NOT pre-reject on the run's maximum floor here. The spread puts
            # the +1 cents on the run's latest positions, which is exactly where
            # a tier step-up sits, so a run whose base is below its own maximum
            # floor can still clear every positional floor -- e.g. floors
            # [2, 3, 5] with total 13 admits [4, 4, 5]. validate() checks the
            # floors position by position, which is the real requirement.

            v = head + _spread(rest, b - a)

            if REQUIRE_NON_BALLOON_TAIL and not rules.is_ballooning_allowed:
                if k >= 2 and last_segment_len(v) < 2:
                    continue

            key = tuple(v)
            if key in seen:
                continue
            seen.add(key)
            if validate(v, total, rules, check_segments=True):
                yield v
