# Multi-tenant deployment: per-tenant project directories

`SHERRY_HOME` isolates every writable store under `APP_HOME/src` (sessions,
memory, skills, `.env`, `sherry.jsonc`, the SQLite databases). Two paths do NOT
follow it by design, so a multi-tenant deployment MUST set the project
directory per tenant:

1. **`<project>/.sherry/coordination.db`** is per-PROJECT coordination state
   (`coordination_db_path(project_root)`), not per-user state. Two tenants that
   bind different project directories are isolated automatically.
2. **The unconfigured project-directory fallback is `ROOT_DIR`** (the code
   checkout), preserving the pre-project-binding behaviour. If no project
   directory is configured, every tenant falls back to the same code directory
   and therefore shares `ROOT_DIR/.sherry/coordination.db`.

Setting `SHERRY_PROJECT_DIR` (or `project_dir` in `sherry.jsonc`) is therefore a
deployment requirement, not an optional convenience. No code change is needed:
the data model is already correct.

## Users and cells

A cell is a trust domain, not a user account. Several Ingress accounts may
share one `tenant_id` and therefore the same workspace, sessions, memory and
project directory; their credentials remain independent. An account may also
belong to several cells. Per-user isolation requires separate tenants (separate
cells and workspaces), and untrusted tenants should run on the container backend
(`python -m fleet create --backend container`), which adds the filesystem,
network and cgroup boundary the process backend does not have.

Accounts are administrator-provisioned in the Ingress database; the cell never
sees a username. `role` (`owner` / `member`) is recorded and reported by
`GET /auth/tenants`, but authorization is membership-based — the label does not
change what a member can reach today.

Manage memberships with:

```bash
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
```

The first membership is the owner and the account's default.

## Fleet cell layout

```text
<host>/fleet/cells/<tenant>/
├── sherry.jsonc              # optional; may instead use SHERRY_PROJECT_DIR
└── workspace/                # per-tenant project root
```

Container environment (mount `<host>/fleet/cells/<tenant>/` at `/app/data`):

```bash
SHERRY_HOME=/app/data
SHERRY_PROJECT_DIR=/app/data/workspace
SHERRY_GATEWAY_TOKEN=<per-cell-token>
SHERRY_AUTH_ENABLED=false
```

Or write `<host>/fleet/cells/<tenant>/sherry.jsonc`:

```jsonc
{
  "project_dir": "/app/data/workspace"
}
```

`SHERRY_AUTH_ENABLED=false` matters here: Ingress traffic reaches a container
from the bridge gateway rather than loopback, so a cell that enforced its own
login would answer `401` to every proxied request. A fresh cell seeds the switch
off; a cell restored from a backup that carried an enabled login is switched off
at rest by the manager (`fleet/container_manager.py`), which fails loudly if the
login survives. The login that matters for a multi-user deployment is the
Ingress one.

The container is created with:

1. the cell data directory and its `workspace/` subdirectory;
2. a per-cell `sherry.jsonc` (when env is not used);
3. the data mount `/app/data` and the env above.

## Bare-metal process layout

```bash
SHERRY_HOME=<fleet_root>/<tenant>
SHERRY_PROJECT_DIR=<fleet_root>/<tenant>/workspace
SHERRY_GATEWAY_TOKEN=<per-cell-token>
SHERRY_DAEMON_HOME=<fleet_root>/daemons
SHERRY_DAEMON_CLIENT_ONLY=1
```

The same `sherry.jsonc` alternative applies at
`<fleet_root>/<tenant>/sherry.jsonc`. `python -m fleet create` writes that
layout for you; `SHERRY_DAEMON_HOME` and `SHERRY_DAEMON_CLIENT_ONLY` make the
cell join the host's shared model daemons instead of spawning its own.

## Verification

After a tenant starts:

```bash
# The effective project root must be the tenant workspace, not the checkout.
curl -H "token: <token>" \
  "http://127.0.0.1:<port>/sessions/project?session_id=<sid>"

# The coordination store must live under the tenant workspace.
ls "<tenant-workspace>/.sherry/coordination.db"

# The shared checkout must not accumulate tenant coordination state.
test ! -e "<sherry-checkout>/.sherry/coordination.db"
```

## Deliberately unchanged

- `coordination_db_path()` keeps its per-project semantics.
- `resolve_default_project_dir()` keeps the `ROOT_DIR` fallback; the deployment
  requirement is surfaced instead of changing the historical behaviour.
- `.sherry/` is created lazily on the first message of a session-bound project
  directory, never merely because the process started.
