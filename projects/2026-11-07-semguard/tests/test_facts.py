import json

import pytest

from semguard.facts import Lexicon, conflict, extract_facts

LEX = Lexicon.default()


def f(t):
    return extract_facts(t, LEX)


def test_numbers_ids_and_number_words():
    assert f("refund order 4521").literals == {("num", "4521")}
    assert f("refund ORD-4521").literals == {("id", "ord-4521")}
    assert f("take five days").literals == f("take 5 days").literals == {("num", "5")}
    assert f("policy for 6-hour flights").literals == {("num", "6")}
    assert f("turn on 2fa").literals == frozenset()  # single-digit tokens like 2fa/s3 are words
    assert f("price is 3.50").literals == {("num", "3.5")}


def test_emails_urls_quotes_not_double_counted():
    lits = f('email bob@x.com about "Q3 report" https://x.io/a?b=1.').literals
    assert lits == {("email", "bob@x.com"), ("quoted", "q3 report"), ("url", "https://x.io/a?b=1")}


def test_negation_and_no_abbreviation():
    assert f("why can't I log in").negated
    assert f("I cannot sign in").negated
    assert not f("please refund order no. 4521").negated
    assert f("there is no invoice").negated


def test_polarity_and_inflections():
    assert conflict(f("how do I enable 2fa"), f("disabling 2fa")) .startswith("polarity 'toggle'")
    assert conflict(f("turn on dark mode"), f("turn off dark mode")).startswith("polarity")
    assert conflict(f("export my data"), f("exporting my data")) is None
    assert conflict(f("how do I export data"), f("how do I import data")) is not None


def test_terms_with_aliases():
    assert conflict(f("read csv in python"), f("read csv in py")) is None
    assert conflict(f("install on mac"), f("install on macos")) is None
    assert "language differs" in conflict(f("read csv in python"), f("read csv in java"))
    assert f("where do I go to enable it").terms == frozenset()  # 'go' alone is not the Go language
    # one side silent on a group is allowed (paraphrase may omit it)
    assert conflict(f("read csv in python"), f("how do I read a csv")) is None


def test_volatile():
    assert f("usd to eur rate today").volatile and f("current status").volatile
    assert not f("how do I reset my password").volatile


def test_guard_subset_disables_checks():
    a, b = f("refund order 1"), f("refund order 2")
    assert conflict(a, b) is not None
    assert conflict(a, b, guards=("negation",)) is None


def test_custom_lexicon(tmp_path):
    p = tmp_path / "lex.json"
    p.write_text(json.dumps({"terms": {"region": {"eu": ["eu", "europe"], "us": ["us-east"]}}}))
    lex = Lexicon.from_file(str(p))
    assert conflict(extract_facts("deploy to europe", lex), extract_facts("deploy to us-east", lex))


@pytest.mark.parametrize("ex", json.load(open("samples/check_examples.json")))
def test_documented_check_examples(ex):
    from semguard.__main__ import main
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["check", ex["cached"], ex["query"]])
    assert json.loads(buf.getvalue())["verdict"] == ex["expect"], ex["why"]
