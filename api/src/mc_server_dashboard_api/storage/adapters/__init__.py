"""Adapters for the ``storage`` context: the filesystem :class:`~.fs.FsStorage`.

Implements the :class:`~..domain.port.Storage` Port over a local directory tree
(docs/app/STORAGE.md Section 7.1), realizing the Section 4 atomic-publish
mechanics (stage -> move to a fresh snapshot -> atomic ``current`` symlink flip ->
parent-dir fsync -> reclaim) and the Section 4.3 orphan sweep. Bound to the Port
only in the edge wiring; never imported by the domain.
"""
