"""
query_limits.py - One rule for how many rows any query may return.

Every SELECT in this project ends in ``LIMIT``, and every row count that can
come from outside the code passes through :func:`clamp_limit` first, so no
request - however it is written - can ask for more than :data:`MAX_LIMIT` rows,
or for a zero or negative count.
"""

from __future__ import annotations

import re

MAX_LIMIT = 100      # the most rows any single query may return
DEFAULT_LIMIT = 20   # what a caller gets when it does not ask for a number

_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]+")
_MAX_DIGITS = 6      # longer numbers are far outside 1..MAX_LIMIT; no need to convert them


class LimitError(ValueError):
    """A requested row limit was not a whole number."""


def clamp_limit(value: object, default: int = DEFAULT_LIMIT) -> int:
    """Turn a requested row limit into an int between 1 and MAX_LIMIT.

    ``None`` or a blank string means "use the default".  Anything that is not a
    plain whole number - ``"abc"``, ``"5.5"``, ``"1e9"``, ``"10; DROP TABLE x"`` -
    raises LimitError instead of being guessed at.  Whole numbers outside
    1..MAX_LIMIT are pulled to the nearest end, so ``"0"`` gives 1 and
    ``"1000000"`` gives MAX_LIMIT.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise LimitError("limit must be a whole number")
    text = str(value).strip()
    if not _WHOLE_NUMBER.fullmatch(text):
        raise LimitError("limit must be a whole number")
    if len(text.lstrip("+-")) > _MAX_DIGITS:
        return 1 if text.startswith("-") else MAX_LIMIT
    return max(1, min(MAX_LIMIT, int(text)))
