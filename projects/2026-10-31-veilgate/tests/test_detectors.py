import pytest

from veilgate.detectors import detect, iban_ok, luhn_ok, ssn_ok


def types(text):
    return [(s.type, s.text) for s in detect(text)]


def test_luhn_and_iban_validators():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert iban_ok("DE89 3704 0044 0532 0130 00")
    assert not iban_ok("DE88 3704 0044 0532 0130 00")
    assert ssn_ok("123", "45", "6789")
    assert not ssn_ok("666", "45", "6789") and not ssn_ok("900", "45", "6789") and not ssn_ok("123", "00", "6789")


@pytest.mark.parametrize("text,expected", [
    ("mail me at jo.doe+x@mail.example.org.", ("EMAIL", "jo.doe+x@mail.example.org")),
    ("call (415) 555-0134 today", ("PHONE", "(415) 555-0134")),
    ("phone: 4155550134", ("PHONE", "4155550134")),
    ("+44 20 7946 0958 is the office", ("PHONE", "+44 20 7946 0958")),
    ("card 4111-1111-1111-1111 declined", ("CREDIT_CARD", "4111-1111-1111-1111")),
    ("ssn 123-45-6789", ("SSN", "123-45-6789")),
    ("iban GB82WEST12345698765432", ("IBAN", "GB82WEST12345698765432")),
    ("from 192.168.10.4 at noon", ("IPV4", "192.168.10.4")),
    ("ship to 42 Old Mill Road. Thanks", ("ADDRESS", "42 Old Mill Road")),
])
def test_structured_positives(text, expected):
    assert expected in types(text)


@pytest.mark.parametrize("text", [
    "Order #4155550134 shipped",                  # labelled id, not a phone
    "invoice 4155550134 is overdue",
    "test card 4111 1111 1111 1112 failed",       # fails Luhn
    "rejected 000-12-3456 and 666-12-3456",       # invalid SSN areas
    "upgraded to version 10.2.3.4 yesterday",     # version, not an IP
    "address 300.1.1.1 is not valid",              # octet > 255
    "We met on Monday in Paris about the Visa plan.",
])
def test_hard_negatives_are_not_redacted(text):
    assert types(text) == []


def test_person_context_gazetteer_and_propagation():
    text = "Customer Kwame Mensah called. Mensah wants a refund. Ms. Ruiz agreed. Thanks, Priya."
    found = types(text)
    assert ("PERSON", "Kwame Mensah") in found          # context cue, name not in gazetteer
    assert ("PERSON", "Mensah") in found                # propagated from the full name
    assert ("PERSON", "Ruiz") in found                  # honorific
    assert ("PERSON", "Priya") in found                 # gazetteer / sign-off
    assert ("PERSON", "Customer Kwame Mensah") not in found


def test_overlap_resolution_prefers_validated_types():
    # The e-mail contains a name-like token; e-mail must win as one span.
    assert types("contact anna.smith@example.com") == [("EMAIL", "anna.smith@example.com")]
