# Web UI mockup

Static, clickable mockup of the Web UI (WEBUI_SPEC.md). **No real API calls** —
all data is embedded in `assets/mock-data.js`; buttons show a toast describing
the API call the real implementation would make.

Open `login.html` in a browser (no server needed), or:

```sh
python3 -m http.server -d docs/ui/mockup 8088
# → http://localhost:8088/login.html
```

Pages:

| Page | Covers |
|---|---|
| `login.html` / `register.html` | Auth |
| `no-community.html` | No-community empty state (#584) — zero-membership landing |
| `dashboard.html` | Community server list, live status demo |
| `server-create.html` | Create wizard (type/version → runtime → config/EULA) |
| `server-detail.html` | Overview / Console / Files / Backups / Players / Settings tabs |
| `community-settings.html` | Members / Roles / Grants / Groups / Audit / General tabs |
| `account.html` | Profile, password, memberships, account deletion |
| `admin-*.html` | Platform admin: overview, users, communities, workers, versions, audit |

Demo behaviors: fake log streaming + RCON echo on the server detail page, a
"starting → running" status flip on the dashboard (~7 s), metric sparklines,
and a periodic `gap` frame marker in the console.

## Responsive / mobile (epic #583)

`assets/style.css` carries a contained responsive layer mirroring the shipped
`webui/src/styles/{tokens,shell}.css`, so every page above reflows when the
browser is narrowed (no separate phone-width HTML files). Two breakpoints:

- **≤900px (tablet, `--bp-tablet`)** — the sidebar collapses to an **icon-only
  rail** (#586), the top bar tightens and the account link drops to just its
  avatar (#554/#585), and the two-column dashboard grid stacks.
- **≤430px (phone, `--bp-phone`)** — content padding tightens 24px→12px (#618),
  buttons wrap instead of overflowing (#618), wide `table.data` becomes a
  contained horizontal scroller (#620), `.form-row` / role matrix stack to a
  single column (#621), and the file browser two-pane layout stacks (#619).

The breakpoint **values** live as `--bp-phone` / `--bp-tablet` tokens; because
CSS variables can't be used inside `@media`, each rule repeats the matching
literal and is kept in sync with the token by convention. See WEBUI_SPEC.md
Section 7.8 for the full per-area spec and known limitations (#626, #627).

Deferred (intentionally, to keep the mockup focused): no per-page phone-width
HTML variants and no responsive demo of the in-browser file editor's reduced
min-height — the shared CSS layer covers the reflow for the documentation's
purpose, and the spec text is the source of truth for the editor decision.
