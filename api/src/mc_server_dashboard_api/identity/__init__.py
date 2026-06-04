"""Identity context: global user accounts and refresh-token sessions.

Holds the identity bounded context's quadrants (domain / adapters). M1 lands the
pure domain and its persistence here; the registration/login use cases and HTTP
edge land with their own features (ARCHITECTURE.md Section 6).
"""
