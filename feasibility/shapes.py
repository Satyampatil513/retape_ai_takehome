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

    Rather than hand-place the steps, we consider every way to cut ``k``
    positions into ``s`` contiguous runs and let the objective pick. Each run
    sits at the lowest values its floors allow; whatever is left over lands on
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

    Enumerated as a memoised DFS rather than ``combinations``. Choosing cut
    points is choosing a subset of the k-1 gaps, so a plain enumeration is
    O(2^k) once ``max_segments >= k``. Two things tame it, and neither changes
    the set of vectors produced:

      * **Pruning.** Two exact necessary conditions kill a branch before its
        vector is built -- the positions still to be covered cannot cost less
        than their own floors, and the final run's spread cannot start below the
        run before it.
      * **Memoisation.** Different cut prefixes often produce identical head
        values, and from there the remaining work is identical, so each
        (position, head) pair is expanded once.
    """
    if k <= 0:
        return

    floors = [floor_at(i + 1, rules) for i in range(k)]
    # floors_from[i] = the least the positions from i onward can possibly cost.
    floors_from = [0] * (k + 1)
    for i in range(k - 1, -1, -1):
        floors_from[i] = floors_from[i + 1] + floors[i]

    max_runs = min(max(rules.max_segments, 1), k)
    emitted: set[tuple[int, ...]] = set()
    # (position, head) -> the fewest runs we have reached that state with. Reaching
    # the same head with fewer runs spent is strictly better, since it leaves more
    # cuts available downstream, so only a cheaper arrival is worth re-expanding.
    expanded: dict[tuple[int, tuple[int, ...]], int] = {}

    def walk(start: int, head: list[int], head_sum: int, prev: int, runs_used: int):
        state = (start, tuple(head))
        seen_runs = expanded.get(state)
        if seen_runs is not None and seen_runs <= runs_used:
            return
        expanded[state] = runs_used

        # Close here: positions [start, k) become the absorbing final run.
        length = k - start
        rest = total - head_sum
        # Necessary: the run must cover its own floors, and its spread starts at
        # rest // length, which may not dip below the previous run.
        if rest >= floors_from[start] and rest >= prev * length:
            v = head + _spread(rest, length)
            if not (
                REQUIRE_NON_BALLOON_TAIL
                and not rules.is_ballooning_allowed
                and k >= 2
                and last_segment_len(v) < 2
            ):
                key = tuple(v)
                if key not in emitted and validate(v, total, rules, check_segments=True):
                    emitted.add(key)
                    yield v

        # Or extend: [start, cut) becomes one more leading run.
        if runs_used + 1 >= max_runs:
            return
        for cut in range(start + 1, k):
            top = max(floors[start:cut])
            values = [max(f, top - 1, prev) for f in floors[start:cut]]
            run_sum = sum(values)
            # Nothing after this run can cost less than its own floors.
            if head_sum + run_sum + floors_from[cut] > total:
                break  # runs only get longer and dearer as `cut` grows
            yield from walk(cut, head + values, head_sum + run_sum, values[-1], runs_used + 1)

    yield from walk(0, [], 0, 0, 0)
