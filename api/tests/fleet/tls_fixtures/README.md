# Control-plane TLS test fixtures

`server.crt` / `server.key` are a **test-only**, self-signed certificate/key
pair used by the control-plane gRPC TLS tests
(`tests/fleet/test_grpc_tls.py`). They prove the API's gRPC listener serves
over TLS and that a TLS client verifying against this cert can complete a
`Session`.

- **Not a secret, not for production.** The private key is committed on purpose
  so the tests are deterministic and need no certificate-minting dependency
  (Python's stdlib cannot mint X.509 certs). Never reuse this pair anywhere
  real.
- **Subject / SAN:** `CN=localhost`, `subjectAltName=DNS:localhost,IP:127.0.0.1`
  — the tests dial `127.0.0.1` / `localhost`.
- **Expiry:** far in the future (year 2126) so the suite does not rot.

Regenerate (only if ever needed) with:

```sh
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout server.key -out server.crt \
  -days 36500 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```
