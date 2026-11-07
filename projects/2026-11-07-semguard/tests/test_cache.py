from semguard.cache import CacheConfig, SemanticCache, scope_key


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_exact_semantic_and_stats():
    c = SemanticCache()
    c.store("How do I reset my password?", "A")
    assert c.lookup("how do i reset my password").reason.startswith("exact")
    lk = c.lookup("help me reset my password please")
    assert lk.hit and lk.entry["response"] == "A" and lk.score > 0.9
    assert c.lookup("what is the per diem for tokyo").status == "miss"
    s = c.stats()
    assert (s["entries"], s["lookups"], s["hit"], s["miss"]) == (1, 3, 2, 1)


def test_guard_rejects_top_candidate_but_serves_compatible_lower_one():
    c = SemanticCache(config=CacheConfig(threshold=0.3))
    c.store("I want to turn off two-factor authentication on my account", "wrong")
    c.store("how can I activate two-factor authentication", "right")
    lk = c.lookup("I want to turn on two-factor authentication on my account")
    # the 'turn off' entry is far more similar textually but is vetoed; the paraphrase is served
    assert lk.hit and lk.entry["response"] == "right"
    assert lk.rejected and "polarity 'toggle'" in lk.rejected[0]["reason"]
    assert lk.rejected[0]["score"] > lk.score


def test_semantic_off_is_exact_only():
    c = SemanticCache(config=CacheConfig(semantic=False, guards=()))
    c.store("how do I reset my password", "A")
    assert c.lookup("How do I reset my password??").hit
    assert not c.lookup("help me reset my password please").hit


def test_volatile_bypass_and_not_stored():
    c = SemanticCache()
    assert c.store("usd to eur rate today", "1.08") is None
    assert c.lookup("usd to eur rate today").status == "bypass"
    assert c.stats()["entries"] == 0


def test_scopes_are_isolated():
    c = SemanticCache()
    s1, s2 = scope_key("m", "system A"), scope_key("m", "system B")
    assert s1 != s2 and s1 == scope_key("m", "system A")
    c.store("how do I reset my password", "A", scope=s1)
    assert c.lookup("how do I reset my password", scope=s1).hit
    assert not c.lookup("how do I reset my password", scope=s2).hit


def test_ttl_expiry():
    clock = Clock()
    c = SemanticCache(config=CacheConfig(ttl_seconds=60), clock=clock)
    c.store("how do I reset my password", "A")
    clock.t += 30
    assert c.lookup("help me reset my password please").hit
    clock.t += 31
    lk = c.lookup("help me reset my password please")
    assert lk.status == "miss" and lk.rejected[0]["reason"] == "expired"
    assert not c.lookup("how do I reset my password").hit  # exact path honours TTL too
    assert c.purge(expired_only=True) == 1


def test_lru_eviction():
    clock = Clock()
    c = SemanticCache(config=CacheConfig(max_entries_per_scope=2), clock=clock)
    c.store("what does error 404 mean", "a"); clock.t += 1
    c.store("what is the per diem for tokyo", "b"); clock.t += 1
    assert c.lookup("what does error 404 mean").hit; clock.t += 1   # refresh a
    c.store("how do I reset my password", "c")                       # evicts b
    assert c.stats()["entries"] == 2
    assert not c.lookup("what is the per diem for tokyo").hit
    assert c.lookup("what does error 404 mean").hit


def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "c.db")
    c = SemanticCache(db)
    c.store("how do I reset my password", "A")
    c.close()
    c2 = SemanticCache(db)
    lk = c2.lookup("help me reset my password please")
    assert lk.hit and lk.entry["response"] == "A"
