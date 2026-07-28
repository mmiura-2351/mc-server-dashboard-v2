"""Parse a single-range HTTP ``Range`` request header (RFC 9110 Section 14.2).

A backup archive is multi-GB, so an interrupted download must be resumable
rather than restartable (issue #2372). Resumption is a ``Range: bytes=N-``
request, which the download edge answers with ``206`` over a ranged read of the
stored bytes.

**One range only.** A multi-range request (``bytes=0-99,200-299``) would need a
``multipart/byteranges`` body for a gain no download client asks for, so it is
served as if ``Range`` were absent — explicitly allowed by RFC 9110 Section
14.2 ("An origin server MUST ignore a Range header field that contains a range
unit it does not understand" and, for a valid but unhandled set, may simply
respond with the full representation).

The three outcomes a caller must handle:

* ``None`` — serve the whole representation (``200``). Either no ``Range`` was
  sent, or it was unusable: an unknown unit, a malformed spec, an inverted
  ``last < first``, or a multi-range set. RFC 9110 requires an invalid
  ``Range`` to be ignored, not rejected.
* a :class:`ByteRange` — serve exactly those inclusive byte positions (``206``).
* :class:`RangeNotSatisfiableError` — the first position lies at or past the end
  of the representation, or a zero-length suffix was asked for (``416``).
"""

from __future__ import annotations

from typing import NamedTuple


class RangeNotSatisfiableError(Exception):
    """The requested range lies entirely outside the representation (``416``)."""


class ByteRange(NamedTuple):
    """An inclusive byte-position range, resolved against a known size."""

    start: int
    end: int

    @property
    def length(self) -> int:
        """The byte count the range covers — both endpoints included."""

        return self.end - self.start + 1


def parse_byte_range(header: str | None, *, size: int) -> ByteRange | None:
    """Resolve a ``Range`` header value against a representation of ``size`` bytes.

    Returns ``None`` when the whole representation is to be served, a
    :class:`ByteRange` clamped to ``size``, or raises
    :class:`RangeNotSatisfiableError`.
    """

    if header is None:
        return None
    unit, sep, spec = header.strip().partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        # A multi-range set: answered with the full representation (see above).
        return None
    first, sep, last = spec.partition("-")
    if not sep:
        return None
    first, last = first.strip(), last.strip()
    if not first:
        return _suffix_range(last, size)
    if not first.isdigit() or (last and not last.isdigit()):
        # ``isdigit`` (not ``int``) so the grammar's bare DIGITs are what is
        # accepted: ``int`` would also take ``+5`` and unicode digits.
        return None
    start = int(first)
    if last and int(last) < start:
        # An inverted spec is invalid, not unsatisfiable: ignore it (RFC 9110
        # Section 14.1.1) rather than answering 416. Only an explicit last
        # position can be inverted — an open-ended ``bytes=N-`` derives its end
        # from the size, and a start past the end is unsatisfiable, not invalid.
        return None
    if start >= size:
        raise RangeNotSatisfiableError(f"first byte {start} of {size}")
    end = size - 1 if not last else min(int(last), size - 1)
    return ByteRange(start, end)


def _suffix_range(last: str, size: int) -> ByteRange | None:
    """Resolve ``bytes=-N`` — the final ``N`` bytes of the representation."""

    if not last.isdigit():
        return None
    suffix = int(last)
    if suffix == 0 or size == 0:
        # Zero bytes can never be satisfied, and neither can any range over an
        # empty representation (RFC 9110 Section 14.1.2).
        raise RangeNotSatisfiableError(f"suffix {suffix} of {size}")
    return ByteRange(max(0, size - suffix), size - 1)
