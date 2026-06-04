"""SNKV-backed BaseKVStorage for LightRAG.

All KV namespaces share one ``snkv.db`` file (via snkv_shared) using
one column family per namespace.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, final

from lightrag.base import BaseKVStorage
from lightrag.utils import logger
from snkv import NotFoundError

from . import snkv_shared


@final
@dataclass
class SNKVKVStorage(BaseKVStorage):
    def __post_init__(self) -> None:
        working_dir = self.global_config["working_dir"]
        if self.workspace:
            db_dir = os.path.join(working_dir, self.workspace)
            self.final_namespace = f"{self.workspace}_{self.namespace}"
        else:
            db_dir = working_dir
            self.workspace = ""
            self.final_namespace = self.namespace

        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "snkv.db")
        self._cf_name = self.namespace
        self._shared: snkv_shared.SharedStore | None = None
        self._cf = None

    def _ex(self):
        return asyncio.get_running_loop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        self._shared = snkv_shared.acquire(self._db_path)

        def _open():
            try:
                self._cf = self._shared.kv.open_column_family(self._cf_name)
            except NotFoundError:
                self._cf = self._shared.kv.create_column_family(self._cf_name)

        await self._ex().run_in_executor(self._shared.executor, _open)

    async def finalize(self) -> None:
        if self._shared is None:
            return

        def _close():
            if self._cf is not None:
                try:
                    self._cf.close()
                except Exception:
                    pass
                self._cf = None

        await self._ex().run_in_executor(self._shared.executor, _close)
        snkv_shared.release(self._db_path)
        self._shared = None

    # ------------------------------------------------------------------
    # BaseKVStorage abstract methods
    # ------------------------------------------------------------------

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        def _get():
            raw = self._cf.get(id.encode())
            return json.loads(raw.decode()) if raw is not None else None

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any] | None]:
        def _get_many():
            out: list[dict[str, Any] | None] = []
            for doc_id in ids:
                raw = self._cf.get(doc_id.encode())
                out.append(json.loads(raw.decode()) if raw is not None else None)
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get_many)

    async def filter_keys(self, keys: set[str]) -> set[str]:
        def _filter():
            return {k for k in keys if not self._cf.exists(k.encode())}

        return await self._ex().run_in_executor(self._shared.executor, _filter)

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        if not data:
            return

        def _upsert():
            self._shared.kv.begin(write=True)
            try:
                for key, val in data.items():
                    self._cf.put(key.encode(), json.dumps(val, ensure_ascii=False).encode())
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _upsert)

    async def delete(self, ids: list[str]) -> None:
        if not ids:
            return

        def _delete():
            self._shared.kv.begin(write=True)
            try:
                for doc_id in ids:
                    try:
                        self._cf.delete(doc_id.encode())
                    except NotFoundError:
                        pass
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _delete)

    async def is_empty(self) -> bool:
        def _check():
            return self._cf.count() == 0

        return await self._ex().run_in_executor(self._shared.executor, _check)

    async def index_done_callback(self) -> None:
        def _sync():
            self._shared.kv.sync()

        await self._ex().run_in_executor(self._shared.executor, _sync)

    async def drop(self) -> dict[str, str]:
        def _drop():
            self._cf.clear()

        try:
            await self._ex().run_in_executor(self._shared.executor, _drop)
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            logger.error(f"[{self.workspace}] Error dropping {self.namespace}: {e}")
            return {"status": "error", "message": str(e)}
