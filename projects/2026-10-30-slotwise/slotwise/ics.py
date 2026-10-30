"""Minimal RFC 5545 VEVENT export for a booking (UTC times, CRLF, folding)."""
from __future__ import annotations

from .availability import EventType
from .store import Booking


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545 section 3.1)."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode()
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode())
            chunk = b""
        chunk += b
    parts.append(chunk.decode())
    return "\r\n ".join(parts)


def _stamp(dt) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def booking_ics(booking: Booking, et: EventType) -> str:
    status = "CONFIRMED" if booking.status == "confirmed" else "CANCELLED"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//slotwise//EN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{booking.id}@slotwise.local",
        f"DTSTAMP:{booking.created_at.replace('-', '').replace(':', '')}",
        f"DTSTART:{_stamp(booking.start)}",
        f"DTEND:{_stamp(booking.end)}",
        f"SUMMARY:{_escape(et.title + ' with ' + booking.invitee_name)}",
        f"DESCRIPTION:{_escape(f'Host timezone: {et.host_tz}. Booked by {booking.invitee_email}.')}",
        f"STATUS:{status}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
