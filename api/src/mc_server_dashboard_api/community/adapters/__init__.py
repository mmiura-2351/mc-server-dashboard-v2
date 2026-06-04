"""Adapters for the ``community`` context.

Concrete async-SQLAlchemy implementations of the ``domain`` repository and
``UnitOfWork`` Ports. Bound to Ports only in the wiring module; never imported
by ``domain`` (ARCHITECTURE.md 2.1).
"""
