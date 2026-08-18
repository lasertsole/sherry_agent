"""SNKV-backed BaseGraphStorage for LightRAG.

Shares ``snkv.db`` with KV and doc-status storage via snkv_shared.
Three column families: ``nodes``, ``edges``, ``adj``.
Edge keys are canonicalised as min(src,tgt)||max(src,tgt) for undirected semantics.
"""
from __future__ import annotations

import asyncio
import collections
import difflib
import json
import os
from dataclasses import dataclass
from typing import Any, final

from lightrag.base import BaseGraphStorage
from lightrag.types import KnowledgeGraph, KnowledgeGraphEdge, KnowledgeGraphNode
from lightrag.utils import logger
from snkv import NotFoundError

from . import snkv_shared

_SEP = "||"


def _edge_key(src: str, tgt: str) -> bytes:
    a, b = (src, tgt) if src <= tgt else (tgt, src)
    return f"{a}{_SEP}{b}".encode()


@final
@dataclass
class SNKVGraphStorage(BaseGraphStorage):
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
        self._shared: snkv_shared.SharedStore | None = None
        self._nodes_cf = None
        self._edges_cf = None
        self._adj_cf = None

    def _ex(self):
        return asyncio.get_running_loop()

    def _get_or_create(self, name: str):
        try:
            return self._shared.kv.open_column_family(name)
        except NotFoundError:
            return self._shared.kv.create_column_family(name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        self._shared = snkv_shared.acquire(self._db_path)

        def _open():
            self._nodes_cf = self._get_or_create("nodes")
            self._edges_cf = self._get_or_create("edges")
            self._adj_cf   = self._get_or_create("adj")

        await self._ex().run_in_executor(self._shared.executor, _open)

    async def finalize(self) -> None:
        if self._shared is None:
            return

        def _close():
            for cf in (self._nodes_cf, self._edges_cf, self._adj_cf):
                if cf is not None:
                    try:
                        cf.close()
                    except Exception:
                        pass
            self._nodes_cf = self._edges_cf = self._adj_cf = None

        await self._ex().run_in_executor(self._shared.executor, _close)
        snkv_shared.release(self._db_path)
        self._shared = None

    # ------------------------------------------------------------------
    # Adjacency helpers (called on executor thread)
    # ------------------------------------------------------------------

    def _get_adj(self, node_id: str) -> list[str]:
        raw = self._adj_cf.get(node_id.encode())
        return json.loads(raw.decode()) if raw is not None else []

    def _set_adj(self, node_id: str, neighbours: list[str]) -> None:
        if neighbours:
            self._adj_cf.put(node_id.encode(), json.dumps(neighbours).encode())
        else:
            try:
                self._adj_cf.delete(node_id.encode())
            except NotFoundError:
                pass

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    async def has_node(self, node_id: str) -> bool:
        def _has():
            return self._nodes_cf.exists(node_id.encode())

        return await self._ex().run_in_executor(self._shared.executor, _has)

    async def get_node(self, node_id: str) -> dict[str, str] | None:
        def _get():
            raw = self._nodes_cf.get(node_id.encode())
            return json.loads(raw.decode()) if raw is not None else None

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def upsert_node(self, node_id: str, node_data: dict[str, str]) -> None:
        def _upsert():
            self._nodes_cf.put(
                node_id.encode(),
                json.dumps(node_data, ensure_ascii=False).encode(),
            )

        await self._ex().run_in_executor(self._shared.executor, _upsert)

    async def delete_node(self, node_id: str) -> None:
        def _delete():
            # Pre-load all adj before the transaction (same pattern as remove_nodes).
            neighbours = self._get_adj(node_id)
            nb_adjs: dict[str, set[str]] = {
                nb: set(self._get_adj(nb)) for nb in neighbours
            }
            for adj in nb_adjs.values():
                adj.discard(node_id)

            self._shared.kv.begin(write=True)
            try:
                for nb, adj in nb_adjs.items():
                    self._set_adj(nb, list(adj))
                    try:
                        self._edges_cf.delete(_edge_key(node_id, nb))
                    except NotFoundError:
                        pass
                # Delete node's own adj entry and node record.
                try:
                    self._adj_cf.delete(node_id.encode())
                except NotFoundError:
                    pass
                try:
                    self._nodes_cf.delete(node_id.encode())
                except NotFoundError:
                    pass
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _delete)

    async def upsert_nodes_batch(self, nodes: list[tuple[str, dict[str, str]]]) -> None:
        def _upsert_batch():
            self._shared.kv.begin(write=True)
            try:
                for node_id, node_data in nodes:
                    self._nodes_cf.put(
                        node_id.encode(),
                        json.dumps(node_data, ensure_ascii=False).encode(),
                    )
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _upsert_batch)

    async def has_nodes_batch(self, node_ids: list[str]) -> set[str]:
        def _has_batch():
            return {nid for nid in node_ids if self._nodes_cf.exists(nid.encode())}

        return await self._ex().run_in_executor(self._shared.executor, _has_batch)

    async def get_nodes_batch(self, node_ids: list[str]) -> dict[str, dict]:
        def _get_batch():
            out: dict[str, dict] = {}
            for nid in node_ids:
                raw = self._nodes_cf.get(nid.encode())
                if raw is not None:
                    out[nid] = json.loads(raw.decode())
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get_batch)

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        def _has():
            return self._edges_cf.exists(_edge_key(source_node_id, target_node_id))

        return await self._ex().run_in_executor(self._shared.executor, _has)

    async def get_edge(self, source_node_id: str, target_node_id: str) -> dict[str, str] | None:
        def _get():
            raw = self._edges_cf.get(_edge_key(source_node_id, target_node_id))
            return json.loads(raw.decode()) if raw is not None else None

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def get_node_edges(self, source_node_id: str) -> list[tuple[str, str]] | None:
        def _get():
            if not self._nodes_cf.exists(source_node_id.encode()):
                return None
            return [(source_node_id, nb) for nb in self._get_adj(source_node_id)]

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str]
    ) -> None:
        def _upsert():
            # Pre-load adj so all reads happen before the write transaction.
            src_adj = set(self._get_adj(source_node_id))
            tgt_adj = set(self._get_adj(target_node_id))
            src_adj.add(target_node_id)
            tgt_adj.add(source_node_id)

            self._shared.kv.begin(write=True)
            try:
                self._edges_cf.put(
                    _edge_key(source_node_id, target_node_id),
                    json.dumps(edge_data, ensure_ascii=False).encode(),
                )
                self._set_adj(source_node_id, list(src_adj))
                self._set_adj(target_node_id, list(tgt_adj))
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _upsert)

    async def upsert_edges_batch(self, edges: list[tuple[str, str, dict[str, str]]]) -> None:
        def _upsert_batch():
            # Pre-load adj for all affected nodes in one pass before the transaction
            # to avoid read-within-transaction isolation issues.
            all_nodes: set[str] = set()
            for src, tgt, _ in edges:
                all_nodes.add(src)
                all_nodes.add(tgt)
            adj_map: dict[str, set[str]] = {
                nid: set(self._get_adj(nid)) for nid in all_nodes
            }
            for src, tgt, _ in edges:
                adj_map[src].add(tgt)
                adj_map[tgt].add(src)

            self._shared.kv.begin(write=True)
            try:
                for src, tgt, data in edges:
                    self._edges_cf.put(
                        _edge_key(src, tgt),
                        json.dumps(data, ensure_ascii=False).encode(),
                    )
                for nid, neighbours in adj_map.items():
                    self._set_adj(nid, list(neighbours))
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _upsert_batch)

    async def remove_nodes(self, nodes: list[str]) -> None:
        def _remove():
            nodes_set = set(nodes)

            # Pre-collect adj for deleted nodes so we know which edges to delete
            # and which external neighbours need their adj updated.
            node_adj: dict[str, list[str]] = {nid: self._get_adj(nid) for nid in nodes}

            # Find external neighbours and pre-load their adj lists.
            external: set[str] = {
                nb
                for nid, nbs in node_adj.items()
                for nb in nbs
                if nb not in nodes_set
            }
            ext_adj: dict[str, set[str]] = {
                nb: set(self._get_adj(nb)) for nb in external
            }

            # Apply removals in-memory.
            for node_id in nodes:
                for nb in node_adj[node_id]:
                    if nb in ext_adj:
                        ext_adj[nb].discard(node_id)

            self._shared.kv.begin(write=True)
            try:
                for node_id in nodes:
                    # Delete all edges incident to this node.
                    for nb in node_adj[node_id]:
                        try:
                            self._edges_cf.delete(_edge_key(node_id, nb))
                        except NotFoundError:
                            pass
                    try:
                        self._adj_cf.delete(node_id.encode())
                    except NotFoundError:
                        pass
                    try:
                        self._nodes_cf.delete(node_id.encode())
                    except NotFoundError:
                        pass
                # Write updated adj for external neighbours.
                for nb, neighbours in ext_adj.items():
                    self._set_adj(nb, list(neighbours))
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _remove)

    async def remove_edges(self, edges: list[tuple[str, str]]) -> None:
        def _remove():
            # Pre-load adj for all affected nodes before the transaction.
            all_nodes: set[str] = set()
            for src, tgt in edges:
                all_nodes.add(src)
                all_nodes.add(tgt)
            adj_map: dict[str, set[str]] = {
                nid: set(self._get_adj(nid)) for nid in all_nodes
            }
            for src, tgt in edges:
                adj_map[src].discard(tgt)
                adj_map[tgt].discard(src)

            self._shared.kv.begin(write=True)
            try:
                for src, tgt in edges:
                    try:
                        self._edges_cf.delete(_edge_key(src, tgt))
                    except NotFoundError:
                        pass
                for nid, neighbours in adj_map.items():
                    self._set_adj(nid, list(neighbours))
                self._shared.kv.commit()
            except Exception:
                self._shared.kv.rollback()
                raise

        await self._ex().run_in_executor(self._shared.executor, _remove)

    async def get_edges_batch(self, pairs: list[dict[str, str]]) -> dict[tuple[str, str], dict]:
        def _get_batch():
            out: dict[tuple[str, str], dict] = {}
            for pair in pairs:
                src = pair.get("source_node_id", pair.get("src", ""))
                tgt = pair.get("target_node_id", pair.get("tgt", ""))
                raw = self._edges_cf.get(_edge_key(src, tgt))
                if raw is not None:
                    out[(src, tgt)] = json.loads(raw.decode())
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get_batch)

    async def get_nodes_edges_batch(
        self, node_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        def _get_batch():
            out: dict[str, list[tuple[str, str]]] = {}
            for nid in node_ids:
                if self._nodes_cf.exists(nid.encode()):
                    out[nid] = [(nid, nb) for nb in self._get_adj(nid)]
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get_batch)

    # ------------------------------------------------------------------
    # Degree operations
    # ------------------------------------------------------------------

    async def node_degree(self, node_id: str) -> int:
        def _deg():
            return len(self._get_adj(node_id))

        return await self._ex().run_in_executor(self._shared.executor, _deg)

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        def _deg():
            return len(self._get_adj(src_id)) + len(self._get_adj(tgt_id))

        return await self._ex().run_in_executor(self._shared.executor, _deg)

    async def node_degrees_batch(self, node_ids: list[str]) -> dict[str, int]:
        def _deg_batch():
            return {nid: len(self._get_adj(nid)) for nid in node_ids}

        return await self._ex().run_in_executor(self._shared.executor, _deg_batch)

    async def edge_degrees_batch(
        self, edge_pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
        def _deg_batch():
            return {
                (src, tgt): len(self._get_adj(src)) + len(self._get_adj(tgt))
                for src, tgt in edge_pairs
            }

        return await self._ex().run_in_executor(self._shared.executor, _deg_batch)

    # ------------------------------------------------------------------
    # Label / search operations
    # ------------------------------------------------------------------

    async def get_all_labels(self) -> list[str]:
        def _get():
            labels = []
            with self._nodes_cf.iterator() as it:
                for key_b, _ in it:
                    labels.append(key_b.decode())
            labels.sort()
            return labels

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def get_popular_labels(self, limit: int = 300) -> list[str]:
        def _get():
            pairs: list[tuple[int, str]] = []
            with self._nodes_cf.iterator() as it:
                for key_b, _ in it:
                    nid = key_b.decode()
                    pairs.append((len(self._get_adj(nid)), nid))
            pairs.sort(key=lambda x: x[0], reverse=True)
            return [p[1] for p in pairs[:limit]]

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def search_labels(self, query: str, limit: int = 50) -> list[str]:
        def _search():
            all_labels: list[str] = []
            with self._nodes_cf.iterator() as it:
                for key_b, _ in it:
                    all_labels.append(key_b.decode())

            q_lower = query.lower()
            exact = [lb for lb in all_labels if q_lower in lb.lower()]
            if len(exact) >= limit:
                return sorted(exact)[:limit]
            fuzzy = difflib.get_close_matches(query, all_labels, n=limit, cutoff=0.4)
            return list(dict.fromkeys(exact + fuzzy))[:limit]

        return await self._ex().run_in_executor(self._shared.executor, _search)

    # ------------------------------------------------------------------
    # All-data operations
    # ------------------------------------------------------------------

    async def get_all_nodes(self) -> list[dict]:
        def _get():
            out = []
            with self._nodes_cf.iterator() as it:
                for key_b, val_b in it:
                    try:
                        props = json.loads(val_b.decode())
                        props["id"] = key_b.decode()
                        out.append(props)
                    except Exception:
                        pass
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def get_all_edges(self) -> list[dict]:
        def _get():
            out = []
            with self._edges_cf.iterator() as it:
                for key_b, val_b in it:
                    try:
                        props = json.loads(val_b.decode())
                        parts = key_b.decode().split(_SEP, 1)
                        if len(parts) == 2:
                            props.setdefault("src_id", parts[0])
                            props.setdefault("tgt_id", parts[1])
                        out.append(props)
                    except Exception:
                        pass
            return out

        return await self._ex().run_in_executor(self._shared.executor, _get)

    async def get_knowledge_graph(
        self, node_label: str, max_depth: int = 3, max_nodes: int = 1000
    ) -> KnowledgeGraph:
        def _bfs() -> KnowledgeGraph:
            if node_label == "*":
                seeds: list[str] = []
                with self._nodes_cf.iterator() as it:
                    for key_b, _ in it:
                        seeds.append(key_b.decode())
            else:
                q_lower = node_label.lower()
                seeds = []
                with self._nodes_cf.iterator() as it:
                    for key_b, _ in it:
                        if q_lower in key_b.decode().lower():
                            seeds.append(key_b.decode())

            visited_nodes: set[str] = set()
            visited_edges: set[tuple[str, str]] = set()
            queue: collections.deque[tuple[str, int]] = collections.deque((s, 0) for s in seeds)
            is_truncated = False
            kg_nodes: list[KnowledgeGraphNode] = []
            kg_edges: list[KnowledgeGraphEdge] = []

            while queue:
                node_id, depth = queue.popleft()
                if node_id in visited_nodes:
                    continue
                if len(visited_nodes) >= max_nodes:
                    is_truncated = True
                    break
                visited_nodes.add(node_id)
                raw = self._nodes_cf.get(node_id.encode())
                props = json.loads(raw.decode()) if raw is not None else {}
                entity_type = props.get("entity_type", "")
                kg_nodes.append(KnowledgeGraphNode(
                    id=node_id,
                    labels=[entity_type] if entity_type else [],
                    properties=props,
                ))
                if depth < max_depth:
                    for nb in self._get_adj(node_id):
                        canon = (min(node_id, nb), max(node_id, nb))
                        if canon not in visited_edges:
                            visited_edges.add(canon)
                            raw_e = self._edges_cf.get(_edge_key(node_id, nb))
                            if raw_e is not None:
                                edata = json.loads(raw_e.decode())
                                kg_edges.append(KnowledgeGraphEdge(
                                    id=f"{canon[0]}{_SEP}{canon[1]}",
                                    type=edata.get("relation_type", edata.get("keywords")),
                                    source=canon[0],
                                    target=canon[1],
                                    properties=edata,
                                ))
                        if nb not in visited_nodes:
                            queue.append((nb, depth + 1))

            return KnowledgeGraph(nodes=kg_nodes, edges=kg_edges, is_truncated=is_truncated)

        return await self._ex().run_in_executor(self._shared.executor, _bfs)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    async def index_done_callback(self) -> None:
        def _sync():
            self._shared.kv.sync()

        await self._ex().run_in_executor(self._shared.executor, _sync)

    async def drop(self) -> dict[str, str]:
        def _drop():
            for cf in (self._nodes_cf, self._edges_cf, self._adj_cf):
                if cf is not None:
                    cf.clear()

        try:
            await self._ex().run_in_executor(self._shared.executor, _drop)
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            logger.error(f"[{self.workspace}] Error dropping graph: {e}")
            return {"status": "error", "message": str(e)}
