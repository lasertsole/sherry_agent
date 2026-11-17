"""``python -m fleet`` — Ingress server and small admin commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
from pathlib import Path

from loguru import logger

from .config import IngressConfig, load_config
from .daemons import FleetDaemons
from .ingress import create_app
from .manager import FleetManager
from .store import FleetStore


def build_ssl_context(config: IngressConfig) -> ssl.SSLContext | None:
    """Terminate TLS when cert+key are configured; None behind an external proxy."""
    if config.tls_cert is None or config.tls_key is None:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config.tls_cert, config.tls_key)
    return context


def _store() -> FleetStore:
    return FleetStore(load_config().db_path)


def _manager() -> FleetManager:
    config = load_config()
    return FleetManager(FleetStore(config.db_path), config)


async def _create(args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    result = await manager.create(
        args.tenant,
        port=args.port,
        start=not args.no_start,
        seed_config=args.seed_config,
    )
    print(json.dumps(result, ensure_ascii=False))  # noqa: T201


async def _start(args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    print(json.dumps(await manager.start(args.tenant), ensure_ascii=False))  # noqa: T201


async def _stop(args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    print(json.dumps(await manager.stop(args.tenant), ensure_ascii=False))  # noqa: T201


async def _remove(args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    await manager.remove(args.tenant)
    print(f"removed tenant {args.tenant}")  # noqa: T201


def _daemons_start(args: argparse.Namespace) -> None:
    result = FleetDaemons(load_config()).start(args.family)
    for item in result:
        print(json.dumps(item, ensure_ascii=False))  # noqa: T201


def _daemons_stop(args: argparse.Namespace) -> None:
    FleetDaemons(load_config()).stop(args.family)
    print("daemons stopped")  # noqa: T201


def _daemons_status(_args: argparse.Namespace) -> None:
    for item in FleetDaemons(load_config()).status():
        print(json.dumps(item, ensure_ascii=False))  # noqa: T201


async def _backup(args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    print(json.dumps(manager.backup(args.tenant, Path(args.out)), ensure_ascii=False))  # noqa: T201


async def _restore(args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    result = await manager.restore(args.tenant, Path(args.source))
    print(json.dumps(result, ensure_ascii=False))  # noqa: T201


def _backup_db(args: argparse.Namespace) -> None:
    print(  # noqa: T201
        json.dumps(_store().backup_database(Path(args.out)), ensure_ascii=False)
    )


async def _manager_list(_args: argparse.Namespace) -> None:
    manager = _manager()
    await manager.store.init()
    for cell in await manager.list():
        print(json.dumps(cell, ensure_ascii=False))  # noqa: T201


async def _user_add(args: argparse.Namespace) -> None:
    store = _store()
    await store.init()
    user = await store.create_user(
        args.username, args.password, tenant_id=args.tenant, role=args.role
    )
    print(f"created user {user['username']} (tenant={user['tenant_id']}, role={user['role']})")  # noqa: T201


async def _user_tenant_add(args: argparse.Namespace) -> None:
    store = _store()
    await store.init()
    user = await store.get_user_by_username(args.username)
    if user is None:
        print(f"unknown user {args.username}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1)
    try:
        await store.add_user_tenant(
            int(user["id"]), args.tenant, role=args.role, is_default=args.default
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc
    print(f"user {user['username']} -> tenant {args.tenant}")  # noqa: T201


async def _user_tenants(args: argparse.Namespace) -> None:
    store = _store()
    await store.init()
    user = await store.get_user_by_username(args.username)
    if user is None:
        print(f"unknown user {args.username}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1)
    rows = await store.list_user_tenants(int(user["id"]))
    print(json.dumps(rows, ensure_ascii=False))  # noqa: T201


async def _user_tenant_remove(args: argparse.Namespace) -> None:
    store = _store()
    await store.init()
    user = await store.get_user_by_username(args.username)
    if user is None:
        print(f"unknown user {args.username}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1)
    removed = await store.remove_user_tenant(int(user["id"]), args.tenant)
    if not removed:
        print(f"user {user['username']} is not a member of {args.tenant}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1)
    print(f"user {user['username']} -/-> tenant {args.tenant}")  # noqa: T201


async def _cell_set(args: argparse.Namespace) -> None:
    store = _store()
    await store.init()
    await store.upsert_cell(
        args.tenant,
        host=args.host,
        port=args.port,
        gateway_token=args.gateway_token,
    )
    print(f"cell {args.tenant} -> {args.host}:{args.port}")  # noqa: T201


async def _cell_list(_args: argparse.Namespace) -> None:
    store = _store()
    await store.init()
    for cell in await store.list_cells():
        print(f"{cell['tenant_id']}\t{cell['host']}:{cell['port']}")  # noqa: T201


def main() -> None:
    """Dispatch the Ingress server and admin subcommands."""
    parser = argparse.ArgumentParser(prog="python -m fleet")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the Ingress")
    serve.set_defaults(func=None)

    user = sub.add_parser("user-add", help="create an account")
    user.add_argument("--username", required=True)
    user.add_argument("--password", required=True)
    user.add_argument("--tenant", required=True)
    user.add_argument("--role", default="user")
    user.set_defaults(func=_user_add)

    add_tenant = sub.add_parser("user-tenant-add", help="authorize a user for another cell")
    add_tenant.add_argument("--username", required=True)
    add_tenant.add_argument("--tenant", required=True)
    add_tenant.add_argument("--role", choices=("owner", "member"), default="member")
    add_tenant.add_argument("--default", action="store_true")
    add_tenant.set_defaults(func=_user_tenant_add)

    user_tenants = sub.add_parser("user-tenants", help="list a user's cells")
    user_tenants.add_argument("--username", required=True)
    user_tenants.set_defaults(func=_user_tenants)

    remove_tenant = sub.add_parser("user-tenant-remove", help="revoke a user's access to a cell")
    remove_tenant.add_argument("--username", required=True)
    remove_tenant.add_argument("--tenant", required=True)
    remove_tenant.set_defaults(func=_user_tenant_remove)

    cell = sub.add_parser("cell-set", help="register/update a tenant cell")
    cell.add_argument("--tenant", required=True)
    cell.add_argument("--host", default="127.0.0.1")
    cell.add_argument("--port", type=int, required=True)
    cell.add_argument("--gateway-token", required=True)
    cell.set_defaults(func=_cell_set)

    listing = sub.add_parser("cells", help="list tenant cells")
    listing.set_defaults(func=_cell_list)

    create = sub.add_parser("create", help="create a tenant and (optionally) start it")
    create.add_argument("--tenant", required=True)
    create.add_argument("--port", type=int)
    create.add_argument("--no-start", action="store_true")
    create.add_argument("--seed-config", action="store_true")
    create.set_defaults(func=_create)

    for name, func, help_text in (
        ("start", _start, "start a tenant cell"),
        ("stop", _stop, "stop a tenant cell"),
        ("rm", _remove, "stop and delete a tenant cell"),
        ("list", _manager_list, "list tenants with process state"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--tenant", required=(name != "list"))
        command.set_defaults(func=func)

    backup = sub.add_parser("backup", help="back up one tenant")
    backup.add_argument("--tenant", required=True)
    backup.add_argument("--out", required=True)
    backup.set_defaults(func=_backup)

    backup_db = sub.add_parser("backup-db", help="back up the central fleet database")
    backup_db.add_argument("--out", required=True)
    backup_db.set_defaults(func=_backup_db)

    restore = sub.add_parser("restore", help="restore one tenant from a backup")
    restore.add_argument("--tenant", required=True)
    restore.add_argument("--from", dest="source", required=True)
    restore.set_defaults(func=_restore)

    for name, func, help_text in (
        ("daemons-start", _daemons_start, "start the shared model daemons"),
        ("daemons-stop", _daemons_stop, "stop the shared model daemons"),
        ("daemons-status", _daemons_status, "show shared daemon readiness"),
    ):
        command = sub.add_parser(name, help=help_text)
        if name == "daemons-start" or name == "daemons-stop":
            command.add_argument(
                "--family",
                action="append",
                choices=["embed", "reranker", "auxiliary", "vision"],
            )
        command.set_defaults(func=func)

    args = parser.parse_args()
    if args.command == "serve":
        import asyncio as _asyncio

        from aiohttp import web

        config = load_config()
        app = _asyncio.run(create_app(config))
        context = build_ssl_context(config)
        scheme = "https" if context else "http"
        logger.info("Ingress listening on {}://{}:{}", scheme, config.host, config.port)
        web.run_app(
            app,
            host=config.host,
            port=config.port,
            ssl_context=context,
        )
        return
    if args.command.startswith("daemons-") or args.command == "backup-db":
        args.func(args)
        return
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
