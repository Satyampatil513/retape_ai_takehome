"""Part 1 coverage: shapes, floors, segments, dates, simulation, fee placement."""

from __future__ import annotations

from datetime import date

import pytest

from feasibility.engine import evaluate_offer
from feasibility.models import LedgerEntry, load_case, offer_total_cents
from feasibility.money import half_up, pct_of_cents
from feasibility.shapes import (
    balloon_vector,
    cadence_dates,
    even_vector,
    floor_at,
    segment_count,
    staircase_vectors,
)
from feasibility.solver import candidates, solve
from tests.helpers import assert_valid_schedule, make_client, make_offer, make_rules


# --------------------------------------------------------------------------
# The four provided cases, checked against the independent validator
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case,shape",
    [
        ("case1_feasible_even", "even"),
        ("case3_balloon", "balloon"),
        ("case4_tiers", "staircase"),
    ],
)
def test_provided_cases_are_fully_valid(case, shape):
    client, offer, rules = load_case(f"cases/{case}")
    result = evaluate_offer(client, offer, rules)
    assert result.pay_shape_used == shape
    assert_valid_schedule(result, client, offer, rules)


def test_case1_balance_lands_exactly_on_zero():
    client, offer, rules = load_case("cases/case1_feasible_even")
    result = evaluate_offer(client, offer, rules)
    assert result.schedule[0].balance_cents == 0
    assert min(r.balance_cents for r in result.schedule) == 0


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

def test_half_up_differs_from_bankers_rounding():
    # round() would give 2 for 2.5; half-up must go away from zero.
    assert half_up("2.5") == 3
    assert half_up("-2.5") == -3
    assert half_up("3.5") == 4
    assert round(2.5) == 2  # documents what we are deliberately not doing


def test_pct_of_cents_avoids_binary_float_drift():
    assert pct_of_cents(0.4, 150000) == 60000
    assert pct_of_cents(0.07, 50) == 4  # 3.5 -> 4, not 3


# --------------------------------------------------------------------------
# Cadence and the horizon
# --------------------------------------------------------------------------

def test_cadence_is_true_eom_and_stops_at_horizon():
    client = make_client(last_draft=date(2026, 7, 1))
    offer = make_offer(first_payment_date=date(2026, 1, 31))
    dates = cadence_dates(client, offer)
    assert dates == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31),
        date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30),
    ]
    assert date(2026, 7, 31) not in dates  # past the horizon


def test_cadence_preserves_mid_month_day_with_clamp():
    client = make_client(first_draft=date(2026, 1, 30), last_draft=date(2026, 4, 30))
    offer = make_offer(first_payment_date=date(2026, 1, 30))
    dates = cadence_dates(client, offer)
    assert dates == [date(2026, 1, 30), date(2026, 2, 28), date(2026, 3, 30), date(2026, 4, 30)]


def test_cadence_defaults_to_eom_of_first_draft_month():
    client = make_client(first_draft=date(2026, 1, 15), last_draft=date(2026, 3, 15))
    offer = make_offer(first_payment_date=None)
    assert cadence_dates(client, offer)[0] == date(2026, 1, 31)


def test_cadence_empty_when_first_payment_is_past_the_horizon():
    client = make_client(last_draft=date(2026, 2, 1))
    offer = make_offer(first_payment_date=date(2026, 3, 31))
    assert cadence_dates(client, offer) == []


def test_draft_landing_after_the_last_cadence_date_is_dead_money():
    """case2's core: the final draft arrives with no cadence date left to use it."""
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    assert cadence_dates(client, offer)[-1] == date(2026, 4, 30)
    assert client.last_draft_date == date(2026, 5, 1)  # arrives too late to spend
    assert evaluate_offer(client, offer, rules).feasible is False


# --------------------------------------------------------------------------
# Floors: token pays and tiers
# --------------------------------------------------------------------------

def test_token_cap_forces_a_strict_increase_past_the_cap():
    rules = make_rules(min_payment_cents=2500, max_token_pays=3)
    assert [floor_at(i, rules) for i in range(1, 6)] == [2500, 2500, 2500, 2501, 2501]


def test_tier_raises_the_floor_from_its_payment_number():
    rules = make_rules(min_payment_cents=2500, max_token_pays=12, min_payment_tiers=[(3, 5000)])
    assert [floor_at(i, rules) for i in range(1, 5)] == [2500, 2500, 5000, 5000]


def test_token_cap_and_tier_combine_by_maximum():
    rules = make_rules(min_payment_cents=2500, max_token_pays=2, min_payment_tiers=[(4, 9000)])
    assert [floor_at(i, rules) for i in range(1, 6)] == [2500, 2500, 2501, 9000, 9000]


def test_token_cap_rejects_an_all_minimum_even_schedule():
    rules = make_rules(min_payment_cents=2500, max_token_pays=3)
    assert even_vector(4, 10000, rules) is None  # four payments of exactly the base min
    assert even_vector(3, 7500, rules) == [2500, 2500, 2500]


def test_case4_respects_the_tier_floor():
    client, offer, rules = load_case("cases/case4_tiers")
    result = evaluate_offer(client, offer, rules)
    payments = [r.creditor_payment_cents for r in result.schedule if r.creditor_payment_cents > 0]
    assert all(p >= 5000 for p in payments[6:])
    assert payments[:6] == [2500] * 6  # token pays kept minimal early, as the objective wants


# --------------------------------------------------------------------------
# Shapes and the segment cap
# --------------------------------------------------------------------------

def test_single_segment_forces_a_flat_staircase():
    rules = make_rules(max_segments=1, min_payment_cents=2500, max_token_pays=6)
    vectors = list(staircase_vectors(4, 40000, rules))
    assert vectors == [[10000] * 4]


def test_segment_cap_rejects_a_three_level_vector():
    rules = make_rules(max_segments=2, min_payment_cents=2500, max_token_pays=6,
                       min_payment_tiers=[(3, 5000)])
    vectors = list(staircase_vectors(4, 30000, rules))
    assert all(segment_count(v) <= 2 for v in vectors)
    # the greedy min-prefix shape here would be 2500/2500/5000/20000 -- three levels
    assert [2500, 2500, 5000, 20000] not in vectors


def test_remainder_cents_inside_a_run_do_not_open_a_new_segment():
    assert segment_count([8333, 8333, 8333, 8334, 8334]) == 1
    assert segment_count([2500, 2500, 7500, 7501]) == 2


def test_every_candidate_sums_exactly_to_the_offer_total():
    client, offer, rules = load_case("cases/case4_tiers")
    total = offer_total_cents(offer)
    cands = list(candidates(cadence_dates(client, offer), offer, rules))
    assert cands
    assert all(sum(c.payments) == total for c in cands)


def test_even_split_puts_remainder_cents_on_the_latest_payments():
    rules = make_rules(min_payment_cents=1, max_token_pays=0)
    assert even_vector(6, 50000, rules) == [8333, 8333, 8333, 8333, 8334, 8334]


def test_balloon_keeps_the_prefix_at_the_floor_and_absorbs_the_rest():
    rules = make_rules(min_payment_cents=2500, max_token_pays=2, is_ballooning_allowed=True)
    assert balloon_vector(4, 40000, rules) == [2500, 2500, 2501, 32499]


def test_balloon_rejected_when_the_remainder_undercuts_the_prefix():
    rules = make_rules(min_payment_cents=2500, max_token_pays=6, is_ballooning_allowed=True)
    assert balloon_vector(4, 8000, rules) is None  # last would be 500, below the floor


def test_balloon_wins_the_tie_when_the_program_fee_is_zero():
    """case3 has program_fee_pct=0, so every candidate ties on fee earliness."""
    client, offer, rules = load_case("cases/case3_balloon")
    result = evaluate_offer(client, offer, rules)
    payments = [r.creditor_payment_cents for r in result.schedule]
    assert result.pay_shape_used == "balloon"
    assert payments[-1] > payments[-2]
    assert payments[:-1] == [2500] * (len(payments) - 1)


def test_balloon_shape_is_not_offered_when_the_creditor_forbids_it():
    client = make_client(last_draft=date(2026, 6, 1))
    offer = make_offer(creditor_balance_cents=60000, settlement_pct=0.5)
    rules = make_rules(is_ballooning_allowed=False, max_segments=4)
    cadence = cadence_dates(client, offer)
    assert all(not c.from_balloon for c in candidates(cadence, offer, rules))


# --------------------------------------------------------------------------
# Simulation: ordering, fixed entries, exact zero
# --------------------------------------------------------------------------

def test_credits_are_applied_before_debits_on_the_same_date():
    """The 2026-01-01 draft and a fixed 2026-01-01 debit land together.

    Credits first: 0 + 10000 - 9000 = 1000, and the settlement fits.
    Debits first: 0 - 9000 = -9000, and the whole offer would read infeasible.
    """
    client = make_client(
        draft_amount_cents=10000,
        last_draft=date(2026, 6, 1),
        current_balance_cents=0,
        extra_entries=[LedgerEntry(date(2026, 1, 1), 9000, "debit")],
    )
    offer = make_offer(creditor_balance_cents=2000, settlement_pct=0.5)  # total 1000
    rules = make_rules(max_terms=1, max_payments=1, min_payment_cents=500)
    result = evaluate_offer(client, offer, rules)
    assert result.feasible is True
    assert result.schedule[0].creditor_payment_cents == 1000
    assert result.schedule[0].balance_cents == 0  # the 1000 that survived Jan 1
    assert_valid_schedule(result, client, offer, rules)


def test_fixed_ledger_debits_are_respected_not_rescheduled():
    client, offer, rules = load_case("cases/case3_balloon")
    result = evaluate_offer(client, offer, rules)
    # the fixed -15000 on 2026-02-01 squeezes the Feb 28 payment down to the floor
    assert result.schedule[1].date == date(2026, 2, 28)
    assert result.schedule[1].balance_cents == 0
    assert_valid_schedule(result, client, offer, rules)


# --------------------------------------------------------------------------
# Program-fee placement: the crux
# --------------------------------------------------------------------------

def test_fee_is_taken_maximally_on_the_first_payment_date():
    client, offer, rules = load_case("cases/case1_feasible_even")
    result = evaluate_offer(client, offer, rules)
    first = result.schedule[0]
    # everything the first draft leaves after the payment and the bank fee
    assert first.program_fee_cents == 20000 - first.creditor_payment_cents - first.bank_fee_cents
    assert first.balance_cents == 0


def test_fee_is_capped_by_a_later_fixed_debit_not_by_todays_balance():
    """A forward greedy would grab 7500 here and starve the Feb 28 payment."""
    client = make_client(
        draft_amount_cents=10000,
        last_draft=date(2026, 4, 1),
        extra_entries=[LedgerEntry(date(2026, 2, 1), 9000, "debit")],
    )
    offer = make_offer(
        creditor_balance_cents=15000, settlement_pct=0.5,   # offer total 7500
        original_balance_cents=60000,
    )
    rules = make_rules(max_terms=3, max_payments=3, min_payment_cents=2500,
                       max_segments=1, program_fee_pct=0.1)  # fee 6000
    result = evaluate_offer(client, offer, rules)
    assert result.feasible is True
    assert result.schedule[0].program_fee_cents == 6000  # not 7500
    assert_valid_schedule(result, client, offer, rules)


def test_fee_defers_past_a_squeezed_month_rather_than_failing():
    client = make_client(
        draft_amount_cents=10000,
        last_draft=date(2026, 4, 1),
        extra_entries=[LedgerEntry(date(2026, 2, 1), 9000, "debit")],
    )
    offer = make_offer(
        creditor_balance_cents=15000, settlement_pct=0.5,
        original_balance_cents=60000,
    )
    rules = make_rules(max_terms=3, max_payments=3, min_payment_cents=2500,
                       max_segments=1, program_fee_pct=0.125)  # fee 7500
    result = evaluate_offer(client, offer, rules)
    fees = [r.program_fee_cents for r in result.schedule]
    assert fees == [6000, 0, 1500]  # front-load, skip the squeezed month, finish later
    assert_valid_schedule(result, client, offer, rules)


def test_fee_only_date_carries_no_bank_fee():
    client = make_client(draft_amount_cents=10000, last_draft=date(2026, 6, 1))
    offer = make_offer(
        creditor_balance_cents=10000, settlement_pct=0.5,   # offer total 5000
        original_balance_cents=100000,
    )
    rules = make_rules(max_terms=1, max_payments=1, min_payment_cents=2500,
                       bank_fee_cents=500, program_fee_pct=0.3)  # fee 30000
    result = evaluate_offer(client, offer, rules)
    assert result.feasible is True
    fee_only = [r for r in result.schedule if r.creditor_payment_cents == 0]
    assert fee_only, "expected the fee to spill onto fee-only dates"
    assert all(r.bank_fee_cents == 0 and r.program_fee_cents > 0 for r in fee_only)
    assert_valid_schedule(result, client, offer, rules)


def test_infeasible_when_the_fee_cannot_finish_by_the_horizon():
    client = make_client(draft_amount_cents=10000, last_draft=date(2026, 3, 1))
    offer = make_offer(
        creditor_balance_cents=10000, settlement_pct=0.5,
        original_balance_cents=1000000,   # fee 100000, far beyond the drafts
    )
    rules = make_rules(max_terms=1, max_payments=1, min_payment_cents=2500,
                       program_fee_pct=0.1)
    result = evaluate_offer(client, offer, rules)
    assert result.feasible is False
    assert result.schedule is None
    assert result.pay_shape_used is None


# --------------------------------------------------------------------------
# Structural infeasibility (no amount of cash helps)
# --------------------------------------------------------------------------

def test_offer_below_the_base_minimum_is_structurally_impossible():
    client = make_client()
    offer = make_offer(creditor_balance_cents=2000, settlement_pct=0.5)  # total 1000
    rules = make_rules(min_payment_cents=2500)
    outcome = solve(client, offer, rules)
    assert outcome.solution is None
    assert outcome.structurally_possible is False


def test_no_cadence_date_before_the_horizon_is_structurally_impossible():
    client = make_client(last_draft=date(2026, 2, 1))
    offer = make_offer(first_payment_date=date(2026, 3, 31))
    outcome = solve(client, offer, make_rules())
    assert outcome.solution is None
    assert outcome.structurally_possible is False


def test_a_cash_short_but_well_formed_offer_is_structurally_possible():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    outcome = solve(client, offer, rules)
    assert outcome.solution is None
    assert outcome.structurally_possible is True


# --------------------------------------------------------------------------
# Regressions: vectors the enumeration used to miss
# --------------------------------------------------------------------------

def test_last_run_may_sit_below_its_own_maximum_floor():
    """Floors [2, 3, 5] with total 13 admits [4, 4, 5] as a single level.

    The spread puts its +1 cents on the run's latest positions, which is exactly
    where a tier step-up sits, so the run's base (4) may sit below the run's
    maximum floor (5) while every position still clears its own floor.
    """
    rules = make_rules(max_terms=3, max_payments=3, min_payment_cents=2,
                       max_token_pays=1, min_payment_tiers=[(3, 5)], max_segments=1)
    assert [floor_at(i, rules) for i in (1, 2, 3)] == [2, 3, 5]
    assert [4, 4, 5] in list(staircase_vectors(3, 13, rules))


def test_leading_run_uses_positional_floors_not_a_flat_maximum():
    """Floors [3, 4] make the leading run [3, 4], not [4, 4].

    A run is one level, but the +/-1 waiver lets it hold two adjacent values, so
    only the position carrying the run's highest floor has to reach it.
    """
    rules = make_rules(max_terms=3, max_payments=3, min_payment_cents=3,
                       max_token_pays=1, min_payment_tiers=[(3, 5)], max_segments=2)
    assert [floor_at(i, rules) for i in (1, 2, 3)] == [3, 4, 5]
    vectors = list(staircase_vectors(3, 15, rules))
    assert [3, 4, 8] in vectors
    # [4, 4, 7] is the same cut set filled from a flat max. It is dominated --
    # equal total, higher prefix sums -- so it is deliberately not generated.
    assert [4, 4, 7] not in vectors


def test_solver_matches_exhaustive_search_on_small_cases():
    """The candidate enumeration must not miss a better schedule.

    On deliberately tiny inputs, enumerate EVERY vector S5 admits -- all
    non-decreasing vectors meeting the sum, the floors, the token cap and the
    segment cap -- score each with the same simulator, and check the solver's
    answer ties the best. This caught two real gaps in the enumeration: a last
    run pre-rejected against its maximum floor, and a leading run forced to a
    flat maximum instead of positional floors.
    """
    import random

    from feasibility.models import (
        Client, CreditorRules, LedgerEntry, Offer, add_months,
        offer_total_cents, program_fee_cents,
    )
    from feasibility.simulate import simulate

    def every_valid_vector(k, total, rules, cap_segments):
        out = []

        def rec(i, prev, left, acc):
            if i == k:
                if left:
                    return
                tokens = sum(1 for x in acc if x == rules.min_payment_cents)
                if tokens > rules.max_token_pays:
                    return
                if cap_segments and segment_count(acc) > rules.max_segments:
                    return
                out.append(list(acc))
                return
            for v in range(max(prev, floor_at(i + 1, rules)), left + 1):
                if v * (k - i) > left:
                    break
                rec(i + 1, v, left - v, acc + [v])

        rec(0, 0, total, [])
        return out

    rng = random.Random(7)
    compared = 0
    for _ in range(700):
        months = rng.randint(2, 5)
        first = date(2026, 1, 1)
        draft = rng.randint(4, 14)
        ledger = [LedgerEntry(add_months(first, i), draft, "credit") for i in range(months)]
        if rng.random() < 0.4:
            ledger.append(
                LedgerEntry(add_months(first, rng.randrange(months)), rng.randint(1, 10), "debit")
            )
        client = Client(draft, 1, first, add_months(first, months - 1),
                        date(2025, 12, 31), 0, ledger)
        offer = Offer("Rng", rng.randint(10, 60), rng.randint(10, 60), rng.choice([0.5, 1.0]),
                      rng.choice([None, date(2026, 1, 31), date(2026, 1, 1)]))
        kcap = rng.randint(1, 5)
        ballooning = rng.random() < 0.3
        rules = CreditorRules(
            kcap, kcap, rng.randint(1, 3), rng.randint(0, 4),
            rng.choice([[], [(2, 4)], [(3, 5)], [(2, 4), (4, 6)]]),
            False, ballooning, rng.randint(1, 3),
            rng.choice([0, 1]), rng.choice([0.0, 0.2, 0.5]),
        )

        cadence = cadence_dates(client, offer)
        if not cadence:
            continue
        total = offer_total_cents(offer)
        fee = program_fee_cents(offer, rules)

        best = None
        for k in range(1, min(rules.max_payments, rules.max_terms, len(cadence)) + 1):
            # S4: the segment cap is ignored while ballooning
            for v in every_valid_vector(k, total, rules, cap_segments=not ballooning):
                sim = simulate(client, cadence, v, rules, fee)
                if sim.ok and (best is None or sim.cum_fee > best):
                    best = sim.cum_fee

        solution = solve(client, offer, rules).solution
        ours = solution.cum_fee if solution else None
        assert (best is None) == (ours is None), "disagreed on whether any schedule fits"
        if best is not None:
            assert ours == best, f"exhaustive search found {best}, solver returned {ours}"
            compared += 1

    assert compared >= 100, f"only {compared} feasible cases generated; widen the fuzz"


def test_a_valid_balloon_makes_staircases_at_that_k_redundant():
    """One candidate per k while ballooning, except where no balloon exists.

    A balloon puts every position on its own floor. A staircase run is one
    level, so a run spanning a floor jump drags its earlier positions up to
    top - 1. The balloon's prefix sums are therefore the pointwise minimum over
    every valid vector at that k, which means weakly higher balances and weakly
    higher fee capacity -- no staircase at that k can win.
    """
    client, offer, rules = load_case("cases/case3_balloon")
    cadence = cadence_dates(client, offer)
    by_k = {}
    for cand in candidates(cadence, offer, rules):
        by_k.setdefault(len(cand.payments), []).append(cand)

    for k, cands in by_k.items():
        if k == 1:
            # a balloon needs k >= 2, so k=1 still comes from the staircase path
            assert [c.shape for c in cands] == ["even"]
        else:
            assert [c.shape for c in cands] == ["balloon"], f"k={k} still offers staircases"


def test_a_balloon_exists_whenever_any_vector_does_for_k_at_least_2():
    """So skipping staircases under a valid balloon only ever leaves k=1 to them.

    The balloon is exactly the pointwise floors plus the remainder, so it needs
    `total >= sum(floors)` -- which is the least ANY valid vector at that k can
    cost. It therefore exists whenever anything does, for k >= 2.
    """
    import random

    from feasibility.models import CreditorRules
    from tests.helpers import random_case

    rng = random.Random(3)
    checked = 0
    for _ in range(400):
        _, offer, base = random_case(rng)
        rules = CreditorRules(
            base.max_terms, base.max_payments, base.min_payment_cents,
            base.max_token_pays, base.min_payment_tiers, False, True,
            base.max_segments, base.bank_fee_cents, base.program_fee_pct,
        )
        total = offer_total_cents(offer)
        for k in range(2, min(rules.max_payments, rules.max_terms) + 1):
            stairs = list(staircase_vectors(k, total, rules))
            if stairs:
                assert balloon_vector(k, total, rules) is not None, (
                    f"k={k} has staircases {stairs[:2]} but no balloon"
                )
                checked += 1
    assert checked >= 50, f"only {checked} comparisons made; widen the fuzz"
