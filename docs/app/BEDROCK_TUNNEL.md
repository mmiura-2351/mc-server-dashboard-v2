# Bedrock Relay Tunnel

> Status: **Implemented** (e2e-verified, issue #1547) · Audience: contributors to `relay/`, `worker/`, `proto/`
>
> This document is the reference for the **relay-side Bedrock (RakNet/UDP)
> tunnel**: the QUIC listener that authenticates a Worker's outbound dial-out,
> the public per-server UDP ingress, the flow table, and the datagram framing
> that carries RakNet traffic between a Bedrock client and a Worker's Geyser
> instance. It is a companion to [`RELAY.md`](RELAY.md) (the Java ingress
> design) and to [`CONTROL_PLANE.md`](CONTROL_PLANE.md) (`OpenBedrockTunnel` /
> `CloseBedrockTunnel`). It implements the network path chosen in epic
> [#1540](https://github.com/mmiura-2351/mc-server-dashboard-v2/issues/1540)
> and issue
> [#1545](https://github.com/mmiura-2351/mc-server-dashboard-v2/issues/1545).
> Once the proto messages exist, the buf module under
> [`../../proto/`](../../proto/) (`mcsd.bedrocktunnel.v1`) is the binding
> contract and this document explains it; where they disagree, the `.proto`
> file wins.
>
> The Worker-side QUIC client (issue #1546, `worker/internal/adapters/bedrocktunnel/`)
> implements this document's wire contract as the client. The feature-level
> overview and deployment docs landed with issue #1547 -- see
> [`BEDROCK.md`](BEDROCK.md) and `../dev/DEPLOYMENT.md` "Bedrock (Geyser)".

## Table of Contents

1. [Scope](#1-scope)
2. [Topology](#2-topology)
3. [Lifecycle](#3-lifecycle)
4. [Handshake: TunnelHello / TunnelHelloAck](#4-handshake-tunnelhello--tunnelhelloack)
5. [Datagram framing](#5-datagram-framing)
6. [Datagram MTU](#6-datagram-mtu)
7. [Flow table](#7-flow-table)
8. [Abuse protection](#8-abuse-protection)
9. [Configuration](#9-configuration)
10. [Config-drift decision](#10-config-drift-decision)
11. [Out of scope / future work](#11-out-of-scope--future-work)

---

## 1. Scope

The relay carries Java traffic over TCP (`RELAY.md`). Bedrock (RakNet) is UDP,
runs its own reliability/congestion control, and must not be tunneled over TCP
(epic #1540 "why this is non-trivial"). This adds a second, independent data
path in the relay binary:

- A **QUIC listener** (RFC 9221 DATAGRAM) that a Worker dials outbound to open
  a per-server Bedrock tunnel, authenticating with a credential the API minted.
- A **per-server public UDP port** (`server.bedrock_port`), bound only while
  that server's tunnel is live, that Bedrock clients connect to.
- A **flow table** mapping Bedrock client source addresses to compact flow ids,
  so one QUIC connection can multiplex many clients.

The relay stays a **thin forwarder**: it never parses RakNet, never sees a
Minecraft protocol byte, and moves opaque datagrams in both directions. Its
only new state is the live tunnel itself (the bound UDP socket, the QUIC
connection, and that connection's flow table) -- there is no separate,
persistent server table; the authenticated QUIC dial-out from the Worker **is**
the registration, exactly as the epic's design specifies.

## 2. Topology

```
Bedrock client
   |  RakNet/UDP -> <base_domain>:<bedrock_port>
   v
RELAY (public):
   - bedrock tunnel QUIC listener (bedrock.tunnel_listen, e.g. :25675/udp)
     -- accepts the Worker's outbound dial, runs the TunnelHello handshake
   - per-server UDP ingress (server.bedrock_port, bound on acceptance)
     -- per-client flow table, ipcaps hygiene
   |  QUIC connection (Worker-initiated, outbound, RFC 9221 DATAGRAM frames)
   v
WORKER (NAT-hidden, issue #1546):
   QUIC client -> UDP to <container>:19132 (docker network)
   v
MC container: Geyser (+ Floodgate), RakNet :19132 -> local Java server
```

The relay's role mirrors the Java tunnel listener
(`relay/internal/tunnel/listener.go`) but over QUIC instead of TLS/TCP, and
with the roles of "connection" and "port" inverted: the Java tunnel is
per-*player-session* (one dial-back per join); the Bedrock tunnel is
per-*server* (one QUIC connection carries every Bedrock client currently
talking to that server, multiplexed by flow id).

## 3. Lifecycle

1. A Bedrock-enabled server reaches `running`. The API mints a credential and
   dispatches `OpenBedrockTunnel { server_id, relay_endpoint, bedrock_port,
   token, tls_ca_pem }` to the Worker over the existing control-plane stream
   (`CONTROL_PLANE.md`, issue #1544, already landed).
2. The Worker (issue #1546) dials the relay's Bedrock tunnel QUIC listener at
   `relay_endpoint`, verifying the relay's certificate against `tls_ca_pem`
   (empty means system roots), and negotiates ALPN `mcsd-bedrock/1` (Section
   4).
3. On the first bidirectional QUIC stream, the Worker sends `TunnelHello
   {server_id, bedrock_port, token}`. The relay calls
   `mcsd.relay.v1.RelayService.ValidateBedrockTunnel(server_id, bedrock_port,
   token)` on the API (the relay has no local waiter to match against, unlike
   the per-player Java token -- Section 10 explains why) and answers with
   `TunnelHelloAck {accepted, reject_reason}`.
4. **Accepted**: the relay binds `net.ListenPacket("udp", ":<bedrock_port>")`
   and maps it to this QUIC connection -- the bind IS the registration. From
   here, datagrams pump both directions (Section 5) until the connection
   closes.
5. **Rejected** (bad credential, validation RPC failure, or the declared port
   could not be bound for a reason other than a same-server stale connection,
   which is displaced instead -- Section 3.1): the relay closes the QUIC
   connection. No UDP port is bound.
6. On server stop (`CloseBedrockTunnel` invalidates the credential API-side)
   or a QUIC disconnect, the relay unbinds the UDP port. The token is **not**
   single-use (unlike the Java per-player token): the Worker may redial with
   the same credential after a transient QUIC drop, and `ValidateBedrockTunnel`
   accepts a repeated presentation for as long as the tunnel is open
   API-side.

Two obligations on the Worker's side of the connection (binding for the
Worker implementation, issue #1546):

- **Keepalives.** The relay applies an explicit 15 s QUIC idle timeout to
  every tunnel connection (`maxIdleTimeout`,
  `relay/internal/bedrock/listener.go`; the effective timeout is the minimum
  of both peers' values). A tunnel with zero connected Bedrock players -- the
  common case -- carries no datagrams, so the Worker MUST enable QUIC
  keepalives with a period well under that timeout (e.g. quic-go
  `KeepAlivePeriod` of 5 s, a third of it). This is also what holds the
  Worker's NAT mapping open (an epic #1540 locked decision). Without
  keepalives, every idle tunnel collapses at the idle timeout and the Worker
  redials in a loop forever.
- **Graceful close.** On its own shutdown and on `CloseBedrockTunnel`, the
  Worker MUST close the QUIC connection (CONNECTION_CLOSE, e.g. quic-go
  `CloseWithError`) rather than just dropping it. A redial takes the port over
  from a stale connection regardless (Section 3.1), so this no longer gates
  reconnection -- but an unclosed connection still holds its UDP port and
  goroutines until either that redial displaces it or the relay notices the
  connection is gone (for a silent drop, the idle timeout), so closing
  promptly is still the contract.

### 3.1 Redial and takeover

A redial while the relay has not yet noticed the *old* QUIC connection is dead
**displaces** it: a hello that passes `ValidateBedrockTunnel` for a port
already bound to another connection closes the old connection and adopts the
new one, instead of being rejected (takeover semantics, issue #1565). With a
gracefully-closing Worker (Section 3) no stale connection is present at all --
the CONNECTION_CLOSE frees the port before or with the redial. Takeover is what
removes the outage for ungraceful ends (Worker crash, network partition),
where the old connection would otherwise linger until the relay's explicit
15 s idle timeout (`maxIdleTimeout`, `relay/internal/bedrock/listener.go`);
that idle timeout remains the backstop for a stale connection whose server is
*not* being redialed.

Takeover needs only the live port-to-Tunnel index (`Listener.tunnels`,
`relay/internal/bedrock/listener.go`), never a server table -- it holds only
ports with a currently bound tunnel and is cleared when a tunnel's `run`
returns. It is not a new auth surface: the displacing hello passes the same
`ValidateBedrockTunnel` check as any bind, so a token holder that could claim
the port after the idle timeout can now claim it immediately, and nothing
weaker can. The displace-then-bind-then-register sequence runs under one mutex
(`Listener.mu`), so concurrent redials for the same port cannot both adopt it;
the displaced tunnel's teardown is idempotent (`Tunnel.close`, `sync.Once`) and
its stale run's `unregister` is a compare-and-delete, so it never removes the
new occupant.

## 4. Handshake: TunnelHello / TunnelHelloAck

Defined in [`../../proto/`](../../proto/) as `mcsd.bedrocktunnel.v1`
(`bedrock_tunnel.proto`) -- Go only, generated for both `worker/` and
`relay/`, since this is a Worker<->relay wire contract the API never sees;
see `proto/README.md`.

- **ALPN**: `mcsd-bedrock/1` -- distinguishes the Bedrock tunnel listener from
  any other QUIC/TLS service that might share the relay's certificate. The
  relay negotiates it via `tls.Config.NextProtos`.
- **TLS material**: the Bedrock tunnel listener reuses the **same** certificate
  as the Java tunnel listener (`tunnel.tls.cert_file` / `tunnel.tls.key_file`,
  RELAY.md Section 13) -- only the ALPN differs on the wire. This is why
  `OpenBedrockTunnel.tls_ca_pem` carries the same CA the Worker already trusts
  for `TunnelDial` (RELAY.md Section 5): one certificate, one CA, two ALPNs.
- **Stream wire format**: on the first bidirectional QUIC stream after
  connecting, each message (in either direction) is framed as a 4-byte
  big-endian length prefix followed by that many bytes of protobuf-marshaled
  message data. `TunnelHello` travels Worker -> relay; `TunnelHelloAck`
  travels relay -> Worker; this is the only exchange on the stream, and both
  sides close their side of the stream immediately after (see the proto file's
  package doc comment for the full framing note, including the size cap and
  read deadline the relay enforces pre-authentication).
- **Rejection**: the relay writes a rejecting ack and then closes the QUIC
  connection. Implementation note: closing the connection immediately after
  writing races the ack's delivery (an in-flight CONNECTION_CLOSE can outrun
  unacknowledged stream data), so the relay waits for the Worker to close its
  side of the stream -- or a bounded deadline, if it does not -- before closing
  the connection (`relay/internal/bedrock/listener.go`,
  `awaitStreamPeerClose`).

## 5. Datagram framing

Once `TunnelHelloAck.accepted` is `true`, every QUIC DATAGRAM frame (RFC 9221)
on the connection, in either direction, is:

```
+------------------+---------------------------+
| flow id (4 bytes,|  RakNet payload (opaque)   |
| big-endian)       |                            |
+------------------+---------------------------+
```

- The **relay assigns flow ids**, one per distinct Bedrock client source
  address (`ip:port`) seen on the bound `bedrock_port` (Section 7).
- The **Worker only ever echoes back** the flow id it received on an inbound
  datagram when it sends the corresponding reply; it never mints one.
- A flow id the relay does not recognize (e.g. the flow was evicted for
  inactivity between the datagram going out and the reply coming back) is
  dropped, not an error.
- Flow ids are **connection-scoped**: every new QUIC connection starts a
  fresh flow table and ids restart from zero, so the Worker MUST discard any
  flow-id state it holds when it redials -- an id carried across a reconnect
  would misroute.
- Neither the relay nor this framing has any awareness of RakNet's own packet
  structure -- the payload is carried byte-for-byte.

## 6. Datagram MTU

`maxDatagramPayload = 1200` bytes (`relay/internal/bedrock/listener.go`) is
the RakNet-payload budget the relay is willing to forward in one QUIC
DATAGRAM. A client UDP datagram larger than this is silently dropped (not
forwarded) rather than fragmented -- RFC 9221 DATAGRAM frames cannot be
fragmented at all, so "conservative" is the only lever available.

**Rationale:**

- RakNet performs its own MTU discovery against whatever it believes the
  server is (the relay, from the client's perspective): it pads early
  handshake packets ("Open Connection Request 1") to a few candidate sizes,
  commonly around 1492, 1200, and 576 bytes, and falls back to the next
  smaller candidate whenever a probe gets no reply.
- The relay does not participate in RakNet, so it cannot rewrite what MTU
  RakNet settles on -- but it *can* simply decline to forward anything larger
  than its own budget. A 1492-byte probe silently fails to reach the Worker
  (dropped at the relay), RakNet retries with the next candidate, and once a
  probe fits within `maxDatagramPayload` it round-trips and RakNet locks that
  size in for the rest of the session. 1200 is one of RakNet's own common
  candidates, so this converges cleanly rather than forcing an arbitrary,
  off-menu size.
- 1200 bytes (plus the 4-byte flow id, 1204 total) fits within `quic-go`'s
  pre-path-MTU-discovery per-datagram limit (~1225 bytes measured on an IPv4
  loopback connection), so `SendDatagram` does not return
  `DatagramTooLargeError` even before Path MTU Discovery (RFC 8899, enabled
  by default) has run its course on a fresh connection. On an IPv6
  minimum-MTU (1280) path the margin is only a few bytes -- not comfortable
  -- but a datagram that does not fit is refused at the sender and RakNet's
  probe-based discovery simply steps down to the next candidate, so the
  budget degrades gracefully rather than breaking. It is also under the
  ~1400-1480 byte usable MTU commonly left by VPNs, PPPoE, and mobile
  carriers on the relay<->Worker leg -- the leg most likely to be
  constrained, since a home-NAT Worker is the target deployment.
- Once RakNet has settled on an MTU at or under this budget, it caps its own
  packet sizes to that value for the rest of the session (fragmenting larger
  reliable messages itself), so the cap is enforced once at connection time in
  practice, not per-packet in steady state -- the drop path in
  `pumpUDPToQUIC` is a safety net, not the common case.

The gate is enforced on the client-to-Worker direction only (`pumpUDPToQUIC`
drops an oversized client datagram); the Worker-to-client direction is not
size-checked at the relay -- an oversized Worker frame is refused at the
Worker's own QUIC datagram-size limit when sending, and post-convergence the
server side does not produce datagrams above the session's negotiated RakNet
MTU anyway. The flow table's idle window (`flowIdleTimeout`, Section 7) is an
unrelated constant. Update this section if the value changes.

## 7. Flow table

`relay/internal/bedrock.FlowTable` (`relay/internal/bedrock/flowtable.go`) is a
NAT-style address<->flow-id map, one instance per bound `bedrock_port`
(i.e. per live tunnel, not shared across servers):

- `Lookup` / `Create` split the read and allocate paths so the caller can apply
  admission checks (Section 8) before committing to a new flow.
- `AddrByID` resolves a flow id back to the client address for the
  relay-to-client direction, and -- like `Lookup` -- refreshes the entry's idle
  deadline, since a reply is as much "activity" as a fresh client datagram.
- Idle entries (default 60 s, `flowIdleTimeout`) are evicted by a periodic
  sweep (`flowSweepInterval`, 15 s) tied to the tunnel's own lifetime; eviction
  also releases the entry's `ipcaps` slot (Section 8).

There is deliberately no cross-tunnel flow table: a `Tunnel` (one per bound
`bedrock_port`) owns its own `FlowTable` and its own `ipcaps.IPCaps` instance,
so state for one server's Bedrock ingress cannot leak into another's and both
are freed together when the tunnel unbinds.

## 8. Abuse protection

A public UDP port with no per-packet authentication invites RakNet
unconnected-ping amplification/spam (an attacker who spoofs a victim's source
IP can make the relay reply toward that victim). The relay reuses
`relay/internal/ipcaps` -- the same per-IP hygiene package the Java game and
tunnel listeners already use -- applied per bound `bedrock_port`:

- **New-flow rate cap** (`AllowJoin`): bounds how many *new* distinct clients
  (by source address) one source IP may start per second. Checked once, when a
  UDP datagram arrives from an address with no existing flow.
- **Concurrent-flow cap** (`Acquire` / `Release`): bounds how many flows one
  source IP may hold at once on a bound port. `Acquire` is called alongside the
  rate check when a flow is created; `Release` is called when the flow table's
  sweep evicts an idle flow.

Both caps are per source **IP** (not `ip:port`), matching how `ipcaps` already
treats the Java listeners, and both are configurable (Section 9). This is
hygiene, not volumetric DDoS protection, matching the posture RELAY.md already
documents for the Java listeners (Section 16 there).

The **QUIC tunnel listener itself** carries a separate per-IP cap on
concurrent unauthenticated handshake windows
(`bedrock.tunnel_max_conns_per_ip`, default 64) -- the same #968 posture as
the TCP tunnel listener's `tunnel.max_conns_per_ip`. Each pre-auth connection
holds relay resources for up to ~15 s and a parseable `TunnelHello` drives a
`ValidateBedrockTunnel` RPC to the API, so an uncapped listener would let one
source IP burn relay TLS work and amplify RPCs against the API indefinitely.
The slot covers only the pre-auth window: it is released once the handshake
resolves either way, so long-lived accepted tunnels do not count against it.

## 9. Configuration

Relay binary (`relay.toml`, `MCD_RELAY_` env prefix -- see
[`CONFIGURATION.md`](CONFIGURATION.md) Section 1-2 for the general precedence
rules and RELAY.md Section 13 for the sibling Java-path keys):

| Key | Default | Meaning |
|---|---|---|
| `bedrock.enabled` | `false` | Master switch for this listener (issue #1584). Off by default so a Java-only relay neither binds nor requires the Bedrock UDP ports (this listener and the per-tunnel `bedrock_port` window) on upgrade -- a host-port conflict on either must not take Java joins down. Wired from the same operator setting as the API's `relay.bedrock_enabled`, via `MCD_RELAY_BEDROCK_ENABLED` (compose sets it from `MCD_API_RELAY__BEDROCK_ENABLED`; see `docs/dev/DEPLOYMENT.md` "Relay" for the upgrade note). |
| `bedrock.tunnel_listen` | `:25675` | The public QUIC/UDP address Workers dial to open a Bedrock tunnel. Reuses `tunnel.tls.{cert_file,key_file}` (Section 4); no separate cert/key configuration. Bound only when `bedrock.enabled` is true. |
| `bedrock.tunnel_max_conns_per_ip` | `64` | Per-IP concurrent cap on unauthenticated handshake windows on the QUIC listener (Section 8), mirroring `tunnel.max_conns_per_ip` on the TCP tunnel listener (issue #968). |
| `bedrock.max_flows_per_ip` | `32` | Per-IP concurrent-flow cap on a bound `bedrock_port` (Section 8). |
| `bedrock.new_flows_per_ip_per_second` | `10` | Per-IP new-flow rate cap on a bound `bedrock_port` (Section 8). |

`bedrock.tunnel_listen`'s default (`:25675`) intentionally matches the
API-side `relay.bedrock_tunnel_port` default (`25675`, `CONFIGURATION.md`
Section 5.13, landed with issue #1550) -- see Section 10. The QUIC idle
timeout (15 s, Section 3) is a code constant (`maxIdleTimeout`), not a config
key.

## 10. Config-drift decision

The API derives `OpenBedrockTunnel.relay_endpoint` from the registered relay's
host (learned via `Register`, same as the Java tunnel endpoint) plus the
**API-side** `relay.bedrock_tunnel_port` setting -- it does not learn the
Bedrock port from the relay itself. This mirrors `relay.game_port` /
`relay.tunnel_port`: both sides are operator-configured and must agree, and
that agreement is not self-healing.

This was flagged as an open design note both in the issue #1545 PM comment and
directly in code: `server_state_sink.py`'s `_sync_bedrock_tunnel` carries a
`NOTE (issue #1545)` inviting the relay implementation to have the relay
advertise its own Bedrock endpoint over `Register` instead (mirroring how it
already advertises the Java `tunnel_endpoint`), in which case the API's
derivation would switch to reading that registered value.

**Decision for this issue: keep the config-based derivation; do not add
self-advertisement.** Reasons:

- Doing so would require a breaking-additive change to
  `mcsd.relay.v1.RegisterRequest`/`RegisterResponse` (a new field) plus
  Python-side changes in `api/src/mc_server_dashboard_api/fleet/adapters/`
  (`RelayRegistration`) and `servers/adapters/server_state_sink.py` --
  cross-context, cross-language work outside this issue's stated scope (relay
  QUIC listener + UDP ingress + flow table + rate caps).
- The risk is bounded and precedented: it is the exact same class of risk the
  deployment already accepts for `relay.game_port` / `relay.tunnel_port` today
  (both operator-configured, not self-healing), with the same mitigation --
  matching defaults out of the box (`bedrock.tunnel_listen` here defaults to
  `:25675`, the same value as the API's `relay.bedrock_tunnel_port` default)
  and `docs/app/CONFIGURATION.md` documenting the pairing.
- It keeps this PR's diff scoped to `relay/` and `proto/`, matching the rest
  of the epic's per-sub-issue split (API-side Bedrock work landed in #1544;
  this is the relay sub-issue).

If this bites in practice, it is a small, additive follow-up flagged in both
places it needs to change (the code `NOTE` above and this section) -- not a
redesign.

## 11. Out of scope / future work

- ~~**End-to-end deployment + docs** (issue #1547)~~ **Landed with #1547**:
  the feature-level overview lives in [`BEDROCK.md`](BEDROCK.md), the
  compose UDP exposure / firewall guidance / manual-verification checklist in
  `../dev/DEPLOYMENT.md` "Bedrock (Geyser)", and the protocol-level e2e in
  `scripts/run_bedrock_e2e.sh` (`.github/workflows/bedrock-e2e.yml`).
- ~~**Stale-connection takeover on redial** (issue #1565)~~ **Landed**: a
  validated hello for an already-bound port displaces the old connection; see
  Section 3.1.
- **Real-client-IP passthrough** -- deferred beyond this initial scope per
  epic #1540; Geyser and the server see the Worker's forwarder IP.
- **Bedrock session reporting** -- the Java tunnel batches `SessionStart` /
  `SessionEnd` to the API (RELAY.md Section 8); no analogous reporting exists
  for Bedrock flows yet.
- **Metrics** -- no Prometheus metrics for flow counts, drop counts, or bind
  failures yet (RELAY.md Section 17 notes the same gap for the Java path).
