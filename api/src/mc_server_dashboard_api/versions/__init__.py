"""Versions context: the global version catalog and JAR provisioning (FR-VER-1/2/3).

Holds the versions bounded context's quadrants (domain / application / adapters /
api). The catalog lists the server types and versions the platform offers, and the
ensure-on-start use case downloads, verifies, and pools the resolved server JAR.

**Java-only at M1.** The catalogued sources (Mojang for vanilla, PaperMC for
Paper) serve Java-edition server JARs only; Bedrock has no catalogued source. So
the catalog resolves Java versions exclusively, and server create accepts only
``mc_edition == 'java'`` (rejecting other editions before staging the row). A
Bedrock catalog is a later milestone.
"""
