"""Adapters for the ``identity`` context.

Concrete async-SQLAlchemy implementations of the ``domain`` repository and
``UnitOfWork`` Ports plus the system ``Clock``. Bound to Ports only in the wiring
module; never imported by ``domain`` (ARCHITECTURE.md 2.1).
"""
