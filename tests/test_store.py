"""Shared state, and the guard that refuses to run without it.

The interesting tests here are the ones about `claim` and `incr`. Both exist
because a read-then-write across processes is not the same operation: two
workers can both read "absent" and both act, which is the duplicate reply this
whole module is for.

`RedisStore` is exercised against a fake that behaves like the commands used,
rather than a live server — the contract being tested is ours, not Redis's.
"""

import pytest

from whatsapp_voice.store import (
    MemoryStore,
    RedisStore,
    build_store,
    guard_worker_count,
)


class FakeRedis:
    """Just the five commands RedisStore uses, with the semantics it relies on."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex is not None:
            self.expiries[key] = ex
        return True

    def delete(self, key):
        self.data.pop(key, None)

    def incr(self, key):
        value = int(self.data.get(key, 0)) + 1
        self.data[key] = str(value)
        return value

    def expire(self, key, seconds):
        self.expiries[key] = seconds


@pytest.fixture(params=["memory", "redis"])
def store(request):
    """Every behavioural test runs against both, because the two must agree."""
    if request.param == "memory":
        return MemoryStore()
    return RedisStore("redis://fake", client=FakeRedis())


class TestBasics:
    def test_missing_key_is_none(self, store):
        assert store.get("nope") is None

    def test_set_then_get(self, store):
        store.set("k", {"a": 1})
        assert store.get("k") == {"a": 1}

    def test_delete(self, store):
        store.set("k", 1)
        store.delete("k")
        assert store.get("k") is None

    def test_values_round_trip_unicode(self, store):
        store.set("k", {"name": "रमेश"})
        assert store.get("k")["name"] == "रमेश"


class TestClaim:
    def test_first_caller_wins(self, store):
        assert store.claim("id", ttl_s=60) is True

    def test_second_caller_loses(self, store):
        """This is deduplication. A read-then-write would let both callers
        through, which is the duplicate reply it exists to prevent."""
        store.claim("id", ttl_s=60)
        assert store.claim("id", ttl_s=60) is False

    def test_different_keys_do_not_collide(self, store):
        store.claim("a", ttl_s=60)
        assert store.claim("b", ttl_s=60) is True

    def test_an_expired_claim_can_be_retaken(self):
        store = MemoryStore()
        store.claim("id", ttl_s=-1)
        assert store.claim("id", ttl_s=60) is True


class TestIncr:
    def test_counts_up(self, store):
        assert [store.incr("k", ttl_s=60) for _ in range(3)] == [1, 2, 3]

    def test_separate_keys_count_separately(self, store):
        store.incr("a", ttl_s=60)
        assert store.incr("b", ttl_s=60) == 1

    def test_only_the_first_write_sets_the_expiry(self):
        """Otherwise every request extends the window and it never closes."""
        redis = FakeRedis()
        store = RedisStore("redis://fake", client=redis)

        store.incr("k", ttl_s=60)
        redis.expiries["wa-voice:k"] = 60
        store.incr("k", ttl_s=999)

        assert redis.expiries["wa-voice:k"] == 60

    def test_the_count_resets_after_expiry(self):
        store = MemoryStore()
        store.incr("k", ttl_s=-1)
        assert store.incr("k", ttl_s=60) == 1


class TestExpiry:
    def test_a_value_past_its_ttl_is_gone(self):
        store = MemoryStore()
        store.set("k", 1, ttl_s=-1)
        assert store.get("k") is None

    def test_no_ttl_means_it_stays(self):
        store = MemoryStore()
        store.set("k", 1)
        assert store.get("k") == 1


class TestRedisSpecifics:
    def test_keys_are_namespaced(self):
        """So this can share a Redis with anything else."""
        redis = FakeRedis()
        RedisStore("redis://fake", namespace="proj", client=redis).set("k", 1)
        assert "proj:k" in redis.data

    def test_values_are_json_not_pickle(self):
        """Someone who can write to Redis should not thereby be able to execute
        code in the payment process."""
        redis = FakeRedis()
        RedisStore("redis://fake", client=redis).set("k", {"a": 1})
        assert redis.data["wa-voice:k"] == '{"a": 1}'

    def test_an_unreadable_value_reads_as_missing(self):
        """Written by an older version, or by something else in the namespace.
        Treating it as absent beats crashing a payment."""
        redis = FakeRedis()
        redis.data["wa-voice:k"] = "not json at all"
        assert RedisStore("redis://fake", client=redis).get("k") is None


class TestBuildStore:
    def test_no_url_gives_memory(self):
        assert isinstance(build_store(None), MemoryStore)
        assert isinstance(build_store(""), MemoryStore)

    def test_a_url_gives_redis(self):
        assert isinstance(
            RedisStore("redis://fake", client=FakeRedis()), RedisStore
        )


class TestWorkerGuard:
    def test_one_worker_on_memory_is_fine(self):
        guard_worker_count(1, MemoryStore())

    def test_several_workers_on_memory_is_refused(self):
        """Silently, this produces duplicate replies and lost PINs — while
        every health check passes."""
        with pytest.raises(RuntimeError, match="REDIS_URL"):
            guard_worker_count(4, MemoryStore())

    def test_the_error_says_what_breaks_and_how_to_fix_it(self):
        with pytest.raises(RuntimeError) as caught:
            guard_worker_count(4, MemoryStore())

        message = str(caught.value)
        assert "two replies" in message
        assert "one worker" in message

    def test_several_workers_on_redis_is_fine(self):
        guard_worker_count(4, RedisStore("redis://fake", client=FakeRedis()))


class TestCredentialsAreNotLogged:
    def test_the_password_is_masked(self, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            RedisStore("redis://user:hunter2@redis.internal:6379", client=FakeRedis())

        assert "hunter2" not in caplog.text
        assert "redis.internal" in caplog.text
