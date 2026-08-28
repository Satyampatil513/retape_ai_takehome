# Settlement Feasibility & Fee Engine

Decide whether a settlement offer is affordable out of a client's escrow account;
if so, schedule it while collecting our program fee as early as possible; if not,
compute the minimum extra funding. Spec: [`ASSIGNMENT.md`](./ASSIGNMENT.md).

## Run

```bash
pip install -r requirements.txt

python run.py cases/case1_feasible_even    # evaluate a case, print the Result
python -m pytest -q                        # 62 tests, ~3s

python tools/trace.py cases/case4_tiers    # show the solver's work
```

Use `python -m pytest`, not bare `pytest`, unless your Python `Scripts` directory is
on PATH. Test files can't be run directly (`python tests/test_cases.py`) -- they need
pytest. The suite runs from any working directory.

## Layout

```
feasibility/
  money.py      half-up rounding
  shapes.py     cadence, position floors, legal payment vectors
  simulate.py   ledger walk + optimal fee placement
  solver.py     candidate enumeration, ranked by the objective
  funding.py    Part 2 minima, by binary search
  engine.py     evaluate_offer(); results.py holds the output types
tools/trace.py  read-only viewer for every candidate, the fee pass, the bisection
tests/helpers.py  case builders, a random case generator, an independent validator
```

## Results

| case | verdict | shape | schedule |
|---|---|---|---|
| case1_feasible_even | feasible | `even` | k=6, 8333 x4 + 8334 x2; fee done by Mar 31 |
| case2_infeasible_minima | infeasible | - | lump 10000 on 2026-01-01; increment 2500 x 5 drafts |
| case3_balloon | feasible | `balloon` | k=5, 2500 x4 + 20000 |
| case4_tiers | feasible | `staircase` | k=10, 2500 x6 + 11250 x4 |

---

# Approach

Two nearly independent decisions:

```
cadence_dates --> candidates(k, shape) --> simulate() --> rank --> best schedule
                  (creditor rules only)    (cash only)   (objective)
```

Payment placement is fixed by the spec (consecutive cadence dates from
`first_payment_date`), so a candidate is just a vector of amounts. We enumerate every
legal vector, place the fee optimally for each, and rank.

### Cadence and horizon

Monthly from `first_payment_date`, truncated at the horizon (`last_draft_date`). This
list serves two purposes: dates a payment may occupy, **and** dates the fee may be
collected on -- including fee-only dates after the last payment, which carry no bank
fee.

That second use decides case 2. Five $100 drafts against a $400 offer plus $100 fee is
exactly sufficient on paper, but the end-of-month cadence runs Jan 31 / Feb 28 /
Mar 31 / Apr 30 / **May 31**, and May 31 is past the May 1 horizon. The May 1 draft
lands inside the horizon with no cadence date left to spend it. Shortfall = $100.

### Payment shapes (the open-ended part)

**`even_pays`** -> all equal, remainder cents onto the latest payments (S5.7).
`max_segments` ignored. We still search `k`.

**`is_ballooning_allowed`** -> a balloon candidate is offered: every payment at its
floor except the last, which absorbs the remainder. `max_segments` ignored; needs
`k >= 2`; rejected if the remainder falls below the previous payment or its own floor.
Ballooning is permitted, not mandatory, so staircases compete alongside it.

**Otherwise -> staircase.** We enumerate every way to cut `k` positions into `s`
contiguous runs, for `s` in `1..max_segments`. Each run sits at its floor; the
remainder lands on the **final** run, keeping early payments as small as the rules
allow. At `k <= 12` that's a few hundred vectors, so we generate them all and let the
objective choose rather than guessing where a step belongs. `s = 1` reproduces the flat
vector, so the flattest and most back-loaded shapes both compete.

**Floors** combine three rules at 1-based position `i`:

```
floor(i) = max( min_payment_cents,
                min_payment_cents + 1   if i > max_token_pays,
                min_cents               for each tier with from_payment <= i )
```

The token rule is a *strict* exceed, hence the extra cent. Because payments are
non-decreasing, the ones sitting at the base minimum are always a prefix -- which is
what makes this positional formula exact. A balloon's "minimum-ish" prefix means
floors in this full sense, so a balloon with `k - 1 > max_token_pays` steps up a cent
at the cap.

A **±1-cent difference inside a run doesn't open a new segment** -- the remainder
waiver S5.7 grants `even`, extended to staircase runs.

`pay_shape_used` is derived from the winning vector, not the flags: one level ->
`even`, balloon builder -> `balloon`, else `staircase`.

### Objective

**Lexicographic maximum of the cumulative fee collected by cadence date 1, then 2,
then 3...** All candidates produce equal-length vectors, so they compare element-wise.
The shape is an outcome of this, never hard-coded. Ties break by:

1. **Prefer a balloon when the creditor allows one.** Load-bearing: case 3 has
   `program_fee_pct: 0.0`, so every candidate ties at zero and the objective alone
   can't choose.
2. **Fewer payments** -- each carries a bank fee. A preference we chose, not a spec
   constraint. Decides case 4: k=10 and k=12 reach the same fee vector, and k=10 saves
   the client $10 in bank fees.
3. **Smaller payments earliest**, for determinism.

### Fee placement: two passes, not a greedy

A forward greedy -- sweep whatever's left into the fee at each date -- is **incorrect**.
Fee taken at date `t` lowers the balance at every date after `t`, so a locally maximal
grab can starve a later payment. Instead:

1. Simulate with payments and bank fees but **no fee**; record the balance after every
   date. Any negative -> this candidate is unaffordable regardless of fee placement.
2. Take **suffix minima over all dates**, not just cadence dates -- a fixed ledger
   debit between two cadence dates constrains how much we may pull early (case 3's
   -$150 on Feb 1).
3. At each cadence date take `min(remaining fee, suffix_min[d] - fee already taken)`.

Step 3 is the lexicographic maximum, and if it can't finish the fee by the horizon then
no allocation can. S5.6(a) holds by construction: the walk starts at cadence index 0,
which *is* `first_payment_date`.

On case 4's Jun 30 row the balance is $420 but the suffix minimum is $350, held down by
Oct 31 four months later. A greedy would read $420 and overdraw. `tools/trace.py`
prints both columns.

### Part 2: minimum additional funds

Binary search over cents against a short-circuiting `is_feasible()` oracle. Bisection
is valid because feasibility is **monotone** in extra money, which rests on two facts:
the only cash rule is `balance >= 0` (no cap, no "must end at zero" -- case 1 finishes
with $340 idle), and the candidate set doesn't depend on cash, so money can't unlock a
cheaper *shape*. The ceiling is every possible outflow plus every fixed debit; if even
that fails we report un-fundable.

**Lump placement** is `as_of_date + 1`, the earliest modifiable date. This is weakly
optimal: for a lump `L` on date `d`, the balance at `x` is
`base(x) - outflows(x) + L * [d <= x]`; moving it earlier flips that indicator on
sooner and never off, so the balance is weakly higher everywhere and any `L` that
worked still works. So `minL(date)` is non-decreasing. A seeded property test enforces
it -- *if `L` is minimal at the earliest date, `L - 1` must fail at every later date.*

A whole range of dates ties at the minimum (Jan 1 through Apr 30 in case 2); we report
the earliest. See assumption 7.

**Monthly increment** goes on every ledger credit after `as_of_date` (S3: the credits
*are* the drafts). `num_drafts` counts every draft **affected**, including ones that
arrive too late to help -- case 2 reports 5 while only 4 do work, which is why the two
minima imply different totals ($100 vs $125), as S8 predicts.

---

# Alternatives considered

- **DP to build the payment vector** -- states of (position, cents allocated so far,
  segments used), transitioning on each payment's amount. Correct, but the state space
  is dominated by the cents dimension (`offer_total` runs to hundreds of thousands)
  while the thing we actually need -- every legal vector -- is only a few dozen per `k`
  at the segment caps these creditors set. Straight enumeration is smaller, faster and
  much easier to read. DP would start to pay off only at a far larger `k` or
  `max_segments`, which is the same boundary noted under limitations.
  (The *fee* needs no search at all -- see the two-pass argument above.)
- **Integer / MILP over the whole problem** -- would handle every constraint uniformly
  and need no shape reasoning, but adds a solver dependency, makes "why this schedule?"
  opaque, and needs a fiddly lexicographic-objective encoding. Overkill at this size.
- **Linear scan over lump amounts** -- O(L) probes where bisection is O(log L) (~16
  probes over 50001 cents). Monotonicity buys the reduction, so we test it rather than
  assume it.
- **Forward greedy for the fee** -- rejected as incorrect, not just suboptimal. Two
  tests pin the failure mode.
- **Equal-length segment runs** -- our first staircase design; enumerating all cut
  points subsumes it and is less arbitrary. Same answer on case 4, where the tier pins
  the boundary anyway.
- **Hard-coding shape per flag** -- rejected; the spec asks for shape as an outcome of
  the objective, which is what makes the k=10-vs-12 and balloon-vs-flat calls fall out.

---

# Assumptions

1. `k` is ours to choose; `first_payment_date` is a hard input (S5.1).
2. Payments and fees may land only on cadence dates.
3. The fee window is every cadence date at or before the horizon, fee-only dates
   included.
4. Ledger entries on or before `as_of_date` are skipped -- already in
   `current_balance_cents` (S3).
5. The balance is checked once per date, after all that date's activity, credits before
   debits. No intra-day ordering among debits.
6. One ledger credit entry = one draft, so two credits on a date count as two drafts.
7. The lump goes on the earliest modifiable date, not the latest date that would tie.
   Same minimum `L` either way; the latest would be friendlier to a client but leaves
   zero slack.
8. A leftover balance is fine; nothing requires finishing at zero.
9. Unused cadence dates are omitted rather than emitted as zero rows (case 4 prints 10
   rows for 12 cadence dates).
10. Half-up rounding wherever a percentage is applied, including both guardrails (S3).
11. `max_terms` and `max_payments` bind identically, per the assignment's author note.
12. A single payment is not a balloon (`k >= 2`).
13. A structurally impossible offer reports `amount_cents: 0` with a reason rather than
    a fabricated number. The spec doesn't define this case.

---

# Known limitations

- **Only the minimal-early fill is generated per cut set.** A schedule needing an early
  run raised *above* its floor purely to afford a very large final payment is outside
  the search. Both ends of the spectrum are covered, so this only bites for a case whose
  only feasible schedule sits strictly between them.
- **A staircase can be a balloon in all but name.** With `max_segments >= 2` and no
  tiers, "steps as late as possible" yields minimums then one large final payment even
  when `is_ballooning_allowed` is false. Case 3 shows it: at k=5 the staircase
  enumeration independently produces `2500 x4, 20000`, identical to the balloon vector.
  On this reading the flag mostly affects labelling and the tie-break rather than
  structure. `shapes.REQUIRE_NON_BALLOON_TAIL` (default `False`) flips to the stricter
  reading; it costs nothing on the four provided cases.
- **Cut-set enumeration is `2^(k-1)` worst case** when `max_segments >= k`. Bounded by
  `k <= 12` here (2048 vectors, ~3ms); a creditor allowing 36 payments would need a
  smarter search.
- **Case 2's shortfall is a calendar artifact, not an affordability problem.** The
  client has exactly enough money, stranded by a 30-day phase mismatch between day-1
  drafts and an end-of-month cadence. Moving `first_payment_date` to 2026-01-01 -- and
  only to day 1, of all 31 options -- makes it feasible with no extra funding. We don't
  act on it: the date is an input, and doing so would contradict the provided
  expectation that case 2 is infeasible.

---

# Tests

`python -m pytest -q` -- 62 tests, ~3s.

The centrepiece is `tests/helpers.py::assert_valid_schedule`, an **independent
validator** that re-derives the cadence, floors and totals from raw inputs and
re-simulates the ledger by hand, deliberately *not* reusing `feasibility.simulate`, so
a simulator bug can't hide behind itself. It checks all ten hard constraints from S5
and runs against every feasible result.

Beyond the provided expectations: all three shapes and their labelling; token-cap and
tier floors and their interaction; the `max_segments` cap and the ±1-cent waiver;
exact-sum; same-day credit-before-debit ordering (constructed so debits-first would
fail the offer); balances landing on exactly zero; EOM vs mid-month cadence with
clamping; the horizon cutoff; fee capped by a *later* debit rather than today's
balance; fee deferring past a squeezed month (`[6000, 0, 1500]`); fee-only dates
carrying no bank fee; half-up vs banker's rounding; both Part 2 minima to the cent;
both guardrails tripping and passing; un-fundable paths; and a seeded property test
over random cases for the lump-placement rule.

# Fixes to the provided scaffolding

- **`Offer.current_balance_cents` -> `creditor_balance_cents`.** S4 renames it (the old
  name collided with the client's SDA balance), but `models.py` and the case JSONs still
  used the old spelling. The loader accepts either key.
- **`round()` -> half-up.** `offer_total_cents` and `program_fee_cents` used Python's
  banker's rounding, which S3 explicitly forbids. Both now use `Decimal` with
  `ROUND_HALF_UP`; `Decimal(str(pct))` also avoids drift like
  `0.4 * 150000 = 60000.000000000007`.
- **Test bootstrapping.** `pyproject.toml` sets `pythonpath`, and `tests/conftest.py`
  pins the repo root, so the suite runs from any directory. All of `feasibility/` is
  ASCII-only so the package imports regardless of the reader's locale codec.
