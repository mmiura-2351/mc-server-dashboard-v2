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
| `dashboard.html` | Community server list, live status demo |
| `server-create.html` | Create wizard (type/version → runtime → config/EULA) |
| `server-detail.html` | Overview / Console / Files / Backups / Players / Settings tabs |
| `community-settings.html` | Members / Roles / Grants / Groups / Audit / General tabs |
| `account.html` | Profile, password, memberships, account deletion |
| `admin-*.html` | Platform admin: overview, users, communities, workers, versions, audit |

Demo behaviors: fake log streaming + RCON echo on the server detail page, a
"starting → running" status flip on the dashboard (~7 s), metric sparklines,
and a periodic `gap` frame marker in the console.
