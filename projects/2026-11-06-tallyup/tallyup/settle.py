"""Settle-up plans: who pays whom so every balance reaches zero.

Balances are net positions in minor units: positive = the group owes this
member, negative = this member owes the group. They always sum to zero.

* ``greedy_plan`` - the common approach: repeatedly match the largest debtor
  with the largest creditor. Never needs more than ``n - 1`` transfers, but can
  miss shortcuts.
* ``optimal_plan`` - the minimum number of transfers. With ``k`` non-zero
  balances the minimum is ``k - g``, where ``g`` is the largest number of
  disjoint zero-sum groups the members can be partitioned into (each group of
  size ``s`` settles internally in ``s - 1`` transfers and no fewer). ``g`` is
  found with an O(2^k * k) subset DP; above ``MAX_EXACT`` members the function
  falls back to greedy and says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

MAX_EXACT = 16


@dataclass(frozen=True)
class Transfer:
    from_member: int
    to_member: int
    amount: int


@dataclass(frozen=True)
class Plan:
    method: str               # "greedy" | "optimal" | "greedy_fallback"
    transfers: list[Transfer]


def _check(balances: Mapping[int, int]) -> dict[int, int]:
    if sum(balances.values()) != 0:
        raise ValueError("balances must sum to zero")
    return {m: b for m, b in balances.items() if b != 0}


def _greedy(bal: dict[int, int]) -> list[Transfer]:
    bal = dict(bal)
    out: list[Transfer] = []
    while bal:
        # Largest creditor / debtor; ties broken by member id for determinism.
        cred = max((m for m in bal if bal[m] > 0), key=lambda m: (bal[m], -m))
        debt = min((m for m in bal if bal[m] < 0), key=lambda m: (bal[m], m))
        amt = min(bal[cred], -bal[debt])
        out.append(Transfer(debt, cred, amt))
        bal[cred] -= amt
        bal[debt] += amt
        for m in (cred, debt):
            if bal[m] == 0:
                del bal[m]
    return out


def greedy_plan(balances: Mapping[int, int]) -> Plan:
    return Plan("greedy", _greedy(_check(balances)))


def max_zero_sum_groups(values: list[int]) -> tuple[int, list[list[int]]]:
    """Partition indices of ``values`` (sum 0, all non-zero) into the maximum
    number of zero-sum groups. Returns (count, groups of indices)."""
    k = len(values)
    full = (1 << k) - 1
    total = [0] * (1 << k)
    for mask in range(1, 1 << k):
        low = mask & -mask
        total[mask] = total[mask ^ low] + values[low.bit_length() - 1]
    # dp[mask]: best number of complete zero-sum groups in any ordering of mask.
    dp = [0] * (1 << k)
    choice = [0] * (1 << k)
    for mask in range(1, 1 << k):
        best, arg = -1, 0
        m = mask
        while m:
            low = m & -m
            v = dp[mask ^ low]
            if v > best:
                best, arg = v, low
            m ^= low
        dp[mask] = best + (1 if total[mask] == 0 else 0)
        choice[mask] = arg
    # Walk back: removing elements one at a time; every time the remaining set
    # sums to zero, the elements removed since the previous cut form a group.
    groups: list[list[int]] = []
    current: list[int] = []
    mask = full
    while mask:
        if total[mask] == 0 and current:
            groups.append(current)
            current = []
        low = choice[mask]
        current.append(low.bit_length() - 1)
        mask ^= low
    if current:
        groups.append(current)
    return dp[full], groups


def optimal_plan(balances: Mapping[int, int]) -> Plan:
    bal = _check(balances)
    if len(bal) > MAX_EXACT:
        return Plan("greedy_fallback", _greedy(bal))
    members = sorted(bal)
    count, groups = max_zero_sum_groups([bal[m] for m in members])
    transfers: list[Transfer] = []
    for g in groups:
        transfers.extend(_greedy({members[i]: bal[members[i]] for i in g}))
    transfers.sort(key=lambda t: (t.from_member, t.to_member))
    assert len(transfers) == len(bal) - count
    return Plan("optimal", transfers)


def apply(balances: Mapping[int, int], transfers: list[Transfer]) -> dict[int, int]:
    """Balances after the transfers are paid (debtor's balance rises)."""
    out = dict(balances)
    for t in transfers:
        if t.amount <= 0 or t.from_member == t.to_member:
            raise ValueError(f"invalid transfer {t}")
        out[t.from_member] = out.get(t.from_member, 0) + t.amount
        out[t.to_member] = out.get(t.to_member, 0) - t.amount
    return out
