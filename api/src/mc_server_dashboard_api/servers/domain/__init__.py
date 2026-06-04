"""Pure domain core for the ``servers`` context: standard library only.

Entity (:class:`~.entities.Server`), value objects, domain errors, and Port
interfaces (``ServerRepository``, the grant-sweep Port, ``UnitOfWork``). No
framework, no I/O, no external library (ARCHITECTURE.md Section 2.1). The
``community`` it belongs to is referenced by id value only — no import of the
community domain (DATABASE.md Section 6; the FK lives at the persistence layer).
"""
