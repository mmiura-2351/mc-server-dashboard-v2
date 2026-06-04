"""Pure domain core for the ``storage`` context: standard library only.

The :class:`~.port.Storage` Port (docs/app/STORAGE.md Section 3), its value
objects (keys/ids, the validated relative path) and typed errors. No framework,
no I/O, no external library, and no import of any other context (ARCHITECTURE.md
Section 2.1); the community/server scope is carried by id value only.
"""
