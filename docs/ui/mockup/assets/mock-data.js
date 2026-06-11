/* Mock data only — the mockup never calls the real API.
 * Shapes mirror the API response models (WEBUI_SPEC.md Section 2). */

window.MOCK = {
  me: { id: "u-1", username: "miura", email: "mmiura@example.com", is_platform_admin: true },

  communities: [
    { id: "c-1", name: "Sakura SMP" },
    { id: "c-2", name: "Dev Playground" },
  ],
  currentCommunity: { id: "c-1", name: "Sakura SMP" },

  servers: [
    {
      id: "s-1", name: "survival", server_type: "paper", mc_version: "1.21.6",
      execution_backend: "container", game_port: 25565,
      desired_state: "running", observed_state: "running",
      observed_at: "2026-06-06T09:58:12Z", assigned_worker_id: "worker-a",
      players: 7, cpu: 38, mem_gb: 3.2,
    },
    {
      id: "s-2", name: "creative-build", server_type: "vanilla", mc_version: "1.21.6",
      execution_backend: "container", game_port: 25566,
      desired_state: "running", observed_state: "starting",
      observed_at: "2026-06-06T10:01:44Z", assigned_worker_id: "worker-a",
      players: 0, cpu: 71, mem_gb: 1.8,
    },
    {
      id: "s-3", name: "modded-forge", server_type: "forge", mc_version: "1.20.1",
      execution_backend: "container", game_port: 25570,
      desired_state: "running", observed_state: "crashed",
      observed_at: "2026-06-06T09:12:03Z", assigned_worker_id: "worker-b",
      players: 0, cpu: 0, mem_gb: 0,
    },
    {
      id: "s-4", name: "minigames", server_type: "fabric", mc_version: "1.21.4",
      execution_backend: "container", game_port: 25571,
      desired_state: "stopped", observed_state: "stopped",
      observed_at: "2026-06-05T22:40:00Z", assigned_worker_id: null,
      players: 0, cpu: 0, mem_gb: 0,
    },
  ],

  members: [
    { membership_id: "m-1", user_id: "u-1", username: "miura", role_names: ["Owner"] },
    { membership_id: "m-2", user_id: "u-2", username: "alex_dig", role_names: ["Moderator"] },
    { membership_id: "m-3", user_id: "u-3", username: "steve_99", role_names: ["Builder"] },
    { membership_id: "m-4", user_id: "u-4", username: "enderchan", role_names: ["Builder", "Backup-op"] },
  ],

  roles: [
    { id: "r-1", name: "Owner", is_preset: true, permissions: "ALL" },
    { id: "r-2", name: "Moderator", is_preset: false,
      permissions: ["server:read","server:start","server:stop","server:restart","server:command","file:read","backup:read","backup:create","member:read","group:read","group:manage","audit:read"] },
    { id: "r-3", name: "Builder", is_preset: false,
      permissions: ["server:read","file:read","backup:read","member:read"] },
    { id: "r-4", name: "Backup-op", is_preset: false,
      permissions: ["server:read","backup:read","backup:create","backup:restore","backup:delete"] },
  ],

  grants: [
    { id: "g-1", user_id: "u-3", username: "steve_99", resource_type: "server", resource_id: "s-2",
      resource_name: "creative-build", permissions: ["server:start","server:stop","file:edit"] },
    { id: "g-2", user_id: "u-4", username: "enderchan", resource_type: "server", resource_id: "s-4",
      resource_name: "minigames", permissions: ["server:start","server:stop","server:restart","server:command"] },
  ],

  groups: [
    { id: "pg-1", name: "Admins", kind: "op",
      players: [{ uuid: "069a79f4", username: "miura" }, { uuid: "ec70bcaf", username: "alex_dig" }],
      servers: ["survival", "creative-build", "modded-forge"] },
    { id: "pg-2", name: "Regulars", kind: "whitelist",
      players: [{ uuid: "7125ba8b", username: "steve_99" }, { uuid: "c06f8906", username: "enderchan" }, { uuid: "61699b2e", username: "zombie_hunter" }],
      servers: ["survival"] },
  ],

  backups: [
    { id: "b-1", created_at: "2026-06-06 04:00", source: "scheduled", size: "412 MiB", created_by: "system" },
    { id: "b-2", created_at: "2026-06-05 21:14", source: "manual", size: "408 MiB", created_by: "miura" },
    { id: "b-3", created_at: "2026-06-05 04:00", source: "scheduled", size: "395 MiB", created_by: "system" },
    { id: "b-4", created_at: "2026-06-04 04:00", source: "scheduled", size: "390 MiB", created_by: "system" },
  ],

  files: [
    { name: "world", is_dir: true, size: null },
    { name: "plugins", is_dir: true, size: null },
    { name: "logs", is_dir: true, size: null },
    { name: "server.properties", is_dir: false, size: 1284 },
    { name: "bukkit.yml", is_dir: false, size: 942 },
    { name: "spigot.yml", is_dir: false, size: 3105 },
    { name: "paper-global.yml", is_dir: false, size: 5210 },
    { name: "eula.txt", is_dir: false, size: 158 },
    { name: "banned-players.json", is_dir: false, size: 2 },
    { name: "whitelist.json", is_dir: false, size: 412 },
  ],

  serverProperties: [
    "#Minecraft server properties",
    "#Fri Jun 06 04:00:11 UTC 2026",
    "enable-rcon=true",
    "rcon.port=25575",
    "rcon.password=********",
    "server-port=25565",
    "motd=\\u00a7aSakura SMP \\u00a77- survival",
    "max-players=20",
    "difficulty=hard",
    "view-distance=10",
    "simulation-distance=8",
    "white-list=true",
    "spawn-protection=16",
    "online-mode=true",
    "pvp=true",
  ].join("\n"),

  fileHistory: [
    { version_id: "v-9f3a21", at: "2026-06-06 09:41", by: "miura" },
    { version_id: "v-8e1c77", at: "2026-06-04 18:02", by: "alex_dig" },
    { version_id: "v-71b0d4", at: "2026-06-01 11:30", by: "miura" },
  ],

  auditRecords: [
    { id: "a-1", created_at: "2026-06-06 10:01:44", operation: "server:start", outcome: "success", actor: "miura", target: "server/creative-build" },
    { id: "a-2", created_at: "2026-06-06 09:58:12", operation: "server:restart", outcome: "success", actor: "alex_dig", target: "server/survival" },
    { id: "a-3", created_at: "2026-06-06 09:12:03", operation: "server:start", outcome: "failure", actor: "miura", target: "server/modded-forge" },
    { id: "a-4", created_at: "2026-06-05 21:14:50", operation: "backup:create", outcome: "success", actor: "miura", target: "server/survival" },
    { id: "a-5", created_at: "2026-06-05 19:03:22", operation: "file:edit", outcome: "success", actor: "alex_dig", target: "survival:/server.properties" },
    { id: "a-6", created_at: "2026-06-05 18:55:09", operation: "member:add", outcome: "success", actor: "miura", target: "user/enderchan" },
    { id: "a-7", created_at: "2026-06-05 18:54:41", operation: "role:manage", outcome: "success", actor: "miura", target: "role/Backup-op" },
    { id: "a-8", created_at: "2026-06-05 13:22:17", operation: "auth:login", outcome: "failure", actor: "steve_99", target: "—" },
  ],

  workers: [
    { id: "worker-a", version: "0.9.2", status: "online", drivers: ["container"],
      assigned: 2, max: 8, cpu_cores: 16, memory: "64 GiB", heartbeat: "2s ago", draining: false },
    { id: "worker-b", version: "0.9.2", status: "online", drivers: ["container"],
      assigned: 1, max: 4, cpu_cores: 8, memory: "32 GiB", heartbeat: "4s ago", draining: true },
    { id: "worker-c", version: "0.9.1", status: "offline", drivers: ["container"],
      assigned: 0, max: 4, cpu_cores: 8, memory: "16 GiB", heartbeat: "3h ago", draining: false },
  ],

  users: [
    { id: "u-1", username: "miura", email: "mmiura@example.com", is_platform_admin: true, active: true, created_at: "2026-04-02" },
    { id: "u-2", username: "alex_dig", email: "alex@example.com", is_platform_admin: false, active: true, created_at: "2026-04-10" },
    { id: "u-3", username: "steve_99", email: "steve@example.com", is_platform_admin: false, active: true, created_at: "2026-04-18" },
    { id: "u-4", username: "enderchan", email: "ender@example.com", is_platform_admin: false, active: true, created_at: "2026-05-02" },
    { id: "u-5", username: "old_timer", email: "old@example.com", is_platform_admin: false, active: false, created_at: "2026-04-03" },
  ],

  versionCatalog: {
    vanilla: { count: 87, refreshed: "12m ago", latest: "1.21.6" },
    paper: { count: 64, refreshed: "12m ago", latest: "1.21.6" },
    fabric: { count: 59, refreshed: "1h ago", latest: "1.21.6" },
    forge: { count: 48, refreshed: "1h ago", latest: "1.21.4" },
  },

  versionsByType: {
    vanilla: ["1.21.6", "1.21.5", "1.21.4", "1.21.3", "1.21.1", "1.20.6", "1.20.4", "1.20.1", "1.19.4", "1.18.2"],
    paper: ["1.21.6", "1.21.5", "1.21.4", "1.21.1", "1.20.6", "1.20.4", "1.20.1", "1.19.4"],
    fabric: ["1.21.6", "1.21.5", "1.21.4", "1.21.1", "1.20.6", "1.20.4"],
    forge: ["1.21.4", "1.21.1", "1.20.6", "1.20.1", "1.19.4", "1.18.2"],
  },

  logLines: [
    "[10:01:50 INFO]: Preparing level \"world\"",
    "[10:01:52 INFO]: Preparing start region for dimension minecraft:overworld",
    "[10:01:55 INFO]: Time elapsed: 2841 ms",
    "[10:01:55 INFO]: Done (5.102s)! For help, type \"help\"",
    "[10:01:55 INFO]: Starting remote control listener",
    "[10:01:55 INFO]: RCON running on 0.0.0.0:25575",
    "[10:02:14 INFO]: steve_99 joined the game",
    "[10:02:31 INFO]: alex_dig joined the game",
    "[10:03:02 INFO]: <steve_99> anyone up for the nether hub build?",
    "[10:03:18 INFO]: <alex_dig> gimme 5 min",
    "[10:04:40 WARN]: Can't keep up! Is the server overloaded? Running 2043ms behind",
    "[10:05:09 INFO]: enderchan joined the game",
    "[10:05:44 INFO]: <enderchan> o/",
    "[10:06:13 INFO]: steve_99 has made the advancement [Hot Stuff]",
  ],

  extraLogLines: [
    "[%T INFO]: <steve_99> brb",
    "[%T INFO]: Autosave started",
    "[%T INFO]: Autosave finished in 312 ms",
    "[%T INFO]: <alex_dig> nice",
    "[%T INFO]: zombie_hunter joined the game",
    "[%T WARN]: Can't keep up! Is the server overloaded? Running 2156ms behind",
    "[%T INFO]: <enderchan> check spawn",
    "[%T INFO]: steve_99 fell from a high place",
  ],

  permissionCatalog: {
    server: ["create", "read", "update", "delete", "start", "stop", "restart", "command"],
    file: ["read", "edit", "history", "rollback"],
    backup: ["create", "read", "restore", "delete", "schedule"],
    member: ["read", "add", "remove"],
    role: ["read", "manage"],
    grant: ["read", "manage"],
    group: ["read", "manage"],
    community: ["read", "update", "delete"],
    audit: ["read"],
  },
};
