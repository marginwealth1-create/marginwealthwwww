"""Unit tests for risk-loop sharding — the `shard_of` partition.

These guard the load-bearing invariant of RISK_SHARDS > 1: every user is
assigned to EXACTLY ONE shard, deterministically and evenly, and single-shard
mode is the identity (so RISK_SHARDS=1 is byte-for-byte the old behaviour).

Pure-function tests — no DB / Redis / event loop needed.
"""

from app.services.risk_enforcer import shard_of


def test_single_shard_is_identity():
    # RISK_SHARDS=1 (and the defensive <=0) must map every user to shard 0.
    for uid in ["a", "b", "6a2595ddc933fd656c60fd40", "", "12345"]:
        assert shard_of(uid, 1) == 0
        assert shard_of(uid, 0) == 0


def test_deterministic_across_calls():
    uid = "6a2595ddc933fd656c60fd40"
    assert shard_of(uid, 4) == shard_of(uid, 4) == shard_of(uid, 4)


def test_result_always_in_range():
    for n in (2, 3, 4, 8, 16):
        for i in range(300):
            s = shard_of(f"user{i}", n)
            assert 0 <= s < n


def test_partition_is_total_and_disjoint():
    # Union of all shard buckets == every user; buckets never overlap.
    users = [f"user{i}" for i in range(500)]
    n = 4
    buckets: dict[int, set[str]] = {k: set() for k in range(n)}
    for u in users:
        buckets[shard_of(u, n)].add(u)
    assert set().union(*buckets.values()) == set(users)
    seen: set[str] = set()
    for k in range(n):
        assert not (buckets[k] & seen)  # disjoint
        seen |= buckets[k]


def test_distribution_roughly_even():
    # sha1 is well-distributed → each shard within ±25% of the mean.
    n = 4
    counts = [0] * n
    total = 4000
    for i in range(total):
        counts[shard_of(f"u{i}", n)] += 1
    mean = total / n
    for c in counts:
        assert 0.75 * mean <= c <= 1.25 * mean
