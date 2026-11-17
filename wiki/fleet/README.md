# 🏢 Bare-Metal Fleet: One Sherry Process per Tenant

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The Fleet deployment runs one normal Sherry backend per tenant and puts a single Ingress in front. Each cell keeps its own `SHERRY_HOME`, workspace and SQLite stores; the Ingress owns browser login and forwards only authenticated traffic. No Docker is required, and cells never listen on a public interface.

**Source of truth:** `fleet/`, `packaging/MULTI_TENANT.md`, `tests/fleet/`.

## 🎯 What it is

The deployment has three layers. A browser talks only to the Ingress (`python -m fleet serve`, default port 8081). The Ingress authenticates the user, resolves the tenant from the JWT claims and reverse-proxies HTTP and WebSocket traffic to that tenant's cell. A cell is an ordinary `python -m server` process bound to `127.0.0.1` on a port from 20100-20299, started detached by the Fleet manager with a per-cell gateway token. Tenant state is one directory tree: `SHERRY_HOME`, `workspace/`, `sherry.jsonc` and `logs/`.

## 🏗️ Topology

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  python -m server :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  python -m server :20101
                             │                      SHERRY_HOME=<fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

The Ingress and the cells run as the same OS user. This is the trusted-tenant tier: the boundaries are process state, ports and routing, not a kernel sandbox.

## 🚪 Ingress: authentication and the browser contract

The Ingress owns every `/auth/*` route and never proxies `/auth/token`, so a cell's per-boot gateway token cannot leak to a browser. Login sets two HttpOnly cookies (`sherry_ingress_access`, `sherry_ingress_refresh`). The access token is a short-lived HS256 JWT carrying `tenants` (default first), `tenant_id` and `role`; the refresh token rotates on every use and is revocable server-side.

The shared SPA sees the same contract as a standalone cell. `/auth/status` also reports `username_required`; `/auth/login` accepts `{username, password}` and returns `expires_in`; `/auth/me` heals an expired access cookie from a valid refresh cookie; `/auth/account` changes a password while enable/disable stays administrator-only. Unsafe requests pass an Origin and `Sec-Fetch-Site` CSRF check. Five failed logins per `(client, username)` in 300 s answer `429`.

Setting `SHERRY_INGRESS_TLS_CERT` and `SHERRY_INGRESS_TLS_KEY` makes the Ingress terminate TLS and marks both cookies `Secure`; `SHERRY_INGRESS_ALLOWED_ORIGINS` adds reverse-proxy origins. Static assets are public, and every `Accept: text/html` GET receives `200.html` or `index.html`, so `/login` and deep routes load before a session exists. API calls stay gated.

## 👥 Users, trust domains and cell switching

A cell is a trust domain, not a user account: any number of accounts may point at the same `tenant_id` and therefore share that cell's sessions, memory, skills and project directory. Credentials stay per account in the Ingress database; changing one password does not touch another. One account may also belong to several cells, held in the many-to-many `user_tenants` table (the legacy `users.tenant_id` remains the default). `init()` rebuilds a legacy `users` table whose `tenant_id` was `UNIQUE` once, then backfills the memberships.

Login selects the account's default membership and sets an HttpOnly `sherry_tenant` cookie next to the auth cookies. `GET /auth/tenants` lists every membership with `role`, `is_default` and `cell_status`; `POST /auth/tenant` validates the requested `tenant_id` against the live table and rewrites the cookie. The proxy and WebSocket router read that cookie on every request and re-check the membership, so an administrator revoking a cell takes effect immediately; a stale cookie falls back to the account's default. The SPA's `TenantSwitcher` appears only when the account has more than one cell and reloads the page after a switch, because cached sessions, memory and sockets all belong to the previous cell.

A user with no membership cannot sign in (`no_tenant`). Use `user-tenant-add`, `user-tenants` and `user-tenant-remove` to manage access; the first membership is the owner and default. `rm` deletes a cell but leaves its memberships, so revoke or reassign them first.

## 🧩 Cell lifecycle

`python -m fleet create --tenant alice` builds `<fleet_root>/alice/{workspace,logs,sherry.jsonc}` and allocates the first free port in 20100-20299. Port selection and the registry write are serialized by a process lock plus a fleet-root `flock`, so two CLI processes cannot register the same port. `start` injects `SHERRY_HOME`, `SHERRY_PROJECT_DIR`, `API_PORT`, `SHERRY_GATEWAY_TOKEN`, `PUBLIC_BASE_URL`, `SHERRY_DAEMON_HOME` and `SHERRY_DAEMON_CLIENT_ONLY`, spawns the server detached and polls `/health` until the budget expires.

`stop` signals only a PID whose environment still names the cell's `SHERRY_HOME`; a reused or foreign PID is left alone. `create` also accepts `--seed-config` to copy the host's `.models` and `.network` files (off by default, because those files carry credentials).

## 🛡️ Supervision and resource limits

| Symptom | Supervisor response |
| :--- | :--- |
| The recorded PID is gone or its `SHERRY_HOME` no longer matches | Reap the stale child and start a replacement |
| The process is alive but `/health` fails three consecutive probes | `stop`, then `start` |
| A stopped cell leaves an unreaped zombie | `waitpid` collects it before a replacement binds the port |
| `logs/cell.log` exceeds `SHERRY_FLEET_LOG_MAX_BYTES` | Copy-truncate to `cell.log.1`, safe for the running O_APPEND descriptor |
| The cell exceeds `SHERRY_FLEET_CELL_MEMORY_MB` or `SHERRY_FLEET_CELL_NOFILE` | The exec wrapper applied `ulimit`; CPU quota remains a cgroup or systemd concern |

The supervisor runs inside the Ingress and probes each running cell every `SHERRY_FLEET_SUPERVISE_INTERVAL_S` seconds. `SHERRY_FLEET_SUPERVISE=0` disables the loop for an operator-managed deployment.

## 💾 Backup and restore

`python -m fleet backup --tenant alice --out <dir>` copies the cell tree while skipping `logs/`, `-wal` and `-shm`, and snapshots every `.db` through `sqlite3.Connection.backup()`, so SQLite files are transactionally consistent. Non-database files are best-effort while the cell keeps running.

`restore` stops the cell, stages the backup, renames the current tree to `<tenant>.pre-restore` and swaps the staged copy in. `python -m fleet backup-db --out <dir>` backs up the central `fleet.db` (users, refresh tokens, cells and process rows).

## 🤝 Shared model daemons

`python -m fleet daemons-start` owns one copy of each local model daemon (embed, reranker, local chat, vision) under `<fleet_root>/daemons`. Cells run with `SHERRY_DAEMON_CLIENT_ONLY=1`: they connect to the recorded pid, never spawn a second copy and never register an atexit kill. `SHERRY_DAEMON_HOME` points every cell at the shared pid directory.

Local chat and vision also need `SHERRY_FLEET_AUXILIARY_MODEL_PATH`, `SHERRY_FLEET_VISION_MODEL_PATH` and `SHERRY_FLEET_VISION_MMPROJ_PATH`. Without them the command reports the missing variable instead of starting a daemon that can never load. A daemon that is down makes its client fall back to the in-process loader, so functionality survives but per-cell memory is no longer shared.

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 Security boundary

| Boundary | What the deployment guarantees |
| :--- | :--- |
| Browser credential | HttpOnly, SameSite=Lax cookies; `/auth/token` is a 404 at the Ingress |
| Cell credential | A per-cell `SHERRY_GATEWAY_TOKEN`; browser-supplied token and ticket values are stripped |
| Cell network | Cells bind `127.0.0.1` only; the Ingress reaches them over loopback |
| Tenant files | `SHERRY_HOME` separates data trees, but every cell shares one OS user; a hostile tenant needs the P3 container tier |

- Accounts on the same tenant share that cell's sessions and memory; per-user isolation requires separate tenants.

## 🧪 Tests

`tests/fleet/` covers the store, Ingress auth, proxy, WebSocket and TLS, manager lifecycle, shared daemons and backup/restore. `test_cell_e2e.py` starts a real `python -m server` cell through the manager and verifies `/health`, PID identity, login, HTTP proxying and a clean SIGTERM.

`pytest tests/fleet` and the repository split gate are the regression commands. Client authentication has its own store, login-page, guard and account-panel suites, and the full backend gate runs the unit, integration and regression groups in separate processes.

## 📌 Operational requirements

- Run the Ingress under systemd or supervisord: its internal supervisor manages cells, not the Ingress itself.
- Build the SPA same-origin with `VITE_API_BACK_URL=same-origin`, and set `PUBLIC_BASE_URL` to the browser-visible Ingress origin.
- Set a stable `SHERRY_INGRESS_JWT_SECRET`; a random per-boot secret invalidates every cookie on restart.
- Terminate TLS at the Ingress or a trusted proxy, and list extra origins in `SHERRY_INGRESS_ALLOWED_ORIGINS`.
- Add cgroup or systemd CPU quotas and external backup or logrotate timers for limits the manager does not enforce.
- Bind a project directory per tenant; an unset project directory makes two tenants share `ROOT_DIR/.sherry/coordination.db`.
- Give every account at least one membership; a login without one answers `no_tenant`.
