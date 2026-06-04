"""Unit tests for the CheckHealth use case, with the DatabasePing Port faked.

Per NFR-TEST-1 the application layer is exercised against a fake Port, no real
database. The use case reports overall health from the database's reachability.
"""

from mc_server_dashboard_api.core.application.check_health import CheckHealth
from mc_server_dashboard_api.core.domain.health import DatabasePing, HealthReport


class _FakePing(DatabasePing):
    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable
        self.calls = 0

    async def is_reachable(self) -> bool:
        self.calls += 1
        return self._reachable


async def test_reports_ok_when_database_reachable() -> None:
    ping = _FakePing(reachable=True)
    report = await CheckHealth(database=ping)()
    assert report == HealthReport(ok=True, database_reachable=True)
    assert ping.calls == 1


async def test_reports_degraded_when_database_unreachable() -> None:
    ping = _FakePing(reachable=False)
    report = await CheckHealth(database=ping)()
    assert report == HealthReport(ok=False, database_reachable=False)
