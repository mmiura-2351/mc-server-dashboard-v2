# Bedrock (Geyser) Support

> Status: **Shipped (initial release)** · Audience: contributors to `api/`,
> `worker/`, `webui/`, operators
>
> This document is the feature-level overview of epic
> [#1540](https://github.com/mmiura-2351/mc-server-dashboard-v2/issues/1540):
> letting Bedrock-edition players join Java servers through the relay,
> worker-NAT-hidden. It covers what a user/operator needs to know — how
> Bedrock gets enabled on a server, how players find it, and today's known
> limitations. For the network design (the QUIC datagram tunnel, the UDP
> ingress, flow demultiplexing, framing), see
> [`BEDROCK_TUNNEL.md`](BEDROCK_TUNNEL.md); this document does not repeat that
> material. For the Java path the Bedrock tunnel rides alongside, see
> [`RELAY.md`](RELAY.md). For settings, see
> [`CONFIGURATION.md`](CONFIGURATION.md) Sections 5.8 and 5.13. For deployment
> (firewall rules, `.env`, manual verification against a real Bedrock client),
> see [`../dev/DEPLOYMENT.md`](../dev/DEPLOYMENT.md) "Bedrock (Geyser)".

## Table of Contents

1. [Scope](#1-scope)
2. [Activation](#2-activation)
3. [Address model](#3-address-model)
4. [Known limitations](#4-known-limitations)

## 1. Scope

**Supported server types today: Paper only** (Geyser-Spigot + Floodgate). Fabric/Forge
(the Geyser mod) is a later extension; Vanilla is permanently unsupported (it
has no plugin directory, so there is nowhere to install Geyser). The relay,
tunnel, and port-allocation machinery are all server-type-agnostic; the Paper
restriction is solely because that is the only server type Geyser currently
ships a build for in this deployment's install paths (Section 2).

## 2. Activation

There is no dedicated "enable Bedrock" toggle and no auto-install. Installing
Geyser through the regular plugin flow **is** the enablement switch: the
dashboard detects Geyser on a server and automatically wires the network path
(allocates a `bedrock_port`, opens the relay tunnel once the server is
running). This also covers existing servers — install the plugin at rest and
the same detection applies on the next start.

Two install paths, because the two plugins ship differently:

- **Geyser**: install from the plugin catalog (Modrinth), like any other
  plugin. Latest is resolved at install time — no pinning, no bundling.
- **Floodgate**: upload the jar from
  [geysermc.org](https://geysermc.org/download#floodgate) instead. Modrinth
  carries no Spigot/Paper build of Floodgate (issue #1548), so the catalog
  path does not apply to it. Geyser auto-detects an installed Floodgate at
  runtime (`auth-type: FLOODGATE`) — no manual configuration is needed on
  either plugin.

The deployment-wide Bedrock gate (`relay.enabled` AND `relay.bedrock_enabled`,
`GET /api/meta` `bedrock_enabled`) must also be on, or Geyser detection
allocates nothing (`CONFIGURATION.md` Section 5.13). See
`../dev/DEPLOYMENT.md` "Bedrock (Geyser)" for turning it on.

## 3. Address model

RakNet has no SNI equivalent and Bedrock has no SRV record, so the relay
cannot route Bedrock the way it routes Java (by hostname read off the wire,
`RELAY.md` Section 3). The only viable discriminator is the destination UDP
port, so each Bedrock-enabled server gets its own public UDP port from a
dedicated window (`ports.bedrock_range_start..end`, default `19132..19231`,
`CONFIGURATION.md` Section 5.8).

A server's Bedrock address is therefore **`<base_domain>:<bedrock_port>`** —
the same wildcard-DNS base domain the Java path uses, but with a per-server
**port** instead of a per-server **subdomain**, and the player must type that
port (a Bedrock client's "Add Server" screen has separate host and port
fields). Server responses carry `bedrock_address` / `bedrock_port` (null when
Geyser is not installed/enabled or the deployment gate is off); the Web UI
surfaces them as a copyable badge next to the Java `join_hostname` badge (see
`../ui/WEBUI_SPEC.md`).

## 4. Known limitations

- **Real client IP is deferred.** Geyser and the server see the Worker's
  forwarder address, not the Bedrock player's real IP — per-client IP
  bans/rate-limits/logs are not accurate for Bedrock players today. Real-IP
  passthrough (PROXY protocol v2, minding its interaction with RakNet's
  connection cookie) is a future extension (epic #1540 "Locked decisions").
  The relay's own UDP ingress rate-limiting (`BEDROCK_TUNNEL.md` Section 8)
  still sees the true client address — only the *server's* view is affected.
- **Floodgate's auth model does not cover Bedrock players in moderation
  tooling keyed on Java identity.** Bedrock players join without a Java
  account (a Floodgate-prefixed username and a Floodgate UUID). Any
  whitelist/ban/group membership keyed on a Java username or UUID does not
  match them.
- **Geyser/server version skew.** The Geyser catalog always resolves the
  latest release, which targets the newest Java Minecraft version. Geyser
  still boots and answers RakNet against an older server, but a real Bedrock
  client's join needs the two protocol versions to line up — either update
  the server or install ViaVersion on it to bridge the gap. Observed live
  against a Paper 1.21.1 server (epic #1540 issue #1542).
