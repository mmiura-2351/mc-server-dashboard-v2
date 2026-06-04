"""Storage context: the authoritative API-side store for world data, JARs, backups.

Holds the storage bounded context's quadrants (domain / adapters). The domain owns
the :class:`~.domain.port.Storage` Port (docs/app/STORAGE.md Section 3) and its
value objects/errors; the adapters own the filesystem implementation that realizes
the Section 4 atomic-publish mechanics. This sub-issue (#104) lands the Port, the
``fs`` adapter, config wiring, and the crash-recovery sweep; the data-plane HTTP
transport, the object adapter, and snapshot cadence land in later sub-issues of
epic #8.
"""
