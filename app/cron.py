"""Small durable cron evaluator for five-field local schedules."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _field(text, minimum, maximum):
    values = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start, end = map(int, base.split("-", 1))
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron field out of range")
        values.update(range(start, end + 1, step))
    return values


def next_due(expression, timezone_name, after):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression needs five fields")
    allowed = [_field(field, *bounds) for field, bounds in zip(fields, RANGES)]
    zone = ZoneInfo(timezone_name)
    instant = datetime.fromisoformat(after)
    if instant.tzinfo is None:
        raise ValueError("after must have a timezone")
    instant = instant.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60 * 5):
        local = instant.astimezone(zone)
        weekday = (local.weekday() + 1) % 7
        day_matches = local.day in allowed[2]
        weekday_matches = weekday in allowed[4]
        if fields[2] != "*" and fields[4] != "*":
            calendar_matches = day_matches or weekday_matches
        else:
            calendar_matches = day_matches and weekday_matches
        if (local.minute in allowed[0] and local.hour in allowed[1]
                and calendar_matches and local.month in allowed[3]):
            return instant.isoformat()
        instant += timedelta(minutes=1)
    raise ValueError("cron expression has no occurrence in five years")
