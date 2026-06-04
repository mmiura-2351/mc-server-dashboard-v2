"""Adapters for the ``core`` context.

Concrete implementations of ``domain`` Ports (here: the async-SQLAlchemy
database plumbing and its liveness probe). Bound to Ports only in the wiring
module; never imported by ``domain`` or ``application`` (ARCHITECTURE.md 2.1).
"""
