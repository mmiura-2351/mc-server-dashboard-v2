"""Per-test lifespan management for endpoint ``TestClient`` s (issue #1980).

Endpoint tests used to acquire their client via ``next(_client(...))`` over a
``def _client(...) -> Iterator[TestClient]: with TestClient(app): yield`` helper.
``next()`` returns the client but drops the only reference to the generator, so
CPython finalizes it immediately: ``GeneratorExit`` fires at the ``yield``, the
``with`` exits, and the app's lifespan *shutdown* runs -- all before the test
body issues its first request.

``enter_client`` fixes this by entering the client's context (running lifespan
*startup*) into a per-test :class:`~contextlib.ExitStack` owned by the
``_client_exit_stack`` autouse fixture in ``conftest.py``. The client's
``__exit__`` (lifespan shutdown) then runs when that stack closes at test
teardown -- AFTER the test body -- so requests execute inside the lifespan
window.

The ExitStack is bound as a module global (mirroring the ``_bind_shared_app``
idiom the endpoint-test modules use) so ``_client`` helpers can stay plain
functions that take per-call arguments, rather than pytest fixtures.
"""

from __future__ import annotations

from contextlib import ExitStack

from fastapi.testclient import TestClient

_exit_stack: ExitStack | None = None


def bind_exit_stack(stack: ExitStack | None) -> None:
    """Bind (or clear) the per-test ExitStack :func:`enter_client` enters into.

    Called only by the ``_client_exit_stack`` autouse fixture (``conftest.py``):
    with the stack on entry, ``None`` on teardown.
    """
    global _exit_stack
    _exit_stack = stack


def enter_client(client: TestClient) -> TestClient:
    """Enter ``client``'s context (lifespan startup) into the per-test stack.

    Returns the same client, now live. Its lifespan shutdown runs when the
    per-test stack closes at teardown, so requests run with the lifespan open.
    """
    if _exit_stack is None:
        raise RuntimeError(
            "enter_client() called with no active per-test ExitStack; the "
            "_client_exit_stack autouse fixture (conftest.py) must be active."
        )
    return _exit_stack.enter_context(client)
