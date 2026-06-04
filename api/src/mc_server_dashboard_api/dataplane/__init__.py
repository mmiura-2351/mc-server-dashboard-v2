"""The API-terminated HTTP data plane (epic #8, issue #106).

The control plane only *triggers* a transfer; the bulk bytes of a working set
ride this separate HTTP surface so a multi-GB hydrate/snapshot never blocks
control traffic (REQUIREMENTS.md Section 5.2, ARCHITECTURE.md Section 4,
CONTROL_PLANE.md Section 1). These endpoints are worker-authenticated only (the
shared control-plane credential as a Bearer token) and are never user-facing;
the contract is documented in docs/app/STORAGE.md Section 8.
"""
