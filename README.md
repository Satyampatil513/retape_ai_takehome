# Settlement Feasibility & Fee Engine

Given a client's escrow account (SDA), a settlement offer, and a creditor's rules,
decide whether the offer is affordable, and if so produce a payment schedule that
collects our program fee as early as possible. If it is not affordable, compute the
minimum extra funding that would make it affordable.

The full specification is in [`ASSIGNMENT.md`](./ASSIGNMENT.md).

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# evaluate a case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q                                # 62 tests, ~3s

# show the solver's work: every candidate, the fee pass, the funding search
python tools/trace.py cases/case4_tiers
python tools/trace.py cases/case4_tiers --k 12          # fee pass for a chosen k
python tools/trace.py cases/case2_infeasible_minima     # + the funding bisection
```

## Layout

```
feasibility/
  models.py     data models, loaders, date helpers (provided; two fixes, see below)
  money.py      half-up rounding
  shapes.py     cadence, position floors, and legal creditor-payment vectors
  simulate.py   date-by-date ledger walk + optimal program-fee placement
  solver.py     candidate enumeration, ranked by the fee objective
  funding.py    Part 2: the two minima, by binary search
  results.py    the output dataclasses
  engine.py     evaluate_offer(), and re-exports of the result types
tools/
  trace.py      read-only viewer for the solver's intermediate state
tests/
  helpers.py       case builders, a random case generator, and an
                   INDEPENDENT schedule validator
  test_cases.py    the provided example expectations
  test_smoke.py    the provided scaffolding sanity tests
  test_engine.py   Part 1
  test_funding.py  Part 2
```

## Results on the provided cases

| case | verdict | shape | schedule |
|---|---|---|---|
| case1_feasible_even | feasible | `even` | k=6, 8333 x4 + 8334 x2; fee fully collected by Mar 31 |
| case2_infeasible_minima | infeasible | - | lump 10000 on 2026-01-01; increment 2500 across 5 drafts |
| case3_balloon | feasible | `balloon` | k=5, 2500 x4 + 20000 |
| case4_tiers | feasible | `staircase` | k=10, 2500 x6 + 11250 x4 |

---

# Approach

The problem splits into two decisions that are almost independent:

1. **Which creditor-payment vector?** Determined entirely by the creditor's rules --
   floors, counts, shape flags. No cash involved.
2. **Given that vector, where does the program fee go?** Determined entirely by cash.

That separation is what makes the whole thing tractable. Payment placement is fixed
by the spec (consecutive cadence dates from `first_payment_date`), so a candidate is
just a vector of amounts. We enumerate every legal vector, place the fee optimally
for each, and rank the results.

```
cadence_dates ---> candidates(k, shape) ---> simulate() ---> rank ---> best schedule
                   (rules only)              (cash only)     (objective)
```

## Cadence and the horizon

The cadence is monthly from `first_payment_date`, truncated to dates at or before
the horizon (`last_draft_date`). Crucially this list is used for **two** things: it
is the set of dates a creditor payment may occupy, and it is also the set of dates
the program fee may be collected on -- including the "fee-only" dates after the last
creditor payment, which carry no bank fee.

Getting that second use right is what decides case 2. The client has 5 drafts of
$100 against a $400 offer plus a $100 fee, so the money is exactly sufficient on
paper. But `first_payment_date` is 2026-01-31, which is end-of-month, so the cadence
runs Jan 31 / Feb 28 / Mar 31 / Apr 30 / **May 31** -- and May 31 is past the May 1
horizon. The May 1 draft lands inside the horizon but with no cadence date left to
spend it on. It is dead money, the shortfall is exactly $100, and that is the
expected `lump_sum`. See "Known limitations" for more on this.

## Payment shapes (the open-ended part)

The assignment leaves the shapes loosely defined on purpose. Our reading:

**`even_pays = true` -> `even`.** All payments equal, remainder cents distributed
onto the latest payments so the sequence stays non-decreasing (S5.7). `max_segments`
is ignored, per S4. We still search over `k`.

**`is_ballooning_allowed = true` -> a `balloon` candidate is offered.** Every payment
except the last sits at its floor; the last absorbs the entire remainder.
`max_segments` is ignored while ballooning, again per S4. A balloon needs `k >= 2`
(a single payment is not a balloon, it is the whole settlement) and is rejected if
the remainder would fall below the previous payment or below its own floor.
Ballooning is *permitted*, not mandatory, so the staircase candidates are still
generated alongside and the objective picks.

**Otherwise -> `staircase`, at most `max_segments` distinct levels.** Rather than
hand-place the steps, we enumerate every way to cut the `k` positions into `s`
contiguous runs, for every `s` from 1 to `max_segments`. Each run sits at the lowest
level its floors allow; whatever is left over lands on the **final** run, which is
what keeps the early payments as small as the rules permit. At `k <= 12` this is at
most a few hundred vectors, so we can afford to generate them all and let the
objective choose rather than guessing where a step "should" go.

A useful property of this enumeration: `s = 1` reproduces the flat vector, so the
flattest and the most back-loaded shapes are both in the candidate set and the
ranking decides between them. An earlier design of ours fixed the runs at equal
length; enumerating all cuts subsumes that and is less arbitrary.

**How token pays and tiers interact with all of this.** The floor at 1-based
position `i` is the maximum of three rules:

```
floor(i) = max( min_payment_cents,
                min_payment_cents + 1   if i > max_token_pays,
                min_cents               for each tier with from_payment <= i )
```

The token rule is a *strict* exceed -- "any further payment must exceed the base
minimum" -- so past the cap the floor is one cent above the base. Because payments
are non-decreasing, the payments sitting exactly at the base minimum are always a
prefix, which is what makes this positional formula exact rather than an
approximation. Tiers and the token cap combine by maximum, and the resulting floor
sequence is non-decreasing, so a vector built from floors is automatically
non-decreasing too.

For a balloon this means the "minimum-ish payments early" are floors in this full
sense, not naive copies of `min_payment_cents`: a balloon with `k - 1 > max_token_pays`
steps its prefix up by a cent at the cap.

**Reported shape.** `pay_shape_used` is derived from the vector that won, not from
the flags: one level -> `even`, the balloon builder -> `balloon`, otherwise
`staircase`. So a creditor with `even_pays = false` whose best schedule happens to
be flat is honestly reported as `even`.

**A ±1-cent difference inside a run does not open a new segment.** S5.7 explicitly
blesses remainder distribution for `even`; we extend the same waiver to a staircase's
runs, otherwise an indivisible remainder would silently consume a segment.

## The objective, and how ties break

> Collect the program fee as early as possible.

Made precise: **lexicographic maximum of the cumulative fee collected by cadence
date 1, then 2, then 3, ...** Every candidate produces a vector of the same length
(one entry per cadence date), so they compare element-wise. This is the whole
ranking; the shape is an outcome of it, never hard-coded.

Ties are broken, in order:

1. **Prefer a genuine balloon when the creditor allows one.** This is load-bearing,
   not cosmetic: case 3 has `program_fee_pct: 0.0`, so *every* candidate scores
   identically at zero and the objective alone cannot choose. A flat 6 x $50 is
   equally feasible there. Without this tie-break the reported shape would be
   arbitrary.
2. **Fewer creditor payments.** Each payment date carries a bank fee, so a shorter
   schedule is cheaper for the client at equal fee earliness. This is a *preference*
   we chose, not a constraint in the spec -- nothing requires minimising `k`. It
   decides case 4: k=10 and k=12 both reach the fee vector
   `(7000, 14000, 21000, 28000, 30000, ...)`, and k=10 wins on 2 fewer bank fees,
   saving the client $10.
3. **Smaller payments earliest**, purely so the output is deterministic.

## Placing the fee: two passes, not a greedy

This is the part most likely to be got wrong, so it is worth spelling out.

The tempting approach is a forward greedy: at each cadence date, after paying the
creditor, sweep whatever is left into the fee. **That is incorrect.** Fee taken at
date `t` lowers the balance at every date after `t`, so a locally maximal grab can
starve a later creditor payment. Greedy minimises the balance at every future date,
which is precisely what you cannot afford to do.

The correct version is exact and costs one extra pass:

1. **Pass A.** Simulate the full ledger with the creditor payments and bank fees but
   **no fee**, recording the balance after every date's activity. If any date goes
   negative, this candidate is unaffordable regardless of fee placement.
2. **Suffix minima** of those balances, over **all** dates -- not just cadence dates.
   A fixed ledger debit sitting between two cadence dates constrains how much we may
   pull early, and case 3's -$150 on Feb 1 is exactly that.
3. **Pass B.** At each cadence date in order, take
   `min(remaining fee, suffix_min[d] - fee already taken)`.

Step 3 is the lexicographic maximum: at each date it takes the largest amount that
cannot break any future date, and since it maximises the cumulative fee at every
prefix, if it cannot finish the fee by the horizon then no allocation can. S5.6(a)
(no fee before the first creditor payment) holds by construction, because the fee
walk starts at cadence index 0, which *is* `first_payment_date`.

`tools/trace.py` prints the `sufxmin` and `cap` columns so this is visible. On
case 4's Jun 30 row the balance that day is $420, but the suffix minimum is $350 --
held down by Oct 31, four months later. A forward greedy would read $420 and overdraw.

## Part 2: minimum additional funds

Both minima are found by **binary search over cents** against a short-circuiting
`is_feasible()` oracle (it returns on the first affordable candidate rather than
ranking all of them).

Bisection is valid because feasibility is **monotone** in extra money, and that
rests on two facts specific to this problem:

- The only cash rule is `balance >= 0` (S5.10). There is no cap, no "must end at
  zero", no penalty for leftover -- case 1 finishes with $340 idle in the account.
  Surplus cash is inert, never harmful.
- The candidate set does not depend on cash at all. Shapes come from the creditor
  rules, so money cannot unlock a *different*, cheaper schedule.

The search ceiling is everything we could ever debit (settlement + fee + every bank
fee) plus every fixed debit already on the books; sitting in the account from the
start, that cannot leave any date negative. If even the ceiling fails, we report
un-fundable rather than returning a number.

**Lump sum placement.** We place it on `as_of_date + 1`, the earliest date we are
permitted to touch (S3: entries on or before `as_of_date` are already baked into
`current_balance_cents`, so adding one there would be rewriting settled history).

Placing it earliest is *weakly optimal*, and we can say why rather than assuming it.
For a lump `L` on date `d`, the balance on any date `x` is
`base(x) - outflows(x) + L * [d <= x]`. Moving the lump earlier to `d' <= d` makes
that indicator flip on sooner and never off, so the balance is weakly higher on every
date and every `balance >= 0` check that passed still passes. Therefore `minL(date)`
is non-decreasing and the earliest legal date attains the minimum. This is enforced by
a seeded property test over randomly generated cases, stated as: *if `L` is minimal at
the earliest date, then `L - 1` must fail at every later date.*

Note this leaves a genuine choice unresolved. A whole *range* of dates ties at the
minimum -- in case 2, every date from Jan 1 through Apr 30 gives the same $100, and
only May 1 fails. We report the earliest because S8's phrasing points that way
("an earlier lump is weakly more useful") and because it is correct under every case.
Reporting the *latest* tying date would be friendlier to a client who would rather not
wire money in January, at the cost of an extra search and zero slack. See assumptions.

**Monthly increment.** Added to every ledger credit dated after `as_of_date`, since
S3 says the credits in the ledger *are* the drafts. `num_drafts` counts every draft
**affected**, even ones that arrive too late to be useful -- case 2 reports 5 while
only 4 do any work. That is why the two minima imply different totals ($100 lump vs
$125 of increments), exactly as S8 predicts.

---

# Alternatives considered

**Dynamic programming over (cadence date, fee collected so far)** for the fee
placement. Correct, but unnecessary: once the payment vector is fixed the cash is
fungible and the suffix-minimum argument gives the provable optimum in a single
linear pass. DP would add a state dimension of size `program_fee_cents` for an answer
we can compute exactly in O(dates).

**An integer / mixed-integer program** over the whole thing -- payments, fee split
and feasibility as one constrained optimisation. It would handle the objective and
every constraint uniformly and would not need the shape reasoning at all. Rejected as
overkill for this size: it adds a solver dependency, makes the reasoning opaque to a
reader, turns "why this schedule?" into "ask the solver", and needs a careful
lexicographic-objective encoding. The problem's structure -- a small candidate set,
plus fungible cash -- makes the direct method both faster and far easier to justify.

**Linear scan over lump-sum amounts** instead of bisection. Simple and obviously
correct, but it is O(L) feasibility probes where bisection is O(log L): ~16 probes
against a 50001-cent range for case 2. Monotonicity is what buys the reduction, so
we assert monotonicity in a test rather than assuming it.

**A forward greedy for the fee.** Rejected as incorrect, not merely suboptimal; see
above. Two tests pin the failure mode it would have.

**Equal-length segment runs** for the staircase (partition `k` into
`max_segments` near-equal runs). Our first design. Superseded by enumerating all cut
points, which contains the equal-length answer as one of its candidates and is less
arbitrary. Both give the same answer on case 4, where the tier at payment 7 pins the
boundary anyway.

**Hard-coding the shape from the flags.** Rejected: the assignment is explicit that
the shape should be an outcome of the objective, and doing it that way is what makes
the k=10-vs-k=12 and balloon-vs-flat decisions fall out on their own.

**Choosing `first_payment_date` ourselves.** Considered and rejected as
out of scope; see "Known limitations".

---

# Assumptions

1. **`k` is ours; the start date is not.** S5.1 says we choose the count `k`, and that
   payments start at `first_payment_date`. We treat the start as a hard input.
2. **Payments and fees may only land on cadence dates.** Nothing in the spec provides
   a mechanism to debit the account on an off-cadence day.
3. **The fee window is every cadence date at or before the horizon**, including
   fee-only dates after the last creditor payment.
4. **Ledger entries dated on or before `as_of_date` are skipped** -- they are already
   in `current_balance_cents` (S3). Applying them again would double-count.
5. **The balance is checked once per date**, after all of that date's activity, with
   credits before debits (S3). We do not check intra-day orderings among debits.
6. **The drafts are the credits.** For the monthly increment, one ledger credit entry
   is one draft, so two credits on the same date count as two drafts.
7. **The lump sum goes on the earliest modifiable date** (`as_of_date + 1`), not on
   the latest date that would tie. Both give the same minimum `L`; see above.
8. **A leftover balance at the end is fine.** Nothing requires the account to finish
   at zero, and every feasible case here ends with a positive balance.
9. **Unused cadence dates are omitted from the schedule** rather than emitted as
   zero rows. Case 4 uses 10 of its 12 cadence dates, so it prints 10 rows.
10. **Round-half-up everywhere a percentage is applied**, including the two Part 2
    guardrails, per S3.
11. **`max_terms` and `max_payments` bind identically** (`k <= min(...)`), as the
    assignment's own author note says.
12. **A single payment is not a balloon.** The balloon builder requires `k >= 2`.
13. **A structurally impossible offer reports `amount_cents: 0`** with
    `within_guardrail: false` and an explanatory reason, rather than a made-up number.
    The spec does not define this case.

---

# Known edge cases and limitations

**The staircase search generates only the minimal-early fill per cut set.** For a
given set of cut points, earlier runs sit at their floors and the last run absorbs the
remainder. A schedule that would need an early run raised *above* its floor purely to
make a very large final payment affordable is outside the search. The two ends of the
spectrum are covered (`s = 1` is the flat vector, `s = max_segments` is the most
back-loaded), so this only matters for a case whose only feasible schedule sits
strictly between them. Widening it would mean searching the fill as well as the cuts.

**A staircase can be a balloon in all but name.** With `max_segments >= 2` and no
tiers, "steps as late as possible" produces minimum payments followed by one large
final payment -- structurally a balloon, even when `is_ballooning_allowed` is false.
Case 3 shows it concretely: at k=5 the staircase enumeration independently produces
`2500 x4, 20000`, byte-identical to the balloon vector. We accept this reading, and
note the consequence honestly: on this reading `is_ballooning_allowed` mostly affects
*labelling* and the tie-break rather than structure. `shapes.REQUIRE_NON_BALLOON_TAIL`
(default `False`) flips to the stricter reading, requiring the final segment to have
length >= 2 when ballooning is disallowed. It costs nothing on the four provided
cases -- case 4's final segment is 4 payments long.

**Enumeration cost is exponential in the worst case.** The staircase cut sets are
`2^(k-1)` when `max_segments >= k`. Bounded in practice by `k <= min(max_payments,
max_terms, cadence length)`, so at k=12 that is 2048 vectors and the whole case
evaluates in ~3ms. A creditor allowing 36 payments with an unbounded `max_segments`
would need a smarter search.

**Case 2's shortfall is a calendar artifact, not an affordability problem.** The
client has exactly enough money; it is stranded by a 30-day phase mismatch between a
day-1 draft schedule and an end-of-month cadence. We verified that moving
`first_payment_date` to 2026-01-01 -- and *only* to day 1, of all 31 possibilities --
makes the case feasible with no extra funding, because it aligns the cadence with the
draft day and picks up a fifth slot on the horizon itself. In the real world you would
renegotiate the first payment date rather than ask the client for $100. We do not act
on this: the date is an input, and treating it as ours would also contradict the
provided expectation that case 2 is infeasible.

**Ballooning is assumed permitted, not required.** If a creditor sets
`is_ballooning_allowed` expecting a balloon *specifically*, we would still return a
staircase whenever one scored better on the objective. The tie-break means this only
happens when the staircase strictly wins on fee earliness.

---

# Tests

`pytest -q` -- 62 tests in about 3 seconds.

The centrepiece is `tests/helpers.py::assert_valid_schedule`, an **independent
validator** that re-derives the cadence, floors and totals from the raw inputs and
re-simulates the ledger by hand, deliberately *not* reusing `feasibility.simulate`,
so a bug in the engine's simulator cannot hide behind itself. It checks all ten hard
constraints from S5 and is applied to every feasible result.

Coverage beyond the provided expectations:

- **Shapes** -- even, staircase, balloon; the flat-but-not-`even_pays` labelling; the
  balloon shape not being offered when the creditor forbids it.
- **Floors** -- the token cap forcing `base + 1`, tier step-ups, the two combining by
  maximum, and an all-minimum even schedule being rejected by the token cap.
- **Segments** -- `max_segments = 1` forcing flat, a 3-level vector being rejected at
  `max_segments = 2`, and the ±1-cent waiver.
- **Simulation** -- credits before debits on a shared date (constructed so that
  debits-first ordering would fail the whole offer), fixed ledger debits being
  honoured, and balances landing on exactly zero.
- **Dates** -- EOM cadence, mid-month day preservation with clamping, the default
  `first_payment_date`, the horizon cutoff, and a draft arriving after the last
  cadence date.
- **Fee** -- maximal collection on the first payment date; a fee capped by a *later*
  fixed debit rather than by today's balance (the case a forward greedy fails); the
  fee deferring past a squeezed month and finishing later (`[6000, 0, 1500]`);
  fee-only dates carrying no bank fee; infeasibility when the fee cannot finish by
  the horizon.
- **Money** -- half-up vs banker's rounding at exactly `.5`, and float drift.
- **Part 2** -- both minima; their minimality to the cent (`L - 1` must fail);
  monotonicity; both guardrails tripping and passing, including the `40%` branch
  binding instead of the `$100` floor; `num_drafts` counting useless drafts; the two
  minima implying different totals; un-fundable paths.
- **A seeded property test** over randomly generated cases (`tests/helpers.py::random_case`),
  asserting that no later lump-sum placement ever needs less than the earliest one.

---

# Two fixes to the provided scaffolding

1. **`Offer.current_balance_cents` -> `Offer.creditor_balance_cents`.** ASSIGNMENT.md
   S4 renames this field (the old name collided with the client's SDA balance), but
   `models.py` and the shipped case JSONs still used the old spelling. The dataclass
   now uses the new name and the loader accepts either key.
2. **`round()` -> half-up.** `offer_total_cents` and `program_fee_cents` used Python's
   built-in `round()`, which is banker's rounding -- S3 explicitly requires half-up and
   says not to rely on the language default. Both now route through
   `money.pct_of_cents`, which uses `Decimal` with `ROUND_HALF_UP`. `Decimal(str(pct))`
   also avoids binary float drift such as `0.4 * 150000 = 60000.000000000007`.

All source under `feasibility/` is ASCII-only so the package imports regardless of the
reader's locale codec.
