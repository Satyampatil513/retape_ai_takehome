"""Entry point: evaluate one offer against one client's escrow account.

The work is split across:
  * ``shapes``   -- cadence, floors, and legal creditor-payment vectors
  * ``simulate`` -- date-by-date ledger walk + optimal (earliest) fee placement
  * ``solver``   -- enumerate candidates, rank them by fee earliness

The output dataclasses live in ``results`` and are re-exported here so
``from feasibility.engine import Result`` keeps working.
"""

from __future__ import annotations

from feasibility.models import Client, CreditorRules, Offer
from feasibility.results import (  # noqa: F401  (re-exported)
    AdditionalFunds,
    FundsOption,
    Result,
    ScheduleRow,
)
from feasibility.solver import solve


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification.

    Return a Result with feasible=True and a schedule when the offer fits, or
    feasible=False with additional_funds (minimum lump sum AND minimum monthly
    increment) when it does not.
    """
    outcome = solve(client, offer, rules)
    if outcome.solution is not None:
        return Result(
            feasible=True,
            pay_shape_used=outcome.solution.shape,
            schedule=outcome.solution.rows,
            additional_funds=None,
        )

    # Part 2 (minimum additional funds) -- next phase.
    return Result(feasible=False, pay_shape_used=None, schedule=None, additional_funds=None)
