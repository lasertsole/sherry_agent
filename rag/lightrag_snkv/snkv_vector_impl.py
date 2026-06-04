"""SNKV-backed BaseVectorStorage for LightRAG.

All vector namespaces (entities, relationships, chunks) share one
``snkv_vec.db`` file via snkv_shared.  Each namespace uses six
namespace-prefixed column families and an in-memory usearch HNSW index.
A ``snkv_vec.{namespace}.usearch`` sidecar is written on close to skip
the O(n*d) rebuild on the next open.

No dependency on snkv.vector.VectorStore — usearch is managed directly.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from dataclasses import dataclass
from typing import Any, final

import numpy as np

try:
    from usearch.index import Index as UsearchIndex
except ImportError as exc:
    raise ImportError(
        "usearch is required for SNKVVectorStorage.\n"
        "Install with:  pip install snkv[vector]"
    ) from exc

from lightrag.base import BaseVectorStorage
from lightrag.utils import compute_mdhash_id, logger
from snkv import NotFoundError

from . import snkv_shared


def _pack_i64(n: int) -> bytes:
    return struct.pack(">q", n)


def _unpack_i64(b: bytes) -> int:
    return struct.unpack(">q", b)[0]


def _get_or_create_cf(kv, name: str):
    try:
        return kv.open_column_family(name)
    except NotFoundError:
        return kv.create_column_family(name)


@final
@dataclass
class SNKVVectorStorage(BaseVectorStorage):
    def __post_init__(self) -> None:
        self._validate_embedding_func()

        kwargs = self.global_config.get("vector_db_storage_cls_kwargs", {})
        cosine_threshold = kwargs.get("cosine_better_than_threshold")
        if cosine_threshold is None:
            raise ValueError(
                "cosine_better_than_threshold must be set in vector_db_storage_cls_kwargs"
            )
        self.cosine_better_than_threshold = cosine_threshold

        working_dir = self.global_config["working_dir"]
        if self.workspace:
            db_dir = os.path.join(working_dir, self.workspace)
            self.final_namespace = f"{self.workspace}_{self.namespace}"
        else:
            db_dir = working_dir
            self.workspace = ""
            self.final_namespace = self.namespace

        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "snkv_vec.db")
        self._sidecar_path = os.path.join(db_dir, f"snkv_vec.{self.namespace}.usearch")
        self._dim = self.embedding_func.embedding_dim
        self._max_batch_size = self.global_config["embedding_batch_num"]

        ns = self.namespace
        self._cf_val_name  = f"vec_val_{ns}"   # key → JSON payload
        self._cf_raw_name  = f"vec_raw_{ns}"   # key → float32 bytes
        self._cf_idk_name  = f"vec_idk_{ns}"   # key → int64 usearch label
        self._cf_idi_name  = f"vec_idi_{ns}"   # int64 → key
        self._cf_meta_name = f"vec_meta_{ns}"  # config (next_id)
        self._cf_rev_name  = f"vec_rev_{ns}"   # entity_name → JSON list of relation keys

        self._shared: snkv_shared.SharedStore | None = None
        self._val_cf  = None
        self._raw_cf  = None
        self._idk_cf  = None
        self._idi_cf  = None
        self._meta_cf = None
        self._rev_cf  = None
        self._index: UsearchIndex | None = None
        self._next_id: int = 0

    def _ex(self):
        return asyncio.get_running_loop()

    # ------------------------------------------------------------------
    # Reverse-index helpers (sync, called on executor thread)
    # ------------------------------------------------------------------

    def _get_rev(self, entity_name: str) -> list[str]:
        raw = self._rev_cf.get(entity_name.encode())
        return json.loads(raw.decode()) if raw else []

    def _write_rev_map(self, rev_map: dict[str, list[str]]) -> None:
        """Write a pre-computed rev_map dict inside an open transaction."""
        for entity_name, keys_list in rev_map.items():
            if keys_list:
                self._rev_cf.put(entity_name.encode(), json.dumps(keys_list).encode())
            else:
                try:
                    self._rev_cf.delete(entity_name.encode())
                except NotFoundError:
                    pass

    # ------------------------------------------------------------------
    # Sync open/close (run on shared executor thread)
    # ------------------------------------------------------------------

    def _open_store(self) -> None:
        kv = self._shared.kv
        self._val_cf  = _get_or_create_cf(kv, self._cf_val_name)
        self._raw_cf  = _get_or_create_cf(kv, self._cf_raw_name)
        self._idk_cf  = _get_or_create_cf(kv, self._cf_idk_name)
        self._idi_cf  = _get_or_create_cf(kv, self._cf_idi_name)
        self._meta_cf = _get_or_create_cf(kv, self._cf_meta_name)
        self._rev_cf  = _get_or_create_cf(kv, self._cf_rev_name)

        stored_nid = self._meta_cf.get(b"next_id")
        self._next_id = _unpack_i64(stored_nid) if stored_nid else 0

        self._index = UsearchIndex(ndim=self._dim, metric="cos", dtype="f32")
        self._index.expansion_search = 64

        # Try sidecar fast-path
        if os.path.exists(self._sidecar_path):
            nid_path = self._sidecar_path + ".nid"
            try:
                with open(nid_path, "rb") as f:
                    sidecar_nid = _unpack_i64(f.read(8))
                if sidecar_nid == self._next_id:
                    candidate = UsearchIndex.restore(self._sidecar_path)
                    if candidate.ndim == self._dim:
                        self._index = candidate
                        self._index.expansion_search = 64
                        return
            except Exception:
                pass
            # Stale or corrupt — remove and fall through to rebuild
            for p in (self._sidecar_path, nid_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

        # Rebuild index from raw CF
        if self._raw_cf.count() > 0:
            ids: list[int] = []
            vecs: list[np.ndarray] = []
            with self._raw_cf.iterator() as it:
                for key_b, vec_bytes in it:
                    id_raw = self._idk_cf.get(key_b)
                    if id_raw is not None:
                        ids.append(_unpack_i64(id_raw))
                        vecs.append(np.frombuffer(vec_bytes, dtype=np.float32))
            if ids:
                self._index.add(np.array(ids, dtype=np.uint64), np.stack(vecs))

    def _close_store(self) -> None:
        if self._index is not None and self._sidecar_path:
            try:
                self._index.save(self._sidecar_path)
                with open(self._sidecar_path + ".nid", "wb") as f:
                    f.write(_pack_i64(self._next_id))
            except Exception:
                for p in (self._sidecar_path, self._sidecar_path + ".nid"):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

        for cf in (self._val_cf, self._raw_cf, self._idk_cf, self._idi_cf, self._meta_cf, self._rev_cf):
            if cf is not None:
                try:
                    cf.close()
                except Exception:
                    pass
        self._val_cf = self._raw_cf = self._idk_cf = self._idi_cf = self._meta_cf = self._rev_cf = None
        self._index = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        self._shared = snkv_shared.acquire(self._db_path)
        await self._ex().run_in_executor(self._shared.executor, self._open_store)

    async def finalize(self) -> None:
        if self._shared is None:
            return
        await self._ex().run_in_executor(self._shared.executor, self._close_store)
        snkv_shared.release(self._db_path)
        self._shared = None

    # ------------------------------------------------------------------
    # BaseVectorStorage abstract methods
    # ------------------------------------------------------------------

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        if not data:
            return

        current_time = int(time.time())
        keys = list(data.keys())
        contents = [v["content"] for v in data.values()]

        batches = [
            contents[i : i + self._max_batch_size]
            for i in range(0, len(contents), self._max_batch_size)
        ]
        embeddings_list = await asyncio.gather(*[self.embedding_func(b) for b in batches])
        embeddings = np.concatenate(embeddings_list)

        if len(embeddings) != len(keys):
            logger.error(
                f"[{self.workspace}] embedding count mismatch "
                f"{len(embeddings)} != {len(keys)} for {self.namespace}"
            )
            return

        def _batch_put():
            kv = self._shared.kv

            # Pre-read phase — must happen before the transaction to avoid
            # read-within-write isolation issues in SQLite.
            pre_ids: dict[bytes, int | None] = {}
            pre_entity_pairs: dict[bytes, tuple[str | None, str | None]] = {}

            for key in keys:
                key_b = key.encode()
                old_raw = self._idk_cf.get(key_b)
                pre_ids[key_b] = _unpack_i64(old_raw) if old_raw is not None else None

                old_val_b = self._val_cf.get(key_b)
                if old_val_b is not None:
                    try:
                        old_val = json.loads(old_val_b.decode())
                        pre_entity_pairs[key_b] = (old_val.get("src_id"), old_val.get("tgt_id"))
                    except Exception:
                        pre_entity_pairs[key_b] = (None, None)
                else:
                    pre_entity_pairs[key_b] = (None, None)

            # New src/tgt pairs from incoming data
            new_entity_pairs: dict[bytes, tuple[str | None, str | None]] = {
                key.encode(): (val.get("src_id"), val.get("tgt_id"))
                for key, val in zip(keys, data.values())
            }

            # Collect all entity names that need reverse index changes
            affected: set[str] = set()
            for key_b in new_entity_pairs:
                for e in (*pre_entity_pairs.get(key_b, (None, None)),
                           *new_entity_pairs[key_b]):
                    if e:
                        affected.add(e)

            # Pre-load all affected reverse index entries
            rev_map: dict[str, list[str]] = {e: self._get_rev(e) for e in affected}

            # Apply changes in-memory
            for key_b, (new_src, new_tgt) in new_entity_pairs.items():
                key_str = key_b.decode()
                old_src, old_tgt = pre_entity_pairs.get(key_b, (None, None))

                # Remove from old entities' rev lists
                for old_e in (old_src, old_tgt):
                    if old_e and old_e in rev_map:
                        try:
                            rev_map[old_e].remove(key_str)
                        except ValueError:
                            pass

                # Add to new entities' rev lists (deduplicated)
                for new_e in (new_src, new_tgt):
                    if new_e:
                        lst = rev_map.setdefault(new_e, [])
                        if key_str not in lst:
                            lst.append(key_str)

            base_id = self._next_id
            new_ids: dict[bytes, int] = {
                key.encode(): base_id + i for i, key in enumerate(keys)
            }

            kv.begin(write=True)
            try:
                for key, val, emb in zip(keys, data.values(), embeddings):
                    key_b = key.encode()
                    new_id = new_ids[key_b]
                    old_id = pre_ids[key_b]

                    stored = {
                        "__created_at__": current_time,
                        **{k: v for k, v in val.items() if k in self.meta_fields},
                        "content": val.get("content", ""),
                    }
                    self._val_cf.put(key_b, json.dumps(stored, ensure_ascii=False).encode())
                    self._raw_cf.put(key_b, emb.astype(np.float32).tobytes())
                    self._idk_cf.put(key_b, _pack_i64(new_id))
                    self._idi_cf.put(_pack_i64(new_id), key_b)
                    if old_id is not None:
                        try:
                            self._idi_cf.delete(_pack_i64(old_id))
                        except NotFoundError:
                            pass

                self._write_rev_map(rev_map)
                self._next_id = base_id + len(keys)
                self._meta_cf.put(b"next_id", _pack_i64(self._next_id))
                kv.commit()
            except Exception:
                self._next_id = base_id
                kv.rollback()
                raise

            # Post-commit: update usearch index
            for key_b, old_id in pre_ids.items():
                if old_id is not None:
                    try:
                        self._index.remove(old_id)
                    except Exception:
                        pass

            ids_arr  = np.array(list(new_ids.values()), dtype=np.uint64)
            vecs_arr = np.stack([emb.astype(np.float32) for emb in embeddings])
            cap = self._index.capacity
            if cap > 0 and (len(self._index) + len(ids_arr)) / cap >= 0.9:
                self._index.reserve(max(cap * 2, len(self._index) + len(ids_arr) + 1))
            self._index.add(ids_arr, vecs_arr)

        await self._ex().run_in_executor(self._shared.executor, _batch_put)

    async def query(self, query: str, top_k: int, query_embedding: list[float] = None) -> list[dict[str, Any]]:
        if query_embedding is not None:
            q_vec = np.asarray(query_embedding, dtype=np.float32)
        else:
            emb = await self.embedding_func([query], _priority=5)
            q_vec = np.asarray(emb[0], dtype=np.float32)

        max_dist = 1.0 - self.cosine_better_than_threshold

        def _search():
            if self._index is None or len(self._index) == 0:
                return []

            matches = self._index.search(q_vec.reshape(1, -1), top_k)
            labels  = np.asarray(matches.keys).ravel()
            dists   = np.asarray(matches.distances).ravel()

            out = []
            for label, dist in zip(labels, dists):
                if dist > max_dist:
                    continue
                key_b = self._idi_cf.get(_pack_i64(int(label)))
                if key_b is None:
                    continue
                val_b = self._val_cf.get(key_b)
                if val_b is None:
                    continue
                try:
                    val = json.loads(val_b.decode())
                except Exception:
                    val = {}
                out.append({
                    "id": key_b.decode(),
                    "distance": 1.0 - dist,
                    "created_at": val.pop("__created_at__", None),
                    **val,
                })
            return out

        return await self._ex().run_in_executor(self._shared.executor, _search)

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        def _get():
            raw = self._val_cf.get(id.encode())
            if raw is None:
                return None
            val = json.loads(raw.decode())
            return {"id": id, "created_at": val.pop("__created_at__", None), **val}

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any] | None]:
        def _get_many():
            out: list[dict[str, Any] | None] = []
            for id_str in ids:
                raw = self._val_cf.get(id_str.encode())
                if raw is None:
                    out.append(None)
                else:
                    val = json.loads(raw.decode())
                    out.append({"id": id_str, "created_at": val.pop("__created_at__", None), **val})
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get_many)

    async def delete(self, ids: list[str]) -> None:
        if not ids:
            return

        def _delete():
            # Pre-read phase
            pre_data: list[tuple[bytes, int, str | None, str | None]] = []
            for id_str in ids:
                key_b = id_str.encode()
                id_raw = self._idk_cf.get(key_b)
                if id_raw is None:
                    continue
                int_id = _unpack_i64(id_raw)
                src_id = tgt_id = None
                val_b = self._val_cf.get(key_b)
                if val_b is not None:
                    try:
                        val = json.loads(val_b.decode())
                        src_id = val.get("src_id")
                        tgt_id = val.get("tgt_id")
                    except Exception:
                        pass
                pre_data.append((key_b, int_id, src_id, tgt_id))

            if not pre_data:
                return

            # Pre-load reverse index for affected entities
            affected: set[str] = set()
            for _, _, src, tgt in pre_data:
                if src:
                    affected.add(src)
                if tgt:
                    affected.add(tgt)
            rev_map: dict[str, list[str]] = {e: self._get_rev(e) for e in affected}

            # Apply removals in-memory
            for key_b, _, src, tgt in pre_data:
                key_str = key_b.decode()
                for e in (src, tgt):
                    if e and e in rev_map:
                        try:
                            rev_map[e].remove(key_str)
                        except ValueError:
                            pass

            # Single transaction for all deletes + reverse index updates
            self._shared.kv.begin(write=True)
            try:
                for key_b, int_id, _, _ in pre_data:
                    for cf in (self._val_cf, self._raw_cf, self._idk_cf):
                        try:
                            cf.delete(key_b)
                        except NotFoundError:
                            pass
                    try:
                        self._idi_cf.delete(_pack_i64(int_id))
                    except NotFoundError:
                        pass
                self._write_rev_map(rev_map)
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

            # Post-commit: remove from usearch
            for _, int_id, _, _ in pre_data:
                try:
                    self._index.remove(int_id)
                except Exception:
                    pass

        await self._ex().run_in_executor(self._shared.executor, _delete)

    async def delete_entity(self, entity_name: str) -> None:
        entity_id = compute_mdhash_id(entity_name, prefix="ent-")

        def _delete():
            key_b = entity_id.encode()
            id_raw = self._idk_cf.get(key_b)
            if id_raw is None:
                return
            int_id = _unpack_i64(id_raw)
            self._shared.kv.begin(write=True)
            try:
                for cf in (self._val_cf, self._raw_cf, self._idk_cf):
                    try:
                        cf.delete(key_b)
                    except NotFoundError:
                        pass
                try:
                    self._idi_cf.delete(_pack_i64(int_id))
                except NotFoundError:
                    pass
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise
            try:
                self._index.remove(int_id)
            except Exception:
                pass

        await self._ex().run_in_executor(self._shared.executor, _delete)

    async def delete_entity_relation(self, entity_name: str) -> None:
        def _delete_relations():
            # O(1) lookup via reverse index instead of O(n) full scan
            keys_to_delete = self._get_rev(entity_name)
            if not keys_to_delete:
                return

            # Pre-read int_ids and cross-entity pairs
            pre_data: list[tuple[bytes, int, str | None, str | None]] = []
            for key_str in keys_to_delete:
                key_b = key_str.encode()
                id_raw = self._idk_cf.get(key_b)
                if id_raw is None:
                    continue
                int_id = _unpack_i64(id_raw)
                src_id = tgt_id = None
                val_b = self._val_cf.get(key_b)
                if val_b is not None:
                    try:
                        val = json.loads(val_b.decode())
                        src_id = val.get("src_id")
                        tgt_id = val.get("tgt_id")
                    except Exception:
                        pass
                pre_data.append((key_b, int_id, src_id, tgt_id))

            if not pre_data:
                # Stale reverse index entry — clean it up
                self._shared.kv.begin(write=True)
                try:
                    try:
                        self._rev_cf.delete(entity_name.encode())
                    except NotFoundError:
                        pass
                    self._shared.kv.commit()
                except Exception:
                    self._shared.kv.rollback()
                return

            # Pre-load reverse index for ALL affected entities (including entity_name
            # itself and any other entities that share these relation keys)
            affected: set[str] = set()
            for _, _, src, tgt in pre_data:
                if src:
                    affected.add(src)
                if tgt:
                    affected.add(tgt)
            rev_map: dict[str, list[str]] = {e: self._get_rev(e) for e in affected}

            # Apply removals in-memory
            for key_b, _, src, tgt in pre_data:
                key_str = key_b.decode()
                for e in (src, tgt):
                    if e and e in rev_map:
                        try:
                            rev_map[e].remove(key_str)
                        except ValueError:
                            pass

            # Single transaction: delete vectors + update all reverse indices
            self._shared.kv.begin(write=True)
            try:
                for key_b, int_id, _, _ in pre_data:
                    for cf in (self._val_cf, self._raw_cf, self._idk_cf):
                        try:
                            cf.delete(key_b)
                        except NotFoundError:
                            pass
                    try:
                        self._idi_cf.delete(_pack_i64(int_id))
                    except NotFoundError:
                        pass
                self._write_rev_map(rev_map)
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

            # Post-commit: remove from usearch
            for _, int_id, _, _ in pre_data:
                try:
                    self._index.remove(int_id)
                except Exception:
                    pass

            logger.debug(
                f"[{self.workspace}] deleted {len(pre_data)} relations for {entity_name}"
            )

        await self._ex().run_in_executor(self._shared.executor, _delete_relations)

    async def get_vectors_by_ids(self, ids: list[str]) -> dict[str, list[float]]:
        def _get_vecs():
            out: dict[str, list[float]] = {}
            for id_str in ids:
                raw = self._raw_cf.get(id_str.encode())
                if raw is not None:
                    out[id_str] = np.frombuffer(raw, dtype=np.float32).tolist()
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get_vecs)

    async def index_done_callback(self) -> None:
        def _sync():
            self._shared.kv.sync()

        await self._ex().run_in_executor(self._shared.executor, _sync)

    async def drop(self) -> dict[str, str]:
        def _drop():
            for cf in (self._val_cf, self._raw_cf, self._idk_cf, self._idi_cf,
                       self._meta_cf, self._rev_cf):
                if cf is not None:
                    cf.clear()
            self._next_id = 0
            self._index = UsearchIndex(ndim=self._dim, metric="cos", dtype="f32")
            self._index.expansion_search = 64
            for p in (self._sidecar_path, self._sidecar_path + ".nid"):
                try:
                    os.unlink(p)
                except OSError:
                    pass

        try:
            await self._ex().run_in_executor(self._shared.executor, _drop)
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            logger.error(f"[{self.workspace}] Error dropping {self.namespace}: {e}")
            return {"status": "error", "message": str(e)}
