# Minecraft Server Dashboard v2

A self-hostable dashboard to provision and operate Minecraft servers across one
or more hosts. An authoritative API owns identity, Communities, authorization,
and data; stateless Workers run the Minecraft processes; a React web UI drives
it all from the browser.

This file is the conceptual front door — *what* this is and *why*. Operational
and development how-to lives under [`docs/`](docs/README.md); this README links
into it rather than duplicating it.

## Philosophy & goals

The system manages Minecraft servers for small communities. It keeps the
machine that owns state and business logic apart from the machines that run
Minecraft, treats the way a server is run as a pluggable backend, and decides
what each member may do per operation inside a Community.
[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) is the source of truth for scope;
the design principles are:

- **API ↔ Worker separation.** The authoritative API is split from the machines
  that run Minecraft. Execution is delegated to **Workers** on separate hosts,
  so capacity scales by adding Workers (REQUIREMENTS.md Section 1).
- **Pluggable execution and storage.** How a server runs (host process,
  container, …) sits behind an `ExecutionDriver`; the authoritative store
  (fs / remote-fs / object) sits behind a `Storage` Port. Both are selected by
  configuration, so new backends drop in without touching business logic
  (REQUIREMENTS.md Section 8).
- **The Community model.** Resources belong to **Communities** — a lightweight
  multi-tenancy for small groups. A user account is global and may join many
  Communities, holding different roles in each (REQUIREMENTS.md Sections 6.2
  and 6.3).
- **Two-layer authorization.** Layer 1 is visibility: non-members get no
  existence signal for a Community's resources. Layer 2 is operations within a
  Community, decided by custom roles plus per-resource grants
  (REQUIREMENTS.md Section 6.4).
- **Simplicity first, with the right seams.** Target scale is small (a few dozen
  Communities, tens of concurrent servers). Implementations stay simple, but
  the abstractions never assume small scale in their *shape*
  (REQUIREMENTS.md Section 1.1).

## Architecture

The system is two cooperating services plus a shared contract and a browser UI;
an optional game-ingress relay ([`docs/app/RELAY.md`](docs/app/RELAY.md)) lets
Workers behind NAT serve players without exposing their IP.

```
        ┌───────────┐   HTTP (same-origin)   ┌──────────────────────────┐
        │  Browser  │ ─────────────────────▶ │  api/   (Python/FastAPI)  │
        │  webui/   │ ◀───── serves SPA ───── │  authoritative: state,   │
        └───────────┘                         │  auth, Storage, registry │
                                              └────────────┬─────────────┘
                                                           │ proto/ (gRPC
                                                           │ control plane)
                                                           ▼
                                              ┌──────────────────────────┐
                                              │  worker/   (Go)          │
                                              │  stateless: runs MC via  │
                                              │  ExecutionDriver         │
                                              └──────────────────────────┘
```

- **`api/`** (Python / FastAPI) — the **authoritative** service. Owns identity,
  Communities, authorization, server lifecycle records, the pluggable `Storage`,
  the Worker registry, and both ends of the API↔Worker channel.
- **`worker/`** (Go) — a **stateless, replaceable** agent that actually runs
  Minecraft via a pluggable `ExecutionDriver`. It holds no authoritative data:
  it hydrates on start and snapshots on stop or on an interval.
- **`proto/`** (buf) — the shared protobuf/gRPC **control-plane contract** between
  the two services. The Worker initiates a persistent bidirectional stream that
  multiplexes API→Worker commands and Worker→API events; `api/` and `worker/`
  depend on `proto/` but never on each other.
- **`webui/`** (React SPA) — the browser UI, served **same-origin** by the API
  container (FastAPI `StaticFiles`), so there is no CORS layer.

Both services follow **Hexagonal (Ports & Adapters)** layering: a pure domain
core, use cases depending only on domain Ports, adapters implementing those
Ports, and wiring at the edge. The dependency arrow always points inward to the
domain. See [`docs/app/ARCHITECTURE.md`](docs/app/ARCHITECTURE.md) for the full
layering and module boundaries, and
[`docs/app/CONTROL_PLANE.md`](docs/app/CONTROL_PLANE.md) for the API↔Worker
contract.

## Repository layout

| Path | What it is |
|---|---|
| [`api/`](api/README.md) | Authoritative API service (Python / FastAPI). |
| [`worker/`](worker/README.md) | Stateless Minecraft execution agent (Go). |
| [`relay/`](relay/README.md) | Optional game-ingress relay (Go): hostname-routed player traffic; NAT'd Workers dial back to it. |
| [`webui/`](webui/README.md) | Single-page web UI (React + TypeScript + Vite). |
| [`proto/`](proto/README.md) | Shared protobuf / gRPC control-plane contract (buf). |
| [`docs/`](docs/README.md) | Long-form requirements, architecture, and developer docs. |

## Documentation map

- [`docs/README.md`](docs/README.md) — the deep-docs index (application and
  developer docs).
- [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) — the source of truth for scope.
- [`docs/dev/DEPLOYMENT.md`](docs/dev/DEPLOYMENT.md) — how to deploy and run a
  single-host stack.
- [`docs/dev/DEVELOPMENT.md`](docs/dev/DEVELOPMENT.md) — first-time setup and the
  day-to-day developer workflow.

## License

Released under the MIT License — see [`LICENSE`](LICENSE).
