"""Deterministic log template mining by masking variable tokens."""
import hashlib
import re

# Any token fragment containing a digit is treated as a variable (ids, ports, durations, IPs).
_VAR = re.compile(r"[\w.:\-]*\d[\w.:\-]*")
_WS = re.compile(r"\s+")


def mine_template(msg: str) -> str:
    return _WS.sub(" ", _VAR.sub("<*>", msg)).strip()


def template_id(template: str) -> str:
    return hashlib.sha1(template.encode()).hexdigest()[:10]
