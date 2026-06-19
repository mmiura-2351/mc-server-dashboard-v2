"""Unit tests for the version-range satisfaction predicate (issue #1293).

Exhaustive over both dialects: Fabric/Quilt semver predicates (comparisons,
tilde, caret, x-ranges, AND/OR combinations), Forge/NeoForge Maven intervals,
and the tolerant any/empty/unparseable fallback.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.application.version_range import version_satisfies


class TestAnyAndFallback:
    @pytest.mark.parametrize("loader", ["fabric", "quilt", "forge", "neoforge"])
    @pytest.mark.parametrize("spec", ["", "  ", "*"])
    def test_empty_or_star_is_any(self, loader: str, spec: str) -> None:
        assert version_satisfies("1.2.3", spec, loader) is True

    def test_unparseable_semver_is_any(self) -> None:
        # Garbage that no predicate branch accepts -> tolerant True.
        assert version_satisfies("1.0.0", ">=not.a.version", "fabric") is True

    def test_unparseable_maven_is_any(self) -> None:
        assert version_satisfies("1.0.0", "[broken", "forge") is True

    def test_never_raises_on_weird_input(self) -> None:
        assert version_satisfies("", "~", "fabric") is True
        assert version_satisfies("1", "^", "fabric") is True


class TestSemverComparisons:
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            ("1.20.1", ">=1.20", True),
            ("1.19.4", ">=1.20", False),
            ("1.20.0", ">=1.20", True),
            ("1.20.0", ">1.20", False),
            ("1.20.1", ">1.20", True),
            ("1.19.9", "<1.20", True),
            ("1.20.0", "<1.20", False),
            ("1.20.0", "<=1.20", True),
            ("1.20.1", "<=1.20", False),
            ("1.20", "=1.20", True),
            ("1.20.0", "1.20", True),
            ("1.21", "1.20", False),
        ],
    )
    def test_comparison_predicates(
        self, version: str, spec: str, expected: bool
    ) -> None:
        assert version_satisfies(version, spec, "fabric") is expected


class TestSemverTilde:
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            ("1.2.3", "~1.2.3", True),
            ("1.2.9", "~1.2.3", True),
            ("1.3.0", "~1.2.3", False),
            ("1.2.0", "~1.2", True),
            ("1.2.9", "~1.2", True),
            ("1.3.0", "~1.2", False),
            ("1.1.0", "~1.2", False),
        ],
    )
    def test_tilde(self, version: str, spec: str, expected: bool) -> None:
        assert version_satisfies(version, spec, "fabric") is expected


class TestSemverCaret:
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            ("1.2.3", "^1.2.3", True),
            ("1.9.0", "^1.2.3", True),
            ("2.0.0", "^1.2.3", False),
            ("1.0.0", "^1", True),
            ("1.9.9", "^1", True),
            ("2.0.0", "^1", False),
            ("0.2.3", "^0.2.3", True),
            ("0.3.0", "^0.2.3", False),
        ],
    )
    def test_caret(self, version: str, spec: str, expected: bool) -> None:
        assert version_satisfies(version, spec, "fabric") is expected


class TestSemverXRange:
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            ("1.2.0", "1.2.x", True),
            ("1.2.9", "1.2.x", True),
            ("1.3.0", "1.2.x", False),
            ("1.2.0", "1.2.*", True),
            ("1.5.0", "1.x", True),
            ("2.0.0", "1.x", False),
        ],
    )
    def test_x_range(self, version: str, spec: str, expected: bool) -> None:
        assert version_satisfies(version, spec, "fabric") is expected


class TestSemverCombinations:
    def test_and_with_comma(self) -> None:
        assert version_satisfies("1.20.1", ">=1.20,<1.21", "fabric") is True
        assert version_satisfies("1.21.0", ">=1.20,<1.21", "fabric") is False

    def test_and_with_space(self) -> None:
        assert version_satisfies("1.20.1", ">=1.20 <1.21", "fabric") is True
        assert version_satisfies("1.19.0", ">=1.20 <1.21", "fabric") is False

    def test_or_with_double_pipe(self) -> None:
        # The manifest parser joins a list-valued range with `` || ``.
        assert version_satisfies("1.18.0", ">=1.20 || 1.18.x", "fabric") is True
        assert version_satisfies("1.20.1", ">=1.20 || 1.18.x", "fabric") is True
        assert version_satisfies("1.19.0", ">=1.20 || 1.18.x", "fabric") is False

    def test_quilt_uses_semver_dialect(self) -> None:
        assert version_satisfies("1.5.0", ">=1.0", "quilt") is True
        assert version_satisfies("0.9.0", ">=1.0", "quilt") is False


class TestMavenIntervals:
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            ("1.5", "[1,2)", True),
            ("1.0", "[1,2)", True),
            ("2.0", "[1,2)", False),
            ("0.9", "[1,2)", False),
            ("1.0", "(1,2)", False),
            ("2.0", "(1,2]", True),
            ("5.0", "[1,)", True),
            ("0.5", "[1,)", False),
            ("1.0", "(,2]", True),
            ("2.0", "(,2]", True),
            ("2.1", "(,2]", False),
            ("1.5", "[1.5]", True),
            ("1.6", "[1.5]", False),
        ],
    )
    def test_intervals(self, version: str, spec: str, expected: bool) -> None:
        assert version_satisfies(version, spec, "forge") is expected

    def test_bare_version_is_exact(self) -> None:
        assert version_satisfies("36.2.0", "36.2.0", "forge") is True
        assert version_satisfies("36.2.1", "36.2.0", "forge") is False

    def test_union_of_intervals(self) -> None:
        # Maven comma-joins intervals as a union (OR).
        assert version_satisfies("1.5", "[1,2),[3,4)", "neoforge") is True
        assert version_satisfies("3.5", "[1,2),[3,4)", "neoforge") is True
        assert version_satisfies("2.5", "[1,2),[3,4)", "neoforge") is False

    def test_neoforge_uses_maven_dialect(self) -> None:
        assert version_satisfies("20.4.100", "[20.4,)", "neoforge") is True
        assert version_satisfies("20.3.0", "[20.4,)", "neoforge") is False
