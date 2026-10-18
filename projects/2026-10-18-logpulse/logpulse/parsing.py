"""Parse lines of the form: 2026-01-01T00:00:03Z svc=api level=INFO msg="..."."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .templates import mine_template, template_id

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) svc=(?P<svc>[\w\-]+) '
    r'level=(?P<level>[A-Z]+) msg="(?P<msg>.*)"$'
)
_LATENCY = re.compile(r"in (\d+)ms")


@dataclass(frozen=True)
class LogRecord:
    ts: int
    service: str
    level: str
    msg: str
    template_id: str
    latency_ms: float | None


def format_line(ts: int, service: str, level: str, msg: str) -> str:
    stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'{stamp} svc={service} level={level} msg="{msg}"'


def parse_line(line: str) -> LogRecord | None:
    """Return a record, or None for malformed lines / unknown levels."""
    m = _LINE.match(line.strip())
    if not m or m["level"] not in LEVELS:
        return None
    ts = int(datetime.strptime(m["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    lat = _LATENCY.search(m["msg"])
    return LogRecord(ts, m["svc"], m["level"], m["msg"], template_id(mine_template(m["msg"])),
                     float(lat.group(1)) if lat else None)
