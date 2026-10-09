"""The instance's clock: one time zone for every "today" and every displayed time.

It comes from KHALA_TIMEZONE (default UTC), not from the server's own zone, so a container and a VPS running the
same settings date records and show times the same way.
"""
import datetime
from zoneinfo import ZoneInfo

_zone = datetime.timezone.utc


def zone_from(name):
    """A tzinfo for an IANA name such as America/Toronto; raises for unknown names."""
    if not name or name.strip().upper() == "UTC":
        return datetime.timezone.utc
    return ZoneInfo(name.strip())


def configure(name):
    global _zone
    _zone = zone_from(name)


def now():
    return datetime.datetime.now(_zone)


def today():
    return now().date()


def local(ts):
    return datetime.datetime.fromtimestamp(float(ts), _zone)
