"""Adapters for the ``storage`` context: :class:`~.fs.FsStorage` and
:class:`~.object_store.ObjectStorage`.

Both implement the same :class:`~..domain.port.Storage` Port (docs/app/STORAGE.md
Section 7) with the same observable atomic-publish guarantee (Section 4), differing
only in realization: ``fs`` over a local directory tree (Section 7.1, ``current``
symlink flip + parent-dir fsync + orphan sweep) and ``object`` over an
S3-compatible store (Section 7.3, pointer-object flip + key-prefix layout + orphan
sweep). The object adapter's S3 client library lives behind the narrow
:class:`~.object_store.S3Client` protocol in :mod:`.object_client`, so the
dependency stays at the very edge. Bound to the Port only in the edge wiring;
never imported by the domain.
"""
