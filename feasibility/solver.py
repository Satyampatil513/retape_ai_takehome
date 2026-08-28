"""Candidate enumeration and the fee-earliness objective."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Iterator, Sequence

from feasibility.models import (
    Client,
    CreditorRules,
    Offer,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.results import ScheduleRow
from feasibility.shapes import (
    balloon_vector,
    cadence_dates,
    even_vector,
    segment_count,
    staircase_vectors,
)
from feasibility.simulate import build_timeline, simulate


@dataclass(frozen=True)
class Candidate:
    payments: list[int]
    shape: str
    from_balloon: bool


@dataclass(frozen=True)
class Solution:
    payments: list[int]
    shape: str
    rows: list[ScheduleRow]
    cum_fee: tuple[int, ...]


@dataclass(frozen=True)
class Outcome:
    solution: Solution | None
    # False means unschedulable on structure alone, so no amount of cash helps.
    structurally_possible: bool


def _label(v: list[int], from_balloon: bool) -> str:
    if from_balloon:
        return "balloon"
    if segment_count(v) == 1:
        return "even"
    return "staircase"


def candidates(
    cadence: Sequence[date], offer: Offer, rules: CreditorRules
) -> Iterator[Candidate]:
    """Every structurally legal payment vector, across all payment counts."""
    total = offer_total_cents(offer)
    k_max = min(rules.max_payments, rules.max_terms, len(cadence))
    for k in range(1, k_max + 1):
        if rules.even_pays:
            v = even_vector(k, total, rules)
            if v is not None:
                yield Candidate(v, "even", False)
            continue
        if rules.is_ballooning_allowed:
            v = balloon_vector(k, total, rules)
            if v is not None:
                yield Candidate(v, "balloon", True)
                # A balloon sits on every floor at once; no staircase here can undercut it.
                continue
        for v in staircase_vectors(k, total, rules):
            yield Candidate(v, _label(v, False), False)


def solve(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credits: Iterable[tuple[date, int]] = (),
) -> Outcome:
    """Pick the feasible schedule that collects our fee earliest.

    Ranking, in order:
      1. lexicographic max of the cumulative-fee-by-cadence-date vector;
      2. prefer an actual balloon when the creditor allows one -- this is what
         breaks the tie when the program fee is zero and every candidate scores
         identically on (1);
      3. fewer creditor payments (fewer bank fees);
      4. smaller payments earliest, purely so the result is deterministic.
    """
    cadence = cadence_dates(client, offer)
    if not cadence:
        return Outcome(None, False)

    fee_total = program_fee_cents(offer, rules)
    timeline = build_timeline(client, cadence, extra_credits)

    structurally_possible = False
    best: Solution | None = None
    best_key = None
    for cand in candidates(cadence, offer, rules):
        structurally_possible = True
        sim = simulate(client, cadence, cand.payments, rules, fee_total, timeline=timeline)
        if not sim.ok:
            continue
        key = (
            sim.cum_fee,
            1 if (cand.from_balloon and rules.is_ballooning_allowed) else 0,
            -len(cand.payments),
            tuple(-p for p in cand.payments),
        )
        if best_key is None or key > best_key:
            best_key = key
            best = Solution(cand.payments, cand.shape, sim.rows, sim.cum_fee)

    return Outcome(best, structurally_possible)


def feasibility_oracle(client: Client, offer: Offer, rules: CreditorRules):
    """Build a reusable ``probe(extra_credits) -> bool``.

    The candidate vectors come from the creditor rules alone, so they do not
    change as the funding search varies the money. Enumerating them once and
    reusing them across every probe is the difference between one enumeration
    and one per bisection step (roughly 32 of them across the two searches).

    The probe short-circuits on the first affordable candidate: the funding
    search only needs a yes/no, not the best schedule.
    """
    cadence = cadence_dates(client, offer)
    fee_total = program_fee_cents(offer, rules)
    vectors = [c.payments for c in candidates(cadence, offer, rules)] if cadence else []

    def probe(extra_credits: Iterable[tuple[date, int]] = ()) -> bool:
        if not vectors:
            return False
        timeline = build_timeline(client, cadence, extra_credits)
        for payments in vectors:
            if simulate(
                client, cadence, payments, rules, fee_total, timeline=timeline
            ).ok:
                return True
        return False

    return probe


def is_feasible(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credits: Iterable[tuple[date, int]] = (),
) -> bool:
    """Does *any* valid schedule fit? One-shot form of feasibility_oracle."""
    return feasibility_oracle(client, offer, rules)(extra_credits)
