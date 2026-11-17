"""Problem responses shared by the identity routers."""

from __future__ import annotations

from fastapi import status

from mc_server_dashboard_api.http_problem import ProblemException, problem


def password_hasher_unavailable() -> ProblemException:
    # The password could not be checked at all (the hasher itself failed, #3749):
    # a transient server failure, not the uniform 401 of a wrong password.
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE, "password_verification_unavailable"
    )
