"""Debt simplification: turn net balances into as few payments as possible.

Input is {user_id: net_minor}: positive means the group owes them, negative
means they owe the group. The values always sum to zero.

* `greedy_transfers` is the classic min-cash-flow heuristic: repeatedly make
  the biggest debtor pay the biggest creditor. Each payment zeroes at least one
  person, so it never needs more than (people with a balance - 1) payments.
* Finding the true minimum is NP-hard (it's partitioning the people into the
  most zero-sum groups; each group of k people then needs k-1 payments). For
  small groups we solve that exactly with a bitmask DP, and fall back to the
  greedy result for big ones. Real friend groups fit in the exact path.
"""
from __future__ import annotations

import heapq

EXACT_LIMIT = 14  # 2^14 masks * 14 is a few hundred thousand steps


def greedy_transfers(net: dict[int, int]) -> list[tuple[int, int, int]]:
    """Returns [(from_user, to_user, amount_minor)]. Deterministic: ties go to the lower user id."""
    creditors = [(-v, u) for u, v in net.items() if v > 0]
    debtors = [(v, u) for u, v in net.items() if v < 0]  # v is negative: most negative first
    heapq.heapify(creditors)
    heapq.heapify(debtors)
    out: list[tuple[int, int, int]] = []
    while creditors and debtors:
        neg_credit, c = heapq.heappop(creditors)
        debt, d = heapq.heappop(debtors)
        credit = -neg_credit
        pay = min(credit, -debt)
        out.append((d, c, pay))
        if credit > pay:
            heapq.heappush(creditors, (-(credit - pay), c))
        if -debt > pay:
            heapq.heappush(debtors, (debt + pay, d))
    return out


def _zero_sum_groups(people: list[int], balances: list[int]) -> list[list[int]]:
    """Partition into the maximum number of zero-sum groups (exact, bitmask DP).

    dp[mask] = most zero-sum prefixes achievable when the people in `mask` are
    added one at a time in the best order. Walking the chosen order back out
    and cutting wherever the running sum hits zero yields the groups.
    """
    n = len(people)
    full = (1 << n) - 1
    sums = [0] * (full + 1)
    for mask in range(1, full + 1):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + balances[low.bit_length() - 1]
    dp = [0] * (full + 1)
    last = [0] * (full + 1)
    for mask in range(1, full + 1):
        best, pick = -1, 0
        for i in range(n):
            if mask >> i & 1 and dp[mask ^ (1 << i)] > best:
                best, pick = dp[mask ^ (1 << i)], i
        dp[mask] = best + (1 if sums[mask] == 0 else 0)
        last[mask] = pick
    order: list[int] = []
    mask = full
    while mask:
        order.append(last[mask])
        mask ^= 1 << last[mask]
    order.reverse()
    groups: list[list[int]] = []
    current: list[int] = []
    running = 0
    for i in order:
        current.append(people[i])
        running += balances[i]
        if running == 0:
            groups.append(current)
            current = []
    return groups


def simplify(net: dict[int, int]) -> list[tuple[int, int, int]]:
    """Fewest payments that settle everyone (exact for small groups), sorted for stable output."""
    nonzero = {u: v for u, v in net.items() if v != 0}
    if sum(nonzero.values()) != 0:
        raise ValueError("balances must sum to zero")
    if not nonzero:
        return []
    people = sorted(nonzero)
    greedy = greedy_transfers(nonzero)
    if len(people) <= EXACT_LIMIT:
        groups = _zero_sum_groups(people, [nonzero[u] for u in people])
        exact: list[tuple[int, int, int]] = []
        for g in groups:
            exact += greedy_transfers({u: nonzero[u] for u in g})
        if len(exact) <= len(greedy):
            greedy = exact
    return sorted(greedy, key=lambda t: (t[0], t[1], t[2]))
