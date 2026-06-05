"""The ``LiveJarReferences`` seam: which pooled JARs a live server row still needs.

The reference set the JAR-pool GC (D4, issue #293) diffs the pool against. A
pooled JAR (content-addressed by SHA-256) is LIVE iff some server row's config
records it as its resolved JAR (``resolved_jar_sha256``): the hydrate endpoint
splices that exact pooled JAR into the working-set tar at transfer time
(dataplane/api/transfers.py), so a server with a live row needs its pooled JAR to
start. Published snapshots and backups EMBED ``server.jar`` inside their own tars
(the Worker's snapshot packs the whole working dir, excluding nothing at M1), so
they do NOT pin pool JARs; deleting a server (which removes its rows + working
set + backups) removes the only thing pinning its JAR.

The reference set is therefore a bounded scan of the ``server.config`` blobs. The
server rows are owned by the *servers* context, which the versions domain must not
import (import-linter). This Port is the clean seam — bound to the servers
repository only at the wiring layer — keeping the GC use case context-free, the
same posture as the versions ``JarPool`` seam over storage's ``JarStore``.
"""

from __future__ import annotations

import abc


class LiveJarReferences(abc.ABC):
    """Port: the set of pooled-JAR content keys live server rows still reference."""

    @abc.abstractmethod
    async def live(self) -> set[str]:
        """Return every resolved-JAR content key (lowercase-hex SHA-256) in use.

        One key per distinct value; a key shared by many servers appears once.
        Servers with no resolved JAR recorded contribute nothing.
        """
