"""Transaction harness shared by repository contract suites (#3135).

The contract cases depend only on domain repository Ports.  This small harness
abstracts the transaction boundary that legitimately differs between the fast
in-memory fakes and the PostgreSQL adapters: a fake commit is a no-op, while the
SQL arm commits its enclosing UnitOfWork (and therefore surfaces deferred
constraint violations there).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Generic, TypeVar

RepositoryT = TypeVar("RepositoryT")


@dataclass(frozen=True)
class RepositoryTransaction(Generic[RepositoryT]):
    """One open repository transaction and its public commit boundary."""

    repository: RepositoryT
    commit: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class RepositoryHarness(Generic[RepositoryT]):
    """Open fresh transactions over one repository implementation."""

    open: Callable[[], AbstractAsyncContextManager[RepositoryTransaction[RepositoryT]]]
