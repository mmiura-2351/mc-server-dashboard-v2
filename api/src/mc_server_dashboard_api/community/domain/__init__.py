"""Pure domain core for the ``community`` context: standard library only.

Entities (``Community``, ``Membership``, ``Role``, ``ResourceGrant``), value
objects, domain errors, and Port interfaces (repositories, ``UnitOfWork``). No
framework, no I/O, no external library (ARCHITECTURE.md Section 2.1). The
``user`` it references is a foreign identity held by id value only — no import
of the identity domain (DATABASE.md Section 5; the FK lives at the persistence
layer).
"""
