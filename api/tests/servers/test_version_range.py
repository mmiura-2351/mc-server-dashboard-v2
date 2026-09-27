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


class TestSemver:
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            pytest.param("1.20.1", ">=1.20", True, id="greater-equal-newer"),
            pytest.param("1.19.4", ">=1.20", False, id="greater-equal-older"),
            pytest.param("1.20.0", ">=1.20", True, id="greater-equal-boundary"),
            pytest.param("1.20.0", ">1.20", False, id="greater-than-boundary"),
            pytest.param("1.20.1", ">1.20", True, id="greater-than-newer"),
            pytest.param("1.19.9", "<1.20", True, id="less-than-older"),
            pytest.param("1.20.0", "<1.20", False, id="less-than-boundary"),
            pytest.param("1.20.0", "<=1.20", True, id="less-equal-boundary"),
            pytest.param("1.20.1", "<=1.20", False, id="less-equal-newer"),
            pytest.param("1.20", "=1.20", True, id="explicit-equality"),
            pytest.param("1.20.0", "1.20", True, id="implicit-equality-zero-patch"),
            pytest.param("1.21", "1.20", False, id="implicit-equality-different-minor"),
            pytest.param("1.2.3", "~1.2.3", True, id="tilde-exact"),
            pytest.param("1.2.9", "~1.2.3", True, id="tilde-patch"),
            pytest.param("1.3.0", "~1.2.3", False, id="tilde-next-minor"),
            pytest.param("1.2.0", "~1.2", True, id="tilde-short-boundary"),
            pytest.param("1.2.9", "~1.2", True, id="tilde-short-patch"),
            pytest.param("1.3.0", "~1.2", False, id="tilde-short-next-minor"),
            pytest.param("1.1.0", "~1.2", False, id="tilde-short-previous-minor"),
            pytest.param("1.2.3", "^1.2.3", True, id="caret-exact"),
            pytest.param("1.9.0", "^1.2.3", True, id="caret-same-major"),
            pytest.param("2.0.0", "^1.2.3", False, id="caret-next-major"),
            pytest.param("1.0.0", "^1", True, id="caret-short-boundary"),
            pytest.param("1.9.9", "^1", True, id="caret-short-same-major"),
            pytest.param("2.0.0", "^1", False, id="caret-short-next-major"),
            pytest.param("0.2.3", "^0.2.3", True, id="caret-zero-exact"),
            pytest.param("0.3.0", "^0.2.3", False, id="caret-zero-next-minor"),
            pytest.param("1.2.0", "1.2.x", True, id="x-range-boundary"),
            pytest.param("1.2.9", "1.2.x", True, id="x-range-patch"),
            pytest.param("1.3.0", "1.2.x", False, id="x-range-next-minor"),
            pytest.param("1.2.0", "1.2.*", True, id="star-range-boundary"),
            pytest.param("1.5.0", "1.x", True, id="x-range-major"),
            pytest.param("2.0.0", "1.x", False, id="x-range-next-major"),
            # Semver ``+build`` metadata must not affect precedence (issue #1293).
            pytest.param(
                "1.0.0+build", ">=1.0.0", True, id="build-metadata-comparison"
            ),
            pytest.param(
                "0.92.2+1.20.1", "=0.92.2", True, id="build-metadata-equality"
            ),
            pytest.param(
                "0.92.2+1.20.1", "0.92.2", True, id="build-metadata-implicit-equality"
            ),
            pytest.param(
                "0.92.2+1.20.1", ">=0.92.0", True, id="build-metadata-fabric-range"
            ),
            pytest.param(
                "0.11.2+build.123", ">=0.11.0", True, id="build-metadata-generic-range"
            ),
            pytest.param(
                "0.91.0+x", ">=0.92.0", False, id="build-metadata-out-of-range"
            ),
        ],
    )
    def test_predicates(self, version: str, spec: str, expected: bool) -> None:
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

    def test_malformed_token_in_union_does_not_widen_range(self) -> None:
        # A garbage token should be ignored, not widen the range to "any".
        # 2.5 is NOT in [1,2), and [garbage is unparseable -> False.
        assert version_satisfies("2.5", "[1,2),[garbage", "forge") is False

    def test_valid_token_matches_despite_malformed_sibling(self) -> None:
        # 1.5 IS in [1,2); the garbage token is ignored.
        assert version_satisfies("1.5", "[1,2),[garbage", "forge") is True

    def test_all_tokens_malformed_falls_back_to_any(self) -> None:
        # Every token is unparseable -> tolerant fallback returns True.
        assert version_satisfies("1.0", "[garbage", "forge") is True

    def test_normal_maven_range_still_works(self) -> None:
        assert version_satisfies("1.0", "[1,2)", "forge") is True
        assert version_satisfies("3.0", "[1,2)", "forge") is False


class TestPaperApiVersionFloor:
    # A Bukkit ``api-version`` is a major.minor minimum floor, not an exact
    # version: any patch of the declared minor (or any newer minor) satisfies it.
    @pytest.mark.parametrize(
        ("version", "spec", "expected"),
        [
            ("1.21.1", "1.21", True),
            ("1.21.0", "1.21", True),
            ("1.21", "1.21", True),
            ("1.20.4", "1.21", False),
            ("1.22", "1.21", True),
            ("1.22.3", "1.21", True),
            ("2.0", "1.21", True),
        ],
    )
    def test_floor(self, version: str, spec: str, expected: bool) -> None:
        assert version_satisfies(version, spec, "paper") is expected
