"""Problem responses shared by the identity routers."""

from __future__ import annotations

from fastapi import status

from mc_server_dashboard_api.http_problem import ProblemException, problem


def password_hasher_unavailable() -> ProblemException:
    # The password hashing service is unavailable: the hasher itself failed to
    # check a password (#3749) or to hash a new one (#3762). A transient server
    # failure, not the uniform 401 of a wrong password nor a generic 500.
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE, "password_verification_unavailable"
    )
