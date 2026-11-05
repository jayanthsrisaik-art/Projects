"""winstream: event-time windowed aggregation with watermarks, allowed
lateness, session merging with retractions and exactly-once SQLite checkpoints."""

from .engine import Operator
from .model import Event, WindowSpec, parse_event, parse_window

__all__ = ["Event", "Operator", "WindowSpec", "parse_event", "parse_window"]
__version__ = "0.1.0"
