"""Adapters for the ``servers`` context.

Concrete async-SQLAlchemy implementations of the ``domain`` repository and
``UnitOfWork`` Ports. Bound to Ports only in the wiring module; never imported by
``domain`` (ARCHITECTURE.md 2.1). The grant-sweep half of the unit of work reuses
the community context's resource-grant adapter on the same session, so a
server-delete and its grant sweep are one transaction (DATABASE.md Section 10).
"""
