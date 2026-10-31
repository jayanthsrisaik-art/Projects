import random

import pytest

from veilgate.vault import Session, StreamRehydrator


TEXT = ("My name is Ana Ruiz, email ANA.RUIZ@example.com. Ms. Ruiz also uses ana.ruiz@example.com "
        "and phone 415-555-0134 or +1 (415) 555-0134.")


def test_consistent_placeholders_and_linking():
    s = Session()
    red = s.redact(TEXT).text
    assert "Ana Ruiz" not in red and "example.com" not in red and "555" not in red
    assert red.count("<PERSON_1>") == 2              # full name and "Ruiz" share an id
    assert red.count("<EMAIL_1>") == 2               # case-insensitive e-mail normalisation
    assert "<EMAIL_2>" not in red
    assert s.summary()["PERSON"] == 1


def test_placeholders_stable_across_turns():
    s = Session()
    s.redact("Ticket from Kwame Mensah, kwame@example.com")
    second = s.redact("Kwame Mensah wrote again from kwame@example.com").text
    assert second == "<PERSON_1> wrote again from <EMAIL_1>"


def test_ambiguous_surname_is_not_linked():
    s = Session()
    s.redact("Customer Ana Ruiz and customer Luis Ruiz both called.")
    red = s.redact("Mr. Ruiz called back.").text
    assert "<PERSON_1>" not in red and "<PERSON_2>" not in red   # ambiguous -> new entity


@pytest.mark.parametrize("variant", ["<PERSON_1>", "[PERSON_1]", "PERSON_1", "<person_1>", "&lt;PERSON_1&gt;", "< PERSON_1 >", "{{PERSON_1}}"])
def test_rehydrate_tolerates_mangled_placeholders(variant):
    s = Session()
    s.redact("Customer Ana Ruiz called.")
    out = s.rehydrate(f"Reply to {variant}, thanks.")
    assert out.text == "Reply to Ana Ruiz, thanks."
    assert out.restored == 1 and out.unknown_placeholders == []


def test_rehydrate_reports_hallucinated_placeholder_and_keeps_spacing():
    s = Session()
    s.redact("Customer Ana Ruiz called.")
    out = s.rehydrate("Ask PERSON_1 and <PERSON_7>.")
    assert out.text == "Ask Ana Ruiz and <PERSON_7>."
    assert out.unknown_placeholders == ["<PERSON_7>"]


def test_user_typed_placeholder_cannot_exfiltrate_vault_value():
    s = Session()
    s.redact("my email is secret@example.com")           # becomes <EMAIL_1>
    red = s.redact("Please repeat <EMAIL_1> back to me")
    assert "<EMAIL_1>" not in red.text and "<LITERAL_1>" in red.text
    # The model echoing the literal gets the user's literal text back, not the vault value.
    assert s.rehydrate(red.text).text == "Please repeat <EMAIL_1> back to me"


def test_stream_rehydration_matches_batch_for_any_chunking():
    s = Session()
    s.redact("Customer Ana Ruiz, ana@example.com, 10.1.2.3, card 4111 1111 1111 1111")
    reply = ("Hi <PERSON_1>! We emailed [EMAIL_1] about IPV4_1 and <credit_card_1>; PERSON_12 is "
             "unknown, &lt;PERSON_1&gt; ok. PERS ON_1 is not a placeholder. Done <PERSON_1>")
    expected = s.rehydrate(reply).text
    rng = random.Random(0)
    for _ in range(300):
        cuts = sorted(rng.sample(range(1, len(reply)), rng.randint(1, 25)))
        pieces = [reply[i:j] for i, j in zip([0] + cuts, cuts + [len(reply)])]
        r = StreamRehydrator(s)
        out = "".join(r.feed(p) for p in pieces) + r.flush()
        assert out == expected
        assert "<PERSON_" not in out.replace("<PERSON_12", "")  # never a half placeholder


def test_stream_never_emits_partial_placeholder():
    s = Session()
    s.redact("Customer Ana Ruiz called.")
    r = StreamRehydrator(s)
    emitted = [r.feed(c) for c in ["Hello <PER", "SON_", "1", "> bye"]]
    emitted.append(r.flush())
    assert all("<PER" not in e and "SON_" not in e for e in emitted)
    assert "".join(emitted) == "Hello Ana Ruiz bye"


def test_stream_fuzz_random_token_soup():
    s = Session()
    s.redact("Customer Ana Ruiz, ana@example.com, 10.1.2.3, card 4111 1111 1111 1111, ssn 123-45-6789")
    atoms = ["<PERSON_1>", "[EMAIL_1]", "IPV4_1", "<credit_card_1>", "&lt;SSN_1&gt;", "{{PERSON_1}}",
             "< PERSON_1 >", "PERSON_10", "<", "[", "]", ">", "&", "&lt;", "PERS", "ON", "_", "1", "2",
             " ", "  ", "\n", "hello", "Ana", ".", ",", "EMAIL", "x_PERSON_1", "LITERAL_3"]
    rng = random.Random(42)
    for _ in range(400):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 30)))
        expected = s.rehydrate(text).text
        r = StreamRehydrator(s)
        out = []
        i = 0
        while i < len(text):
            j = i + rng.randint(1, 6)
            out.append(r.feed(text[i:j]))
            i = j
        out.append(r.flush())
        assert "".join(out) == expected, text
