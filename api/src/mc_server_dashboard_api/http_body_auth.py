"""The route's credential check, before its body is read (issue #3365).

FastAPI's route handler takes a declared body first -- it buffers the JSON, or
spools the multipart form to disk -- and solves the route's dependencies, the
authentication among them, only afterwards: the dependencies and the body feed
one call to the endpoint, and the body is parsed to build it. A dependency that
needs nothing but a header still waits for it. So the 401 of an upload route came
after up to 513 MiB had been written to disk for a caller with no credential.

:func:`authorize_before_body` closes that order. The body-limit middleware
(:mod:`mc_server_dashboard_api.http_body_limit`) calls it at a request's first
body read, when routing has already put the matched route in the scope. It
solves, with FastAPI's own resolver, those of the route's dependencies that
read a credential -- any dependency built on a FastAPI security scheme, however
deep -- and lets what they raise be the answer:

* it is the route's own check, not a copy of it: the same callables, the same
  ``dependency_overrides``, so the 401 / 403 / 404 is the one the route gave
  before, byte for byte, and a new route is covered by declaring its
  dependency as every route does;
* it is not the decision: the handler solves the same dependencies again once
  the body is read, and that result is what the endpoint receives;
* a route with no such dependency (login, registration, the Worker-credentialled
  data plane, which checks its credential before it streams) is untouched.

Solving a dependency a second time costs its lookups a second time; that is paid
only by requests whose body is read at all.
"""

from __future__ import annotations

import logging

from fastapi import status
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import Scope

from mc_server_dashboard_api.http_problem import INTERNAL_ERROR_REASON, problem

_logger = logging.getLogger(__name__)


def _reads_a_credential(dependant: Dependant) -> bool:
    return isinstance(dependant.call, SecurityBase) or any(
        _reads_a_credential(sub) for sub in dependant.dependencies
    )


async def authorize_before_body(scope: Scope) -> None:
    """Raise the refusal of the matched route's credential checks, if any.

    Raised into a body read, which the framework lets only an ``HTTPException``
    out of (anything else becomes its own 400), so:

    * a validation error -- a malformed path id, found once the caller is
      authenticated -- is left for the dependency to report after the body, as
      the 422 it always was;
    * any other failure is logged and answered 500 here, with the body unread:
      a check that could not be made is not a check that passed.
    """

    route = scope.get("route")
    if not isinstance(route, APIRoute):
        return
    checks = [sub for sub in route.dependant.dependencies if _reads_a_credential(sub)]
    if not checks:
        return
    # No ``receive``: a check cannot read the body it stands in front of.
    request = Request(scope)
    try:
        await solve_dependencies(
            request=request,
            dependant=Dependant(dependencies=checks, path=route.path_format),
            dependency_overrides_provider=route.dependency_overrides_provider,
            async_exit_stack=scope["fastapi_inner_astack"],
            embed_body_fields=False,
        )
    except HTTPException:
        raise
    except RequestValidationError:
        return
    except Exception:
        _logger.exception(
            "unhandled exception authorizing %s %s", request.method, request.url.path
        )
        raise problem(
            status.HTTP_500_INTERNAL_SERVER_ERROR, INTERNAL_ERROR_REASON
        ) from None
