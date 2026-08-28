# Settlement Feasibility & Fee Engine

Decide whether a settlement offer is affordable out of a client's escrow account;
if so, schedule it while collecting our program fee as early as possible; if not,
compute the minimum extra funding. Spec: [`ASSIGNMENT.md`](./ASSIGNMENT.md).

## Run

```bash
pip install -r requirements.txt

python run.py cases/case1_feasible_even    # evaluate a case, print the Result
python -m pytest -q                        # 67 tests, ~3s

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

```
cadence dates -> for each k -> shape per flags -> simulate: place fee -> rank
    (S3)           (S5.1)        (S5.7-5.9)         (S5.6, S5.10)        (S6)
```

One split makes this tractable: **which vector** follows from the creditor rules alone,
no cash involved; **where the fee goes** follows from cash alone, vector already fixed.

## 1. Cadence, horizon, and the range of k

Monthly from `first_payment_date`, truncated at the horizon (`last_draft_date`).
Payments occupy its first `k` dates consecutively (S5.1), so a candidate is just a
vector of amounts, with `k` in `1..min(max_payments, max_terms, len(cadence))`.

The same list is the **fee window**, fee-only dates after the last payment included
(those carry no bank fee). That second use decides case 2: five $100 drafts against
$400 + $100 fee is exactly enough on paper, but the end-of-month cadence jumps
Apr 30 -> **May 31**, past the May 1 horizon. The May 1 draft lands inside the horizon
with no cadence date left to spend it, so the shortfall is exactly $100.

## 2. Floors

Every shape is built from the floor at 1-based position `i`:

```
floor(i) = max( min_payment_cents,
                min_payment_cents + 1   if i > max_token_pays,
                min_cents               for each tier with from_payment <= i )
```

The token rule is a *strict* exceed, hence the extra cent. Payments are non-decreasing,
so those at the base minimum are always a prefix -- which makes this positional formula
exact rather than an approximation.

## 3. Shape: three cases per k

| flag | per k | vector |
|---|---|---|
| `even_pays` | 1 | all equal, remainder cents onto the latest (S5.7) |
| `is_ballooning_allowed` | 1 | every position on its floor, last absorbs the remainder |
| neither | many | staircases -- step 4 |

`max_segments` is ignored in the first two (S4).

Ballooning is permitted rather than mandatory, so staircases ought to compete -- but
cannot win. A balloon puts every position on its **own** floor; a staircase run is one
level, so a run spanning a floor jump drags its earlier positions up to `top - 1`. The
balloon's prefix sums are therefore the pointwise minimum over every valid vector at
that `k`, so where one exists we skip the staircases. Only `k = 1` still needs them: a
balloon costs exactly `sum(floors)`, the least any valid vector can cost, so it exists
whenever anything does.

`pay_shape_used` comes from the winning vector, not the flags: one level -> `even`,
balloon builder -> `balloon`, else `staircase`.

## 4. Enumerating staircases

Every way to cut `k` positions into `s` contiguous runs, `s` in `1..max_segments`. Each
run sits at the lowest values its floors allow; the remainder lands on the **final**
run, keeping early payments as small as the rules permit. `s = 1` is the flat vector, so
both ends of the spectrum compete and the objective picks the step placement.

**One fill per cut set suffices.** All valid vectors sum to `offer_total`, so the final
balance is identical across them; only earlier dates differ, and they improve as the
running total spent falls. The cheapest fill minimises every prefix sum for its cut set.

**Every cut set is kept**, because across them no single vector is cheapest. An earlier
cut lets position 1 sit on its own low floor but leaves a bigger tail; a later cut drags
early positions up to `top - 1` and leaves a smaller one. Floors `[200, 900, 900, 900]`,
total 4000:

```
[200, 1266, 1267, 1267]   prefix sums  200, 1466, 2733
[899,  900,  900, 1301]   prefix sums  899, 1799, 2699
```

Neither is pointwise below the other, and each wins for some ledger. A **one-cent
difference inside a run** does not open a new segment -- the waiver S5.7 grants `even`,
extended to staircase runs. Cut sets are the powerset of the gaps, so this runs as a
memoised DFS -- see Complexity.

## 5. Placing the fee: two passes, not a greedy

A forward greedy -- sweep what is left into the fee at each date -- is **incorrect**:
fee taken at `t` lowers the balance at every later date, so a locally maximal grab can
starve a later payment. Instead:

1. Simulate with payments and bank fees but **no fee**, recording the balance after
   every date. Any negative and the candidate is unaffordable however the fee is placed.
2. Take **suffix minima over all dates**, not just cadence dates -- a fixed ledger debit
   between two cadence dates limits how much we may pull early (case 3's -$150 on Feb 1).
3. At each cadence date take `min(remaining fee, suffix_min[d] - fee already taken)`.

Step 3 is the lexicographic maximum; if it cannot finish the fee by the horizon, no
allocation can. S5.6(a) holds by construction -- the walk starts at cadence index 0,
which *is* `first_payment_date`.

Case 4's Jun 30 row: balance $420, suffix minimum $350, held down by Oct 31 four months
later. A greedy would read $420 and overdraw.

## 6. Ranking

**Lexicographic maximum of the cumulative fee by cadence date 1, then 2, then 3...**
All candidates produce equal-length vectors, so they compare element-wise, and the shape
is an outcome of this rather than hard-coded. Ties break by:

1. **Prefer a balloon where allowed.** Load-bearing: case 3 has `program_fee_pct: 0.0`,
   so every candidate ties at zero and the objective alone cannot choose.
2. **Fewer payments** -- each carries a bank fee. Our preference, not a spec constraint.
   Decides case 4: k=10 and k=12 tie on fee, and k=10 saves the client $10.
3. **Smaller payments earliest**, for determinism.

## 7. Part 2: minimum additional funds

Binary search over cents against a short-circuiting feasibility oracle. Bisection is
valid because feasibility is **monotone** in extra money, resting on two facts: the only
cash rule is `balance >= 0` (no cap, no "must end at zero" -- case 1 finishes with $340
idle), and the candidate set does not depend on cash, so money cannot unlock a cheaper
*shape*. The ceiling is every possible outflow plus every fixed debit; if that fails we
report un-fundable.

**Lump sum** goes on `as_of_date + 1`, the earliest modifiable date, which is weakly
optimal: for a lump `L` on `d` the balance at `x` is `base(x) - outflows(x) + L*[d <= x]`,
so moving it earlier flips that indicator on sooner and never off -- the balance is
weakly higher everywhere and any `L` that worked still works. Hence `minL(date)` is
non-decreasing. A property test enforces it: *if `L` is minimal at the earliest date,
`L - 1` must fail at every later date.* A whole range of dates ties at that minimum
(Jan 1 through Apr 30 in case 2); we report the earliest (assumption 7).

**Monthly increment** goes on every ledger credit after `as_of_date` (S3: the credits
*are* the drafts). `num_drafts` counts every draft **affected**, including ones arriving
too late to help -- case 2 reports 5 while only 4 do work, which is why the two minima
imply different totals ($100 vs $125), as S8 predicts.

---

# Complexity

**D** = cadence dates at or before the horizon, **M** = distinct dates simulated,
**K** = `min(max_payments, max_terms, D)`, **S** = `max_segments`.

| stage | cost |
|---|---|
| candidate enumeration | see below |
| `simulate()` per candidate | O(M); the dated view is built once per search |
| Part 2 | x ~17 bisection probes per search |

**Why the naive enumeration is exponential.** Cutting `k` positions into `s` contiguous
runs means choosing `s - 1` cut points from the `k - 1` gaps, i.e. `C(k-1, s-1)`:

```
S >= k    ->  sum(j=0..k-1) C(k-1, j) = 2^(k-1)     the powerset of the gaps
S small   ->  O(k^(S-1))                            polynomial
```

`max_segments >= max_payments` just means "no segment limit", which a creditor may well
send, and it walks the whole powerset -- 2^22 ~ 4.2M cut sets at k=23, yielding the same
850 valid vectors that `S = 3` does.

**Why not a scoring DP.** Our objective is not prefix-separable: the fee at a date
depends on the suffix minimum of the whole balance trajectory, so a partial vector
cannot be reduced to one best value per state. A DP over `(position, segments, cents
allocated)` would work but reintroduces the cents dimension we rejected for the payment
vector in the first place.

**What we do instead** is the other half of DP -- memoised DFS over shared prefixes,
which is where the redundancy actually lives:

- **Prune** on two *necessary* conditions before building a vector: the positions still
  to cover cannot cost less than their own floors, and the final run's spread cannot
  start below the run before it.
- **Memoise** on `(position, head)`, keeping the fewest runs the state was reached with
  -- arriving with runs to spare leaves more cuts available downstream.

Both are lossless; the candidate set is identical, which
`test_solver_matches_exhaustive_search_on_small_cases` checks. Two smaller wins: the
dated ledger view is built once per search rather than per candidate, and Part 2
enumerates candidates once rather than once per probe (~32 across the two searches).

```
months   S   before ms   after ms   speedup
    36   3       144.4       66.4         2x
    36   4       657.6       81.4         8x
    24  24    115785.9       97.5      1187x
```

The provided cases are unaffected -- all four still evaluate in single-digit ms.

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

- **Only the cheapest fill is generated per cut set**, which loses nothing. Every valid
  vector sums to `offer_total`, so the final date's balance is identical across all of
  them; only the earlier dates differ, and they improve as the running total spent
  falls. The cheapest fill minimises every prefix sum for its cut set, so it dominates
  every other fill on both feasibility and fee capacity. Checked against exhaustive
  enumeration -- see Tests.
- **A staircase can be a balloon in all but name.** With `max_segments >= 2` and no
  tiers, "steps as late as possible" yields minimums then one large final payment even
  when `is_ballooning_allowed` is false. Case 3 shows it: at k=5 the staircase
  enumeration independently produces `2500 x4, 20000`, identical to the balloon vector.
  On this reading the flag mostly affects labelling and the tie-break rather than
  structure. `shapes.REQUIRE_NON_BALLOON_TAIL` (default `False`) flips to the stricter
  reading; it costs nothing on the four provided cases.
- **Enumeration is still worst-case exponential in theory.** The pruning and
  memoisation above make `max_segments >= max_payments` tractable (2 minutes -> 0.1s at
  k=23), but no bound proves it stays that way for every rules set. A creditor allowing
  many payments with unusual floors could still be slow.
- **Case 2's shortfall is a calendar artifact, not an affordability problem.** The
  client has exactly enough money, stranded by a 30-day phase mismatch between day-1
  drafts and an end-of-month cadence. Moving `first_payment_date` to 2026-01-01 -- and
  only to day 1, of all 31 options -- makes it feasible with no extra funding. We don't
  act on it: the date is an input, and doing so would contradict the provided
  expectation that case 2 is infeasible.

---

# Tests

`python -m pytest -q` -- 67 tests, ~3s.

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

`test_solver_matches_exhaustive_search_on_small_cases` is the strongest check: on tiny
inputs it enumerates *every* vector S5 admits, scores each with the same simulator, and
asserts the solver ties the best. It caught two real gaps in the enumeration -- a last
run pre-rejected against its own maximum floor, and a leading run forced to a flat
maximum rather than positional floors -- both since fixed.

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
