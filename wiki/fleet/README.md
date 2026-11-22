# 🏢 Fleet Deployment: One Sherry Cell per Tenant

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The Fleet deployment runs one Sherry backend per tenant and puts a single Ingress in front. Each cell keeps its own `SHERRY_HOME`, workspace and SQLite stores; the Ingress owns browser login and forwards only authenticated traffic. Two backends share that surface: a bare-metal **process** tier for trusted tenants and a **container** tier (Docker or Podman) that adds OS-level filesystem, network and cgroup isolation for tenants you do not trust. Cells never listen on a public interface.

**Source of truth:** `fleet/`, `packaging/MULTI_TENANT.md`, `tests/fleet/`.

## 🎯 What it is

The deployment has three layers. A browser talks only to the Ingress (`python -m fleet serve`, default port 8081). The Ingress authenticates the user, resolves the tenant from the JWT claims and reverse-proxies HTTP and WebSocket traffic to that tenant's cell. A cell is either an ordinary `python -m server` process or a container running the same server image, bound to `127.0.0.1` on a port from 20100-20299, started by the Fleet manager with a per-cell gateway token. Tenant state is one directory tree — `SHERRY_HOME`, `workspace/`, `sherry.jsonc`, `logs/` — and both backends mount or point at the same tree, so a tenant can move between them.

## 🏗️ Topology

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  process  :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  container :20101
                             │                      /app/data ← <fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

The Ingress and the cells run as the same OS user. The process tier is the trusted-tenant tier: the boundaries are process state, ports and routing, not a kernel sandbox. The container tier adds a separate mount namespace, its own bridge network and hard CPU/memory/pids caps.

## 🚪 Ingress: authentication and the browser contract

The Ingress owns every `/auth/*` route and never proxies `/auth/token`, so a cell's per-boot gateway token cannot leak to a browser. Login sets two HttpOnly cookies (`sherry_ingress_access`, `sherry_ingress_refresh`). The access token is a short-lived HS256 JWT carrying `tenants` (default first), `tenant_id` and `role`; the refresh token rotates on every use and is revocable server-side.

The shared SPA sees the same contract as a standalone cell, with one difference: the Ingress login is `username` + password (see the multi-user section below), so `/auth/status` also reports `username_required: true`. `/auth/me` heals an expired access cookie from a valid refresh cookie; unsafe requests pass an Origin and `Sec-Fetch-Site` CSRF check.

Setting `SHERRY_INGRESS_TLS_CERT` and `SHERRY_INGRESS_TLS_KEY` makes the Ingress terminate TLS and marks both cookies `Secure`; `SHERRY_INGRESS_ALLOWED_ORIGINS` adds reverse-proxy origins. Static assets are public, and every `Accept: text/html` GET receives `200.html` or `index.html`, so `/login` and deep routes load before a session exists. API calls stay gated.

## 👥 Multi-user capability: accounts, cells and switching

The Ingress is the only component that knows about people; a cell never sees a username. Accounts are **administrator-provisioned** — there is no self-service signup — and live in the Ingress database (`users` plus the many-to-many `user_tenants`). The SPA always shows the login form at the Ingress (`/auth/status` answers `auth_required: true`, `username_required: true`, `has_account: true`), `POST /auth/login` takes `{username, password}` and answers `expires_in`, and a failed attempt is limited per `(client address, username)`: five in 300 s answer `429`, while an unknown username still pays a dummy hash verification so timing does not leak existence. An account with no membership is refused with `403 no_tenant`.

| Concept | Where it lives | What it means |
| :--- | :--- | :--- |
| Account | Ingress `users` | A username + scrypt hash — the only credential a person holds. Password change is self-service (`PUT /auth/account`); enabling or disabling the login and deleting an account are administrator actions and answer `managed_by_admin`. |
| `role` | `users.role` / `user_tenants.role` | Recorded and reported (`GET /auth/tenants`, `/auth/me`), but **not enforced**: authorization is membership-based, so `owner` and `member` currently reach the same cell identically. Treat the label as metadata. |
| Cell (trust domain) | One `tenant_id` | One workspace, one memory store, one set of sessions and model settings. Everything inside is shared by every member of the cell. |
| Membership | `user_tenants(user_id, tenant_id, role, is_default)` | Authorizes one account for one cell. Many-to-many: one account may belong to several cells, and one cell may serve several accounts. |

**What is shared, what is per account.** Members of a cell share its sessions, chat history, memory (FACTS / MEMORY / USER), skills, project directory and model settings — that is what a trust domain is for. Credentials, refresh-token rotation and the active-cell choice are per account/per browser: two accounts never see each other's password, but they do see each other's conversations, because the conversation belongs to the cell.

**Switching and revocation.** Login selects the account's default membership and sets an HttpOnly `sherry_tenant` cookie beside the auth cookies. `GET /auth/tenants` lists every membership with `role`, `is_default` and live `cell_status`; `POST /auth/tenant` validates the requested `tenant_id` against the live table and rewrites the cookie. The proxy and the WebSocket router re-check the membership on every request, so `user-tenant-remove` takes effect immediately — a stale cookie falls back to the account's default cell. The SPA's `TenantSwitcher` appears only above one membership and reloads the page after a switch, because cached sessions, memory and sockets belong to the previous cell.

**Provisioning quickstart.**

```bash
python -m fleet create --tenant team-a                     # add --backend container for OS isolation
python -m fleet user-add --username alice --password '...' --tenant team-a
python -m fleet user-tenant-add --username bob --tenant team-a --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username bob --tenant team-a
```

The first membership is the owner and the account's default. `rm` deletes a cell but leaves its memberships, so revoke or reassign them first.

**Isolation between users.** There is none *inside* a cell — the cell is the isolation unit. Give two people separate cells when they must not see each other's sessions, and use the container tier when those cells belong to untrusted tenants (it adds the filesystem, network and cgroup boundary). A container cell runs with its own login disabled at rest (`SHERRY_AUTH_ENABLED=false` plus an enforced flip, see the container section), because Ingress traffic reaches it from the bridge gateway rather than loopback — the login that matters is the Ingress's.

## 🧩 Cell lifecycle

`python -m fleet create --tenant alice` builds `<fleet_root>/alice/{workspace,logs,sherry.jsonc}` and allocates the first free port in 20100-20299. Port selection and the registry write are serialized by a process lock plus a fleet-root `flock`, so two CLI processes cannot register the same port. `--backend process` (the default) records `runtime=process`; `--backend container` records `runtime=container` with the image and the container name, and every later command reads that column instead of asking for the backend again.

`start` injects `SHERRY_HOME`, `SHERRY_PROJECT_DIR`, `API_PORT`, `SHERRY_GATEWAY_TOKEN`, `PUBLIC_BASE_URL`, `SHERRY_DAEMON_HOME` and `SHERRY_DAEMON_CLIENT_ONLY`, spawns the server detached and polls `/health` until the budget expires.

`stop` signals only a PID whose environment still names the cell's `SHERRY_HOME`; a reused or foreign PID is left alone. `create` also accepts `--seed-config` to copy the host's `.models` and `.network` files (off by default, because those files carry credentials).

## 📦 Container tier (OS-level isolation)

`python -m fleet create --tenant alice --backend container --image sherry-agent:dev` keeps the same data tree and the same Ingress route, but the cell runs in its own container:

```text
--name sherry-cell-alice        --init          --restart unless-stopped
--cap-drop=ALL                  --security-opt no-new-privileges
--memory 4g --cpus 2            --pids-limit 1024
--network sherry-cell-alice-net --volume <fleet_root>/alice:/app/data
-p 127.0.0.1:20100:8080         --add-host host.docker.internal:host-gateway
--label sherry.fleet.tenant=alice --label sherry.fleet.attempt=<id>
```

A 0600 env file carries `SHERRY_HOME=/app/data`, `SHERRY_PROJECT_DIR=/app/data/workspace`, the gateway token, `API_PORT`, `SHERRY_AUTH_ENABLED=false`, `PUBLIC_BASE_URL` and `LOCAL_DAEMON_HOST=host.docker.internal`; it is deleted as soon as `create` has consumed it. The container image is the repository Dockerfile unchanged, so `docker build` or `podman build` is the only image step.

The health probe is `/health` **plus** the attempt label: a container that answers on the port but carries a different `sherry.fleet.attempt` is treated as a stale leftover, so a crashed container that the engine restarted cannot masquerade as the new one.

**Two network modes.** `SHERRY_FLEET_CONTAINER_NETWORK=publish` (the default on Linux and macOS) creates one bridge network per tenant and publishes `127.0.0.1:<port>`; containers cannot reach each other. `host` shares the runtime's network namespace, and the cell listens on its assigned port directly — this is the WSL2 fallback, because the WSL2 kernel ships without nftables support and netavark cannot apply the publish rules (`auto` picks `host` on a Windows host and `publish` elsewhere). Host mode trades network isolation for portability; keep `publish` where the kernel allows it.

**The cell login is off by construction.** Ingress traffic reaches a container from the bridge gateway, not from loopback, so a cell that enforced its own login would answer `401` to every proxied request. A fresh cell seeds `auth_settings.enabled=0` from `SHERRY_AUTH_ENABLED=false`. A cell **restored from a backup** can bring an enabled login back; the manager detects it through `/auth/status` and flips the row at rest in `<data_dir>/src/data/auth.db`, giving the cell's 2 s state cache time to expire. If the login survives that, `create`/`start` fails loudly instead of leaving a cell nobody can reach.

**Upgrade and audit.** `python -m fleet upgrade --tenant alice --image <tag>` stops the container, recreates it on the new image against the same data tree and rolls back to the previous image if the new one fails its health probe. `python -m fleet doctor [--tenant alice]` walks every container cell and checks: the container exists and runs, `cap-drop` removed the engine's core capability set (podman reports the expanded list, docker reports `ALL`), `no-new-privileges` is set, memory/CPU/pids limits are non-zero, the fleet labels match the tenant, the publish binding is `127.0.0.1` only or the network mode is `host`, the tenant network holds a single container, the data directory is `0700` (POSIX) and `/health` answers. A failed check makes the command exit 1.

## 🛡️ Supervision and resource limits

| Symptom | Supervisor response |
| :--- | :--- |
| The recorded PID is gone or its `SHERRY_HOME` no longer matches | Reap the stale child and start a replacement |
| The process is alive but `/health` fails three consecutive probes | `stop`, then `start` |
| A stopped cell leaves an unreaped zombie | `waitpid` collects it before a replacement binds the port |
| `logs/cell.log` exceeds `SHERRY_FLEET_LOG_MAX_BYTES` | Copy-truncate to `cell.log.1`, safe for the running O_APPEND descriptor |
| The cell exceeds `SHERRY_FLEET_CELL_MEMORY_MB` or `SHERRY_FLEET_CELL_NOFILE` | The exec wrapper applied `ulimit`; CPU quota remains a cgroup or systemd concern |
| A container cell is exited or missing | `start` recreates and starts it (the data tree is untouched) |
| A container cell runs but `/health` fails three probes | `stop`, then `start` |

The supervisor runs inside the Ingress and probes each running cell every `SHERRY_FLEET_SUPERVISE_INTERVAL_S` seconds. `SHERRY_FLEET_SUPERVISE=0` disables the loop for an operator-managed deployment.

## 💾 Backup and restore

`python -m fleet backup --tenant alice --out <dir>` copies the cell tree while skipping `logs/`, `-wal` and `-shm`, and snapshots every `.db` through `sqlite3.Connection.backup()`, so SQLite files are transactionally consistent. Non-database files are best-effort while the cell keeps running. The container tier uses the same host-side tree, so backup and restore do not change.

`restore` stops the cell, stages the backup, renames the current tree to `<tenant>.pre-restore` and swaps the staged copy in. `python -m fleet backup-db --out <dir>` backs up the central `fleet.db` (users, refresh tokens, cells and process rows).

## 🤝 Shared model daemons

`python -m fleet daemons-start` owns one copy of each local model daemon (embed, reranker, local chat, vision) under `<fleet_root>/daemons`. Cells run with `SHERRY_DAEMON_CLIENT_ONLY=1`: they connect to the recorded pid, never spawn a second copy and never register an atexit kill. `SHERRY_DAEMON_HOME` points every cell at the shared pid directory.

Local chat and vision also need `SHERRY_FLEET_AUXILIARY_MODEL_PATH`, `SHERRY_FLEET_VISION_MODEL_PATH` and `SHERRY_FLEET_VISION_MMPROJ_PATH`. Without them the command reports the missing variable instead of starting a daemon that can never load. A daemon that is down makes its client fall back to the in-process loader, so functionality survives but per-cell memory is no longer shared.

Container cells reach the host daemons through `host.docker.internal`, so the daemons must listen on an address the runtime can route to (`LOCAL_DAEMON_HOST=0.0.0.0` on the host). In host-network mode on a VM-backed runtime the container's "host" is the VM, not the desktop — keep local models on process cells or a Linux host.

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice                                  # process backend
python -m fleet create --tenant bob --backend container --image sherry-agent:dev
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet upgrade --tenant bob --image sherry-agent:next          # container only
python -m fleet doctor --tenant bob                                    # container audits
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
| Tenant files (process) | `SHERRY_HOME` separates data trees, but every cell shares one OS user; a hostile tenant needs the container tier |
| Tenant files (container) | A private mount namespace over the tenant's tree, `cap-drop=ALL`, `no-new-privileges` and hard memory/CPU/pids caps; `terminal` and `python_repl` cannot read another tenant's data |
| Container network | `publish` mode gives each tenant its own bridge and a loopback-only published port; containers cannot reach each other |
| Resource exhaustion | A container OOM or pid storm is killed inside its own cgroup and does not touch the Ingress or other cells |

- Accounts on the same tenant share that cell's sessions and memory; per-user isolation requires separate tenants.
- A container cell still trusts its tenant inside the container: the boundary is tenant-vs-tenant, not user-vs-user.

## 🧪 Tests

`tests/fleet/` covers the store, Ingress auth, proxy, WebSocket and TLS, manager lifecycle, shared daemons and backup/restore. `test_cell_e2e.py` starts a real `python -m server` cell through the manager and verifies `/health`, PID identity, login, HTTP proxying and a clean SIGTERM.

The container backend adds `test_container_manager.py` (a recording fake runtime: command shape, env file, both network modes, health failure cleanup, upgrade rollback, doctor findings, supervision and the restored-login flip) and `test_container_runtime_e2e.py`, an opt-in smoke test that asks a real engine to `create` the cell container:

```bash
SHERRY_FLEET_CONTAINER_E2E=1 uv run --no-sync pytest tests/fleet/test_container_runtime_e2e.py
```

`pytest tests/fleet` and the repository split gate are the regression commands. Client authentication has its own store, login-page, guard and account-panel suites, and the full backend gate runs the unit, integration and regression groups in separate processes.

## 📌 Operational requirements

- Run the Ingress under systemd or supervisord: its internal supervisor manages cells, not the Ingress itself.
- Build the SPA same-origin with `VITE_API_BACK_URL=same-origin`, and set `PUBLIC_BASE_URL` to the browser-visible Ingress origin.
- Set a stable `SHERRY_INGRESS_JWT_SECRET`; a random per-boot secret invalidates every cookie on restart.
- Terminate TLS at the Ingress or a trusted proxy, and list extra origins in `SHERRY_INGRESS_ALLOWED_ORIGINS`.
- Add cgroup or systemd CPU quotas and external backup or logrotate timers for limits the manager does not enforce.
- Bind a project directory per tenant; an unset project directory makes two tenants share `ROOT_DIR/.sherry/coordination.db`.
- Give every account at least one membership; a login without one answers `no_tenant`.
- The container tier needs a Docker or Podman binary on the Ingress host (`SHERRY_FLEET_CONTAINER_RUNTIME`), a built image (`SHERRY_FLEET_IMAGE`, `SHERRY_FLEET_CONTAINER_PULL`), and roughly 1 GB per idle cell on top of the image; set `SHERRY_FLEET_CONTAINER_MEMORY` / `_CPUS` / `_PIDS` to the host's real capacity.
- On a POSIX host the container runs as the fleet user's uid, so the `0700` data tree stays writable without a chown; a different runtime uid needs `--user` support wired into the image or a rootless subuid mapping.
