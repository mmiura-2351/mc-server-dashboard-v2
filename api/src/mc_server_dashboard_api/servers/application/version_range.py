"""Decide whether a concrete version satisfies a dependency's range (issue #1307).

A pure, stdlib-only predicate the validator uses to turn a *present-but-wrong-
version* dependency into a distinct finding, rather than treating any present id
as satisfied. No I/O, no DB, no network.

Three range dialects, selected by the depending plugin's **loader**:

* **Fabric / Quilt** -- semver-style predicates. A range is one or more
  comma- or space-separated predicates ANDed together; ``||`` separates OR
  alternatives (the manifest parser emits ``||`` for a list-valued range). Each
  predicate is one of:

  ===========  =================================================================
  ``*`` / ``""``  any version
  ``1.2.3``    exact (a bare version; ``=1.2.3`` is accepted too)
  ``>=`` ``<=`` ``>`` ``<``  comparison against the predicate version
  ``~1.2``     tilde: same major+minor, patch may rise (``>=1.2.0,<1.3.0``)
  ``^1.2``     caret: same leftmost-non-zero, lower components may rise
  ``1.2.x``    x-range: any version whose given components match (also ``1.2.*``)
  ===========  =================================================================

* **Forge / NeoForge** -- Maven version-interval notation. ``[`` / ``]`` are
  inclusive bounds, ``(`` / ``)`` exclusive, an empty side is unbounded:
  ``[1.0,2.0)``, ``[1.0,)``, ``(,2.0]``, ``[1.5]`` (an exact pin). A bare version
  with no brackets is treated as exact. Comma-separated intervals are ORed
  (Maven's union semantics).

* **Paper** -- a Bukkit ``api-version`` minimum floor compared at
  major.minor granularity: ``1.21`` is satisfied by any ``1.21.x`` or newer
  major.minor, not by ``1.20.4``. (Bukkit ``api-version`` is the oldest server a
  plugin runs on, not an exact version.) Paper plugins carry no dependency range
  (the parser emits ``""``), so this dialect is only ever exercised by the MC
  compatibility check.

Tolerance is the contract: an empty range, ``*``, or anything that does not
parse in the loader's dialect is treated as **any** (returns ``True``). The
validator must never crash on a malformed manifest, and "I can't evaluate this"
errs toward not-a-finding (the human still sees the raw range string).

Versions are compared component-wise: each is split into dot/dash-separated
parts, numeric parts compared as integers and the rest lexically, with a
missing component treated as ``0`` so ``1.2`` == ``1.2.0``. This is not a full
semver pre-release ordering -- it is the pragmatic comparison the loaders'
real-world ranges need.
"""

from __future__ import annotations

import re

# Loaders whose ranges use Maven interval notation; everything else (fabric,
# quilt, unknown) uses the semver-predicate dialect. Paper plugins never carry a
# real dependency range (the parser emits ``""``), so for dependencies they fall
# straight through to the any-fallback regardless of which branch handles them.
_MAVEN_LOADERS = frozenset({"forge", "neoforge"})

# Loaders whose MC-version constraint is a Bukkit ``api-version`` floor; see
# :func:`_paper_satisfies`.
_PAPER_LOADERS = frozenset({"paper"})

_COMPARATORS = ("<=", ">=", "<", ">")


def version_satisfies(version: str, range_spec: str, loader: str) -> bool:
    """Return whether ``version`` satisfies ``range_spec`` under ``loader``.

    ``loader`` is the depending plugin's loader (``fabric``/``quilt`` -> semver
    predicates, ``forge``/``neoforge`` -> Maven intervals). An empty/any range, or
    any range that cannot be parsed in the loader's dialect, returns ``True`` (the
    documented tolerant fallback). Never raises.
    """

    spec = range_spec.strip()
    if not spec or spec == "*":
        return True
    try:
        if loader in _PAPER_LOADERS:
            return _paper_satisfies(version.strip(), spec)
        if loader in _MAVEN_LOADERS:
            return _maven_satisfies(version.strip(), spec)
        return _semver_satisfies(version.strip(), spec)
    except (ValueError, IndexError):
        # Unparseable in the loader's dialect -> treat as "any".
        return True


# --- version comparison ----------------------------------------------------


_PART_SPLIT = re.compile(r"[.\-]")


def _components(version: str) -> list[object]:
    """Split a version into comparable components (ints where numeric).

    Semver ``+build`` metadata is dropped first: per semver it MUST NOT affect
    precedence, and real Fabric mod versions carry it (e.g. ``0.92.2+1.20.1``).
    """

    version = version.split("+", 1)[0]
    parts: list[object] = []
    for raw in _PART_SPLIT.split(version):
        if raw == "":
            continue
        parts.append(int(raw) if raw.isdigit() else raw)
    return parts


def compare_versions(a: str, b: str) -> int:
    """Three-way compare two version strings (-1/0/1), for newest-wins selection.

    A public wrapper over the same component-wise comparison the range dialects
    use internally (issue #1309): the auto-resolver picks the newest compatible
    Modrinth version, so it needs a total order over version numbers.
    """

    return _compare(a, b)


def _compare(a: str, b: str) -> int:
    """Three-way compare two versions component-wise (missing component == 0).

    Numeric vs. numeric compares as integers; any string component sorts before
    a numeric one at the same position (a pre-release-ish ordering that is good
    enough for the loaders' real ranges). Returns -1/0/1.
    """

    ac = _components(a)
    bc = _components(b)
    for i in range(max(len(ac), len(bc))):
        x = ac[i] if i < len(ac) else 0
        y = bc[i] if i < len(bc) else 0
        if x == y:
            continue
        x_int = isinstance(x, int)
        y_int = isinstance(y, int)
        if x_int and y_int:
            return -1 if x < y else 1  # type: ignore[operator]
        if x_int != y_int:
            # A numeric component outranks a string one at the same position.
            return 1 if x_int else -1
        return -1 if str(x) < str(y) else 1
    return 0


# --- Fabric / Quilt semver predicates --------------------------------------


def _semver_satisfies(version: str, spec: str) -> bool:
    """Evaluate a Fabric/Quilt range: ``||``-separated OR of ANDed predicates."""

    for alternative in spec.split("||"):
        predicates = [p for p in re.split(r"[,\s]+", alternative.strip()) if p]
        if not predicates:
            # An empty alternative (e.g. trailing ``||``) means "any".
            return True
        if all(_semver_predicate(version, p) for p in predicates):
            return True
    return False


def _semver_predicate(version: str, predicate: str) -> bool:
    if predicate in ("*", ""):
        return True
    for comp in _COMPARATORS:
        if predicate.startswith(comp):
            return _compare_op(comp, _compare(version, predicate[len(comp) :].strip()))
    if predicate.startswith("~"):
        return _tilde(version, predicate[1:].strip())
    if predicate.startswith("^"):
        return _caret(version, predicate[1:].strip())
    target = predicate[1:].strip() if predicate.startswith("=") else predicate
    if _is_x_range(target):
        return _x_range(version, target)
    return _compare(version, target) == 0


def _compare_op(comp: str, cmp: int) -> bool:
    if comp == ">=":
        return cmp >= 0
    if comp == "<=":
        return cmp <= 0
    if comp == ">":
        return cmp > 0
    return cmp < 0  # "<"


def _is_x_range(target: str) -> bool:
    return any(part in ("x", "X", "*") for part in target.split("."))


def _x_range(version: str, target: str) -> bool:
    """Match an ``x``-range: every non-wildcard component must equal the version."""

    vt = version.split(".")
    for i, part in enumerate(target.split(".")):
        if part in ("x", "X", "*"):
            continue
        if i >= len(vt) or vt[i] != part:
            return False
    return True


def _tilde(version: str, target: str) -> bool:
    """``~1.2.3`` allows patch-level changes; ``~1.2``/``~1`` the minor/major."""

    parts = [p for p in target.split(".") if p != ""]
    if not parts:
        raise ValueError("empty tilde target")
    lower = target
    if len(parts) >= 2:
        upper = f"{parts[0]}.{int(parts[1]) + 1}"
    else:
        upper = f"{int(parts[0]) + 1}"
    return _compare(version, lower) >= 0 and _compare(version, upper) < 0


def _caret(version: str, target: str) -> bool:
    """``^1.2.3`` allows changes that keep the leftmost non-zero component."""

    parts = [int(p) for p in target.split(".") if p != ""]
    if not parts:
        raise ValueError("empty caret target")
    upper_parts = list(parts)
    for i, value in enumerate(parts):
        if value != 0 or i == len(parts) - 1:
            upper_parts = parts[: i + 1]
            upper_parts[i] += 1
            break
    upper = ".".join(str(p) for p in upper_parts)
    return _compare(version, target) >= 0 and _compare(version, upper) < 0


# --- Paper api-version floor -------------------------------------------------


def _paper_satisfies(version: str, spec: str) -> bool:
    """Evaluate a Bukkit ``api-version`` as a major.minor minimum floor.

    A plugin's ``api-version: 1.21`` is the *oldest* server it runs on, compared
    at major.minor granularity: a server at any ``1.21.x`` (or any newer
    major.minor) satisfies it; ``1.20.4`` does not. This is distinct from the
    fabric/forge range dialects, where the MC constraint is an exact version or
    an interval. ``spec`` and ``version`` are truncated to (major, minor) and the
    server's pair must be ``>=`` the floor's.
    """

    return _major_minor(version) >= _major_minor(spec)


def _major_minor(version: str) -> tuple[int, int]:
    """The (major, minor) of a version as ints; a missing minor is ``0``."""

    parts = _components(version)
    major = parts[0] if parts and isinstance(parts[0], int) else 0
    minor = parts[1] if len(parts) > 1 and isinstance(parts[1], int) else 0
    return (major, minor)


# --- Forge / NeoForge Maven intervals --------------------------------------


def _maven_satisfies(version: str, spec: str) -> bool:
    """Evaluate Maven interval notation; comma-joined intervals are ORed."""

    intervals = _split_maven_intervals(spec)
    if not intervals:
        return True
    any_parsed = False
    for token in intervals:
        try:
            if _maven_interval(version, token):
                return True
            any_parsed = True
        except (ValueError, IndexError):
            continue
    if not any_parsed:
        # Every token was unparseable -> re-raise so the outer fallback fires.
        raise ValueError(f"no parseable maven interval in: {spec!r}")
    return False


def _split_maven_intervals(spec: str) -> list[str]:
    """Split on commas that separate intervals, not commas inside brackets."""

    tokens: list[str] = []
    depth = 0
    current = ""
    for ch in spec:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            tokens.append(current)
            current = ""
            continue
        current += ch
    tokens.append(current)
    return [t.strip() for t in tokens if t.strip()]


def _maven_interval(version: str, token: str) -> bool:
    if token[0] in "[(" or token[-1] in "])":
        if not (token[0] in "[(" and token[-1] in "])"):
            # An unbalanced bracket (``[broken``) is unparseable -> any.
            raise ValueError(f"unbalanced maven interval: {token!r}")
    else:
        # A bare version with no brackets is an exact match.
        return _compare(version, token) == 0

    lower_inclusive = token[0] == "["
    upper_inclusive = token[-1] == "]"
    inner = token[1:-1]
    if "," not in inner:
        # ``[1.5]`` -- a single bracketed value is an exact pin.
        bound = inner.strip()
        return bool(bound) and _compare(version, bound) == 0

    low, high = (part.strip() for part in inner.split(",", 1))
    if low:
        cmp = _compare(version, low)
        if cmp < 0 or (cmp == 0 and not lower_inclusive):
            return False
    if high:
        cmp = _compare(version, high)
        if cmp > 0 or (cmp == 0 and not upper_inclusive):
            return False
    return True
