"""Part 2 coverage: the two minima, their minimality, and the guardrails."""

from __future__ import annotations

from datetime import date

from feasibility.engine import evaluate_offer
from feasibility.funding import future_draft_dates, minimum_lump_sum
from feasibility.models import LedgerEntry, load_case, offer_total_cents
from feasibility.solver import is_feasible
from tests.helpers import make_client, make_offer, make_rules


def _case2():
    return load_case("cases/case2_infeasible_minima")


# --------------------------------------------------------------------------
# The provided infeasible case
# --------------------------------------------------------------------------

def test_case2_lump_sum():
    client, offer, rules = _case2()
    lump = evaluate_offer(client, offer, rules).additional_funds.lump_sum
    assert lump.amount_cents == 10000
    assert lump.within_guardrail is True
    assert lump.reason == ""
    assert lump.date == date(2026, 1, 1)  # the earliest date we may still touch
    assert lump.date > client.as_of_date
    assert lump.date <= client.last_draft_date


def test_case2_monthly_increment():
    client, offer, rules = _case2()
    inc = evaluate_offer(client, offer, rules).additional_funds.monthly_increment
    assert inc.amount_cents == 2500
    assert inc.num_drafts == 5
    assert inc.within_guardrail is True
    assert inc.reason == ""


def test_num_drafts_counts_every_future_draft_even_the_useless_one():
    """The 2026-05-01 draft lands after the last cadence date and buys nothing,
    but S8 asks for the number of drafts *affected*, which is all five."""
    client, offer, rules = _case2()
    inc = evaluate_offer(client, offer, rules).additional_funds.monthly_increment
    assert len(future_draft_dates(client)) == 5
    assert inc.num_drafts == 5
    # 2500 x 4 useful drafts == the 10000 lump; the fifth is dead money
    assert inc.amount_cents * 4 == 10000


def test_the_two_minima_need_not_imply_the_same_total():
    client, offer, rules = _case2()
    af = evaluate_offer(client, offer, rules).additional_funds
    lump_total = af.lump_sum.amount_cents
    increment_total = af.monthly_increment.amount_cents * af.monthly_increment.num_drafts
    assert lump_total == 10000
    assert increment_total == 12500  # the extra 2500 arrives too late to be spent
    assert increment_total > lump_total


# --------------------------------------------------------------------------
# Minimality: the bisection really found the smallest amount
# --------------------------------------------------------------------------

def test_lump_sum_is_minimal_to_the_cent():
    client, offer, rules = _case2()
    lump = evaluate_offer(client, offer, rules).additional_funds.lump_sum
    when = lump.date
    assert is_feasible(client, offer, rules, [(when, lump.amount_cents)]) is True
    assert is_feasible(client, offer, rules, [(when, lump.amount_cents - 1)]) is False


def test_monthly_increment_is_minimal_to_the_cent():
    client, offer, rules = _case2()
    inc = evaluate_offer(client, offer, rules).additional_funds.monthly_increment
    dates = future_draft_dates(client)
    assert is_feasible(client, offer, rules, [(d, inc.amount_cents) for d in dates]) is True
    assert is_feasible(client, offer, rules, [(d, inc.amount_cents - 1) for d in dates]) is False


def test_feasibility_is_monotone_in_the_lump_sum():
    """The property bisection depends on: once an amount works, more works too."""
    client, offer, rules = _case2()
    when = date(2026, 1, 1)
    for amount in (0, 5000, 9999):
        assert is_feasible(client, offer, rules, [(when, amount)]) is False
    for amount in (10000, 10001, 50000):
        assert is_feasible(client, offer, rules, [(when, amount)]) is True


def test_an_earlier_lump_is_never_worse_than_a_later_one():
    client, offer, rules = _case2()
    assert is_feasible(client, offer, rules, [(date(2026, 1, 1), 10000)]) is True
    # the same 10000 arriving after the last usable cadence date does nothing
    assert is_feasible(client, offer, rules, [(date(2026, 5, 1), 10000)]) is False


# --------------------------------------------------------------------------
# Guardrails
# --------------------------------------------------------------------------

def _thin_case():
    """A small settlement with a large fee against thin drafts: both minima
    should overshoot their caps."""
    client = make_client(
        draft_amount_cents=1000, first_draft=date(2026, 1, 1), last_draft=date(2026, 3, 1)
    )
    offer = make_offer(
        creditor_balance_cents=10000, settlement_pct=0.5,  # offer total 5000
        original_balance_cents=100000,
    )
    rules = make_rules(
        max_terms=2, max_payments=2, min_payment_cents=2500, program_fee_pct=0.2  # fee 20000
    )
    return client, offer, rules


def test_lump_sum_guardrail_trips_above_65_pct_of_the_offer():
    client, offer, rules = _thin_case()
    lump = evaluate_offer(client, offer, rules).additional_funds.lump_sum
    cap = round(0.65 * offer_total_cents(offer))
    assert lump.amount_cents == 23000
    assert lump.amount_cents > cap == 3250
    assert lump.within_guardrail is False
    assert "65%" in lump.reason


def test_increment_guardrail_trips_above_the_max_of_100_dollars_and_40_pct():
    client, offer, rules = _thin_case()
    inc = evaluate_offer(client, offer, rules).additional_funds.monthly_increment
    # max(10000, 40% of a 1000 draft) == 10000, so the floor is what binds
    assert inc.amount_cents == 11500
    assert inc.within_guardrail is False
    assert "10000" in inc.reason


def test_increment_guardrail_uses_40_pct_when_the_draft_is_large():
    client = make_client(
        draft_amount_cents=100000, first_draft=date(2026, 1, 1), last_draft=date(2026, 3, 1)
    )
    offer = make_offer(
        creditor_balance_cents=800000, settlement_pct=0.5,  # offer total 400000
        original_balance_cents=0,
    )
    rules = make_rules(max_terms=2, max_payments=2, min_payment_cents=2500)
    inc = evaluate_offer(client, offer, rules).additional_funds.monthly_increment
    # 40% of a 100000 draft is 40000, well above the 10000 floor
    assert inc.amount_cents > 40000
    assert inc.within_guardrail is False
    assert "40%" in inc.reason


def test_guardrails_pass_quietly_with_an_empty_reason():
    client, offer, rules = _case2()
    af = evaluate_offer(client, offer, rules).additional_funds
    assert af.lump_sum.within_guardrail is True
    assert af.monthly_increment.within_guardrail is True
    assert af.lump_sum.reason == af.monthly_increment.reason == ""


# --------------------------------------------------------------------------
# Cases where funding cannot help
# --------------------------------------------------------------------------

def test_structurally_impossible_offer_is_unfundable():
    client = make_client()
    offer = make_offer(creditor_balance_cents=2000, settlement_pct=0.5)  # total 1000
    rules = make_rules(min_payment_cents=2500)  # below the base minimum at every k
    result = evaluate_offer(client, offer, rules)
    assert result.feasible is False
    af = result.additional_funds
    for option in (af.lump_sum, af.monthly_increment):
        assert option.amount_cents == 0
        assert option.within_guardrail is False
        assert "any funding level" in option.reason


def test_no_future_drafts_means_no_increment_is_possible():
    client = make_client(
        drafts=False,
        last_draft=date(2026, 6, 1),
        extra_entries=[LedgerEntry(date(2026, 2, 1), 100, "debit")],
    )
    offer = make_offer(creditor_balance_cents=10000, settlement_pct=0.5, original_balance_cents=0)
    rules = make_rules(min_payment_cents=2500)
    af = evaluate_offer(client, offer, rules).additional_funds
    assert af.monthly_increment.num_drafts == 0
    assert af.monthly_increment.amount_cents == 0
    assert af.monthly_increment.within_guardrail is False
    assert "no future drafts" in af.monthly_increment.reason
    # a lump still works: it covers the settlement plus the fixed debit
    assert af.lump_sum.amount_cents == 5100


def test_lump_sum_has_no_placement_when_the_horizon_is_already_past():
    client = make_client(last_draft=date(2025, 12, 31), as_of=date(2025, 12, 31))
    offer = make_offer(first_payment_date=date(2025, 12, 31))
    lump = minimum_lump_sum(client, offer, make_rules())
    assert lump.date is None
    assert lump.within_guardrail is False


# --------------------------------------------------------------------------
# Feasible offers carry no funding block
# --------------------------------------------------------------------------

def test_feasible_offers_report_no_additional_funds():
    for case in ("case1_feasible_even", "case3_balloon", "case4_tiers"):
        client, offer, rules = load_case(f"cases/{case}")
        result = evaluate_offer(client, offer, rules)
        assert result.feasible is True
        assert result.additional_funds is None
        assert result.to_dict()["additional_funds"] is None
