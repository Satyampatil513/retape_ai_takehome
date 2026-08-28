"""Test helpers: case builders and an INDEPENDENT schedule validator.

The validator deliberately re-derives everything from the raw inputs and
re-simulates the ledger by hand, rather than reusing feasibility.simulate, so a
bug in the engine's simulator cannot hide behind itself.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)


def make_client(
    *,
    draft_amount_cents: int = 10000,
    draft_day: int = 1,
    first_draft: date = date(2026, 1, 1),
    last_draft: date = date(2026, 6, 1),
    as_of: date = date(2025, 12, 31),
    current_balance_cents: int = 0,
    extra_entries: list[LedgerEntry] | None = None,
    drafts: bool = True,
) -> Client:
    ledger: list[LedgerEntry] = []
    if drafts:
        d = first_draft
        while d <= last_draft:
            ledger.append(LedgerEntry(d, draft_amount_cents, "credit"))
            nxt = monthly_payment_dates(d, 2)[1]
            d = nxt
    ledger += extra_entries or []
    return Client(
        draft_amount_cents=draft_amount_cents,
        draft_day=draft_day,
        first_draft_date=first_draft,
        last_draft_date=last_draft,
        as_of_date=as_of,
        current_balance_cents=current_balance_cents,
        ledger=ledger,
    )


def make_offer(
    *,
    creditor_balance_cents: int = 60000,
    original_balance_cents: int = 60000,
    settlement_pct: float = 0.5,
    first_payment_date: date | None = date(2026, 1, 31),
) -> Offer:
    return Offer(
        creditor="TestCo",
        creditor_balance_cents=creditor_balance_cents,
        original_balance_cents=original_balance_cents,
        settlement_pct=settlement_pct,
        first_payment_date=first_payment_date,
    )


def make_rules(
    *,
    max_terms: int = 6,
    max_payments: int = 6,
    min_payment_cents: int = 2500,
    max_token_pays: int = 6,
    min_payment_tiers: list[tuple[int, int]] | None = None,
    even_pays: bool = False,
    is_ballooning_allowed: bool = False,
    max_segments: int = 4,
    bank_fee_cents: int = 0,
    program_fee_pct: float = 0.0,
) -> CreditorRules:
    return CreditorRules(
        max_terms=max_terms,
        max_payments=max_payments,
        min_payment_cents=min_payment_cents,
        max_token_pays=max_token_pays,
        min_payment_tiers=min_payment_tiers or [],
        even_pays=even_pays,
        is_ballooning_allowed=is_ballooning_allowed,
        max_segments=max_segments,
        bank_fee_cents=bank_fee_cents,
        program_fee_pct=program_fee_pct,
    )


def expected_floor(pos: int, rules: CreditorRules) -> int:
    f = rules.min_payment_cents
    if pos > rules.max_token_pays:
        f = rules.min_payment_cents + 1
    for from_pos, min_cents in rules.min_payment_tiers:
        if pos >= from_pos:
            f = max(f, min_cents)
    return f


def assert_valid_schedule(result, client: Client, offer: Offer, rules: CreditorRules) -> None:
    """Re-check every hard constraint in ASSIGNMENT.md S5 from scratch."""
    assert result.feasible is True
    rows = result.schedule
    assert rows, "a feasible result must carry a schedule"

    horizon = client.last_draft_date
    start = offer.first_payment_date or default_first_payment_date(client)
    cadence = [d for d in monthly_payment_dates(start, 400) if d <= horizon]

    # -- dates: strictly increasing, on cadence, within the horizon (S5.1)
    dates = [r.date for r in rows]
    assert dates == sorted(set(dates)), "row dates must be strictly increasing"
    assert all(d in cadence for d in dates)
    assert all(d <= horizon for d in dates)

    # -- payments occupy a consecutive prefix of the cadence (S5.1)
    pay_dates = [r.date for r in rows if r.creditor_payment_cents > 0]
    payments = [r.creditor_payment_cents for r in rows if r.creditor_payment_cents > 0]
    assert pay_dates == cadence[: len(pay_dates)], "payments must be consecutive from the first cadence date"
    k = len(payments)
    assert 1 <= k <= min(rules.max_payments, rules.max_terms, len(cadence))

    # -- exact sum (S5.2), non-decreasing (S5.3), floors (S5.4)
    assert sum(payments) == offer_total_cents(offer)
    assert all(payments[i] >= payments[i - 1] for i in range(1, k))
    for i, p in enumerate(payments, start=1):
        assert p >= expected_floor(i, rules), f"payment {i} below its floor"
    assert sum(1 for p in payments if p == rules.min_payment_cents) <= rules.max_token_pays

    # -- segment cap (S5.9); waived for even and for a balloon (S4)
    if result.pay_shape_used == "staircase":
        levels, base = 1, payments[0]
        for p in payments[1:]:
            if p - base > 1:
                levels += 1
                base = p
        assert levels <= rules.max_segments
    if result.pay_shape_used == "balloon":
        assert rules.is_ballooning_allowed, "balloon reported for a creditor that forbids it"

    # -- bank fee exactly on payment-carrying dates (S5.5)
    for r in rows:
        expected = rules.bank_fee_cents if r.creditor_payment_cents > 0 else 0
        assert r.bank_fee_cents == expected

    # -- program fee: fully collected, never before the first payment (S5.6)
    assert sum(r.program_fee_cents for r in rows) == program_fee_cents(offer, rules)
    assert all(r.program_fee_cents >= 0 for r in rows)
    first_pay = cadence[0]
    assert all(r.date >= first_pay for r in rows if r.program_fee_cents > 0)

    # -- independent re-simulation (S5.10): credits before debits, never negative
    credits: dict[date, int] = defaultdict(int)
    debits: dict[date, int] = defaultdict(int)
    for e in client.ledger:
        if e.date <= client.as_of_date:
            continue
        (credits if e.type == "credit" else debits)[e.date] += e.amount_cents
    by_date = {r.date: r for r in rows}

    balance = client.current_balance_cents
    for d in sorted(set(credits) | set(debits) | set(by_date)):
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        r = by_date.get(d)
        if r is not None:
            balance -= r.creditor_payment_cents + r.program_fee_cents + r.bank_fee_cents
        assert balance >= 0, f"balance went negative on {d}"
        if r is not None:
            assert r.balance_cents == balance, f"reported balance wrong on {d}"


def random_case(rng):
    """A randomised but well-formed (client, offer, rules) triple."""
    from feasibility.models import add_months

    as_of = date(2025, 12, 31)
    day = rng.choice([1, 5, 15, 28])
    first = date(2026, 1, day)
    months = rng.randint(2, 8)
    draft = rng.choice([5000, 10000, 20000])
    ledger = [LedgerEntry(add_months(first, i), draft, "credit") for i in range(months)]
    for _ in range(rng.randint(0, 2)):  # fixed debits from previously settled debts
        ledger.append(
            LedgerEntry(add_months(first, rng.randrange(months)), rng.randrange(1000, 20000), "debit")
        )
    client = Client(
        draft, day, first, add_months(first, months - 1), as_of,
        rng.choice([0, 5000]), ledger,
    )
    offer = Offer(
        "Rng",
        rng.randrange(20000, 200000),
        rng.randrange(20000, 200000),
        rng.choice([0.3, 0.4, 0.5, 0.6]),
        rng.choice([None, date(2026, 1, 31), date(2026, 1, day), date(2026, 2, 15)]),
    )
    k = rng.randint(1, 12)
    rules = CreditorRules(
        k, k, rng.choice([1000, 2500, 5000]), rng.randint(0, 6),
        rng.choice([[], [(3, 5000)], [(7, 5000)]]),
        rng.random() < 0.25, rng.random() < 0.25, rng.randint(1, 3),
        rng.choice([0, 500, 1000]), rng.choice([0.0, 0.1, 0.2, 0.25]),
    )
    return client, offer, rules
