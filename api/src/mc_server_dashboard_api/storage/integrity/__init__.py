"""Structural integrity checks for stored world data (issue #738, parent #703).

A pure, standard-library-only library that structurally validates Minecraft
``.mca`` region containers and walks a working set to aggregate the result. It
performs no DB access, no policy, and no wiring into the snapshot/backup paths
(that is the dependent issue #739); it only reads the files it is handed.
"""
