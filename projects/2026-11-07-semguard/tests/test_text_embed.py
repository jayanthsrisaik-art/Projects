import numpy as np

from semguard.embed import HashingEmbedder
from semguard.text import normalize, stem


def test_normalize_contractions_ids_and_thousands():
    assert normalize("I can't open ORD-4521!!") == "i can not open ord-4521"
    assert normalize("Won't pay 1,000 USD") == "will not pay 1000 usd"
    assert normalize("mail a.b@x.com, v2.3") == "mail a.b@x.com v2.3"


def test_stem_is_light():
    assert stem("files") == "file" and stem("reading") == "read" and stem("go") == "go"


def test_embedding_is_deterministic_and_unit_norm():
    e = HashingEmbedder()
    a, b = e.embed("How do I reset my password?"), e.embed("How do I reset my password?")
    assert np.array_equal(a, b)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    assert float(np.linalg.norm(e.embed("!!!"))) == 0.0


def test_paraphrase_is_closer_than_unrelated():
    e = HashingEmbedder()
    q = e.embed("how do I reset my password")
    assert q @ e.embed("help me reset my password please") > q @ e.embed("what is the per diem for tokyo") + 0.5
