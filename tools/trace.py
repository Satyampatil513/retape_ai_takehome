"""Show the solver's work for one case: every candidate, and the fee pass.

    python tools/trace.py cases/case4_tiers
    python tools/trace.py cases/case4_tiers --k 10      # fee pass for a given k
    python tools/trace.py cases/case2_infeasible_minima

Read-only: it drives the same public functions the engine does and prints what
they return. Nothing here affects the answer.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feasibility.models import (  # noqa: E402
    load_case,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.shapes import cadence_dates, floor_at  # noqa: E402
from feasibility.simulate import simulate  # noqa: E402
from feasibility.solver import candidates, solve  # noqa: E402


def rle(v: list[int]) -> str:
    """Compress a payment vector: [2500,2500,2500,7500] -> '2500 x3, 7500'."""
    runs: list[list[int]] = []
    for x in v:
        if runs and runs[-1][0] == x:
            runs[-1][1] += 1
        else:
            runs.append([x, 1])
    return ", ".join(f"{a} x{n}" if n > 1 else str(a) for a, n in runs)


def fee_steps(cum: tuple[int, ...]) -> str:
    """Per-date fee amounts, trailing zeros trimmed."""
    steps = [cum[0]] + [cum[i] - cum[i - 1] for i in range(1, len(cum))]
    while steps and steps[-1] == 0:
        steps.pop()
    return "[" + ", ".join(str(s) for s in steps) + "]"


def header(client, offer, rules, cadence) -> None:
    total = offer_total_cents(offer)
    k_max = min(rules.max_payments, rules.max_terms, len(cadence))
    print(f"offer total      {total}")
    print(f"program fee      {program_fee_cents(offer, rules)}")
    print(f"bank fee/payment {rules.bank_fee_cents}")
    print(f"horizon          {client.last_draft_date}")
    if cadence:
        print(f"cadence ({len(cadence)})      {cadence[0]} .. {cadence[-1]}")
    else:
        print("cadence (0)      none at or before the horizon")
    print(f"k range          1..{k_max}")
    flags = []
    if rules.even_pays:
        flags.append("even_pays")
    if rules.is_ballooning_allowed:
        flags.append("is_ballooning_allowed")
    flags.append(f"max_segments={rules.max_segments}")
    print(f"flags            {', '.join(flags)}")
    if k_max:
        print(f"floors           {[floor_at(i, rules) for i in range(1, k_max + 1)]}")


def show_candidates(client, offer, rules, cadence, fee_total) -> None:
    print()
    print("-- candidates ------------------------------------------------------")
    print("   k  shape      verdict")
    best_key = None
    best_k = None
    rows = []
    for cand in candidates(cadence, offer, rules):
        sim = simulate(client, cadence, cand.payments, rules, fee_total, trace=True)
        k = len(cand.payments)
        if sim.ok:
            key = (
                sim.cum_fee,
                1 if (cand.from_balloon and rules.is_ballooning_allowed) else 0,
                -k,
                tuple(-p for p in cand.payments),
            )
            if best_key is None or key > best_key:
                best_key, best_k = key, k
            verdict = f"ok    fee {fee_steps(sim.cum_fee)}"
        else:
            verdict = f"NO    {sim.trace.reason}"
        rows.append((k, cand, sim, verdict, key if sim.ok else None))

    for k, cand, sim, verdict, key in rows:
        mark = "  <-- BEST" if sim.ok and key == best_key else ""
        print(f"  {k:2d}  {cand.shape:<10} {verdict}{mark}")
        print(f"      pay   {rle(cand.payments)}")
    if best_k is None:
        print("\n  no candidate is affordable")
    return best_k


def show_fee_pass(client, offer, rules, cadence, fee_total, payments, shape) -> None:
    sim = simulate(client, cadence, payments, rules, fee_total, trace=True)
    t = sim.trace
    print()
    print(f"-- fee pass: k={len(payments)}, {shape} -----------------------------------")
    print(
        "  date         credit    debit  payment    bank   bal(A)  sufxmin      cap"
        "      fee  balance"
    )
    cadence_set = set(cadence)
    pay_on = {cadence[i]: payments[i] for i in range(len(payments))}
    taken = 0
    for d in t.dates:
        bal_a = t.balances.get(d)
        pay = pay_on.get(d, 0)
        bank = rules.bank_fee_cents if pay else 0
        if d in cadence_set and d in t.suffix_min:
            sufx = t.suffix_min[d]
            cap = sufx - taken
            fee = t.fee_on.get(d, 0)
            taken += fee
            sufx_s, cap_s, fee_s = str(sufx), str(cap), str(fee)
        else:
            sufx_s = cap_s = fee_s = "-"
        final = "-" if bal_a is None else str(bal_a - taken)
        print(
            f"  {d}  {t.credits.get(d, 0):7d}  {t.debits.get(d, 0):7d}  {pay:7d}  {bank:6d}"
            f"  {('-' if bal_a is None else bal_a):>7}  {sufx_s:>7}  {cap_s:>7}"
            f"  {fee_s:>7}  {final:>7}"
        )
    if not sim.ok:
        print(f"\n  INFEASIBLE: {t.reason}")
    else:
        print("\n  bal(A) is the balance with payments and bank fees but no program fee.")
        print("  cap = sufxmin - fee already taken; the fee column is min(remaining, cap).")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python tools/trace.py <case_dir> [--k N]", file=sys.stderr)
        return 2
    case_dir = argv[1]
    want_k = None
    if "--k" in argv:
        want_k = int(argv[argv.index("--k") + 1])

    client, offer, rules = load_case(case_dir)
    cadence = cadence_dates(client, offer)
    fee_total = program_fee_cents(offer, rules)

    print(f"== {case_dir} ==")
    header(client, offer, rules, cadence)
    if not cadence:
        return 0

    best_k = show_candidates(client, offer, rules, cadence, fee_total)

    outcome = solve(client, offer, rules)
    k = want_k if want_k is not None else best_k
    if k is None:
        return 0
    if want_k is not None:
        picked = next(
            (c for c in candidates(cadence, offer, rules) if len(c.payments) == k), None
        )
        if picked is None:
            print(f"\nno candidate with k={k}")
            return 1
        show_fee_pass(client, offer, rules, cadence, fee_total, picked.payments, picked.shape)
    elif outcome.solution is not None:
        show_fee_pass(
            client, offer, rules, cadence, fee_total,
            outcome.solution.payments, outcome.solution.shape,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
