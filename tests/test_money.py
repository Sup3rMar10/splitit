import random
from decimal import Decimal

import pytest

from app.money import Rates, allocate, exponent, format_minor
from app.simplify import EXACT_LIMIT, greedy_transfers, simplify


@pytest.fixture(scope="module")
def rates():
    return Rates.load("data/rates.json")


def test_format_minor_is_integer_only():
    assert format_minor(12345, "USD") == "123.45"
    assert format_minor(5, "USD") == "0.05"
    assert format_minor(100, "JPY") == "100"
    assert format_minor(1, "KWD") == "0.001"
    assert format_minor(-250, "EUR") == "-2.50"
    assert exponent("JPY") == 0 and exponent("USD") == 2 and exponent("KWD") == 3


def test_allocate_always_sums_to_total_and_stays_within_one_unit_of_exact():
    rng = random.Random(1234)
    for _ in range(3000):
        n = rng.randint(1, 9)
        weights = [rng.randint(0, 50) for _ in range(n)]
        if sum(weights) == 0:
            weights[0] = 1
        total = rng.randint(0, 10**9)
        parts = allocate(total, weights, rotation=rng.randint(0, 20))
        assert sum(parts) == total
        for part, w in zip(parts, weights):
            exact = total * w / sum(weights)
            assert abs(part - exact) < 1  # largest remainder never strays a full unit
            if w == 0:
                assert part == 0


def test_allocate_is_deterministic_and_known_values():
    assert allocate(10000, [1, 1, 1]) == [3334, 3333, 3333]
    assert allocate(10000, [1, 1, 1]) == allocate(10000, [1, 1, 1])
    assert allocate(100, [1, 1, 1], rotation=1) == [33, 34, 33]  # the extra cent rotates
    assert allocate(100, [1, 1, 1], rotation=2) == [33, 33, 34]
    assert allocate(1, [1, 1, 1]) == [1, 0, 0]
    assert allocate(1000, [1, 2]) == [333, 667]
    assert allocate(0, [3, 4]) == [0, 0]


def test_allocate_rejects_bad_input():
    for args in (([], 5), ([0, 0], 5), ([1, -1], 5)):
        with pytest.raises(ValueError):
            allocate(args[1], args[0])
    with pytest.raises(ValueError):
        allocate(-1, [1])


def test_equal_split_rotation_spreads_the_extra_cent_fairly():
    extra = [0, 0, 0]
    for seq in range(1, 301):
        for i, part in enumerate(allocate(100, [1, 1, 1], rotation=seq)):
            extra[i] += part - 33
    assert extra == [100, 100, 100]


def test_rates_load_as_decimal_not_float(rates):
    assert isinstance(rates.rates["INR"], Decimal)
    assert rates.rates["INR"] == Decimal("83.25")
    assert rates.supports("EUR") and not rates.supports("XXX")


def test_convert_same_currency_is_identity(rates):
    assert rates.convert(12345, "USD", "USD") == (12345, "1")


def test_convert_rounds_half_up_and_respects_exponents(rates):
    # 10.01 USD -> 833.3325 INR -> 83333.25 paise -> 83333
    assert rates.convert(1001, "USD", "INR")[0] == 83333
    # 1000 JPY (no minor unit) -> 6.6889 USD -> 669 cents
    assert rates.convert(1000, "JPY", "USD")[0] == 669
    # 10.00 USD -> 1495 JPY (whole yen)
    assert rates.convert(1000, "USD", "JPY")[0] == 1495
    # 3-decimal currency: 10.00 USD -> 3.070 KWD
    assert rates.convert(1000, "USD", "KWD")[0] == 3070
    # exact half rounds up: 0.5 of a minor unit
    r = Rates("USD", {"USD": Decimal(1), "XXX": Decimal("0.5")})
    assert r.convert(1, "USD", "XXX")[0] == 1
    assert r.convert(3, "USD", "XXX")[0] == 2  # 1.5 -> 2


def test_convert_roundtrip_is_close(rates):
    rng = random.Random(7)
    for _ in range(300):
        amount = rng.randint(100, 10**7)
        there, _ = rates.convert(amount, "USD", "INR")
        back, _ = rates.convert(there, "INR", "USD")
        assert abs(back - amount) <= 1


# --- simplification ---------------------------------------------------------------


def _random_net(rng, n, spread=10_000):
    vals = [rng.randint(-spread, spread) for _ in range(n - 1)]
    vals.append(-sum(vals))
    return {i + 1: v for i, v in enumerate(vals)}


def _apply(net, transfers):
    out = dict(net)
    for frm, to, amt in transfers:
        assert amt > 0 and frm != to
        out[frm] += amt
        out[to] -= amt
    return out


def _min_transfers_bruteforce(net):
    """Oracle: n - (max number of disjoint zero-sum groups), by exhaustive search."""
    people = [v for v in net.values() if v != 0]
    n = len(people)
    sums = {}
    for mask in range(1 << n):
        sums[mask] = sum(people[i] for i in range(n) if mask >> i & 1)
    from functools import lru_cache

    @lru_cache(None)
    def best(mask):
        if mask == 0:
            return 0
        low = mask & -mask
        top = 0
        sub = mask
        while sub:
            if sub & low and sums[sub] == 0:
                top = max(top, 1 + best(mask ^ sub))
            sub = (sub - 1) & mask
        return top

    return n - best((1 << n) - 1) if n else 0


def test_simplify_settles_everyone_and_never_needs_more_than_n_minus_1():
    rng = random.Random(99)
    for _ in range(400):
        net = _random_net(rng, rng.randint(2, 12))
        transfers = simplify(net)
        assert all(v == 0 for v in _apply(net, transfers).values())
        assert len(transfers) <= max(0, len([v for v in net.values() if v]) - 1)


def test_simplify_matches_bruteforce_optimum_on_small_groups():
    rng = random.Random(5)
    for _ in range(250):
        n = rng.randint(2, 8)
        # small values make zero-sum subsets likely, which is where greedy goes wrong
        net = _random_net(rng, n, spread=6)
        assert len(simplify(net)) == _min_transfers_bruteforce(net), net


def test_greedy_alone_can_be_beaten_and_simplify_beats_it():
    net = {1: 7, 2: 5, 3: -4, 4: -3, 5: -5}  # {7,-4,-3} and {5,-5} are separate groups: 3 payments suffice
    assert len(greedy_transfers(net)) == 4
    assert len(simplify(net)) == 3


def test_simplify_is_deterministic_and_handles_edge_cases():
    assert simplify({}) == []
    assert simplify({1: 0, 2: 0}) == []
    assert simplify({1: 500, 2: -500}) == [(2, 1, 500)]
    net = {1: 100, 2: 100, 3: -100, 4: -100}
    assert simplify(net) == simplify(dict(reversed(list(net.items()))))
    with pytest.raises(ValueError):
        simplify({1: 5, 2: -4})


def test_simplify_large_group_falls_back_to_greedy_but_still_settles():
    rng = random.Random(3)
    net = _random_net(rng, EXACT_LIMIT + 6)
    transfers = simplify(net)
    assert all(v == 0 for v in _apply(net, transfers).values())
    assert len(transfers) <= len(net) - 1
