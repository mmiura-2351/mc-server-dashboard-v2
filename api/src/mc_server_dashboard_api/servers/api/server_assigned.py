"""The 409 ``server_assigned`` every route answers a held server with (#3852).

A stop keeps its Worker assigned until the final snapshot settles and the server
is released. Until then a delete, every at-rest write to the working set, a manual
backup and an export refuse the server (:class:`ServerAssignedError`, issue
#3998). The hold can last until the reconciler's release, so this is not the
retry-soon ``server_busy``, and the detail says what to do. One builder, so every
route says it the same way.
"""

from __future__ import annotations

from fastapi import status

from mc_server_dashboard_api.http_problem import ProblemException, problem

SERVER_ASSIGNED_DETAIL = (
    "The server is still assigned to a Worker. Wait for it to be released, then "
    "try again."
)


def server_assigned() -> ProblemException:
    return problem(
        status.HTTP_409_CONFLICT,
        "server_assigned",
        extensions={"detail": SERVER_ASSIGNED_DETAIL},
    )
