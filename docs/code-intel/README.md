# 🔎 Code Intel: Four-Layer Code Retrieval for Subagents

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The design-level companion to the [Subagent System README](../../agent/tools/subagent/README.md) — the runtime and API reference for the spawn pipeline, role tool policy, and the `sessions_*` tools — and to the [Subagent Design page](../subagent/README.md) — the two-axis role model (depth role × functional role). This page documents the code retrieval framework those roles use: the tree-sitter symbol index, ast-grep structural search and rewrite, the LSP precise-retrieval tools, and the embedding-backed semantic code search.

Source of truth: `agent/tools/code_intel/**`, `config/features/agent_side/code_intel.py`, `config/features/agent_side/code_intel_semantic.py`, `config/features/agent_side/ast_grep.py`, `config/features/agent_side/lsp.py`, `config/path.py`, `agent/tools/subagent/types/functional_role.py`, `agent/tools/subagent/spawn/core.py`, `agent/tools/subagent/spawn/system_prompt.py`, and `agent/tools/subagent/roles/definitions/librarian/AGENTS.md`. Every statement below was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Design Invariants](#-design-invariants)
- [Layer 1: Tree-sitter Symbol Index](#-layer-1-tree-sitter-symbol-index)
- [Layer 2: ast-grep Structural Search & Rewrite](#-layer-2-ast-grep-structural-search--rewrite)
- [Layer 3: LSP Precise Retrieval](#-layer-3-lsp-precise-retrieval)
- [Layer 4: Semantic Code Search](#-layer-4-semantic-code-search)
- [Role Model & Tool Surface](#-role-model--tool-surface)
- [Configuration](#-configuration)
- [Environment Capability Matrix](#-environment-capability-matrix)
- [Limitations & Failure Modes](#-limitations--failure-modes)
- [Test Map](#-test-map)
- [Related Documentation](#-related-documentation)

## 🎯 Overview

Code Intel answers code-understanding questions for subagents with four retrieval engines, each aimed at a different question shape:

| Layer | Question it answers | Tools | Gate |
|-------|--------------------|-------|------|
| **Tree-sitter symbol index** | Where is a symbol defined, and what calls it? | `explore`, `callers`, `callees`, `impact` | `researcher` + `librarian` |
| **ast-grep** | What code has this structural shape, and how do I rewrite it? | `ast_grep_search`, `ast_grep_rewrite` | every subagent |
| **LSP** | What does the type checker know about this position? | `lsp_goto_definition`, `lsp_find_references`, `lsp_workspace_symbol`, `lsp_call_hierarchy`, `lsp_rename`, `lsp_diagnostics`, `lsp_format`, `lsp_status` | `researcher` + `librarian` |
| **Semantic search** | Which code implements this concept? | `semantic_code_search` | `researcher` + `librarian` |

The engines compose into one workflow: `explore` locates a symbol and its call paths, `ast_grep_search` finds structural patterns across languages, the LSP tools resolve definitions and references with type awareness, and `semantic_code_search` retrieves by concept when no name matches. All four are built and injected at child-agent assembly time.

```text
child agent assembly (spawn/core.py)
  base tools → role policy (allow / deny / main_only gate)
    ├─ role ∈ CODE_INTEL_ROLES?  → + explore/callers/callees/impact/semantic_code_search
    │                              + the eight lsp_* tools
    └─ always                    → + ast_grep_search / ast_grep_rewrite
```

## 🧱 Design Invariants

1. **The main agent never sees these tools.** No code-intel or LSP builder is registered in `_MAIN_TOOLS_BUILDERS`; injection happens only inside `_build_child_agent`, after the role tool policy.
2. **The tool faces are role-gated.** The tree-sitter suite and all eight LSP tools go to `CODE_INTEL_ROLES` (`researcher`, `librarian`); ast-grep goes to every functional role because it is a core structural capability.
3. **Fail-open is the contract.** A missing binary, an unparseable file, an unavailable model, a timeout, or a refused request returns a JSON payload with an actionable message; no retrieval failure raises into the child turn.
4. **Everything is bounded by construction.** File counts, per-file bytes, build time, batches, result counts, path counts, pattern size, and opened files all have caps; the LSP manager adds an LRU concurrency limit and idle reaping.
5. **Writes are opt-in and containment-checked.** `ast_grep_rewrite` previews unless `dry_run=false`; `lsp_rename` and `lsp_format` preview unless called with the apply flag; every write path re-validates traversal and project-root containment before touching disk.
6. **No new model providers.** Semantic search reuses the existing `models/embed_model` and `models/reranker_model` wrappers; the only new storage is the SQLite index.
7. **Roots resolve per call.** `SHERRY_CODE_INTEL_ROOT`, `SHERRY_SG_ROOT`, and `SHERRY_LSP_ROOT` override the working root for drills and tests; without them the process cwd and the shared `resolve_project_path` gate apply.

## 🌳 Layer 1: Tree-sitter Symbol Index

Four modules implement the index: `agent/tools/code_intel/extract.py` (pure bytes → symbols and call sites), `agent/tools/code_intel/indexer.py` (SQLite persistence and incremental walking), `agent/tools/code_intel/query.py` (matching and call-graph queries), and `agent/tools/code_intel/tools.py` (the LangChain wrappers).

| Grammar | Language keys | Extensions | Symbol nodes |
|---------|---------------|------------|--------------|
| Python | `python` | `.py` | `function_definition`, `class_definition`, `call` |
| TypeScript | `typescript`, `tsx` | `.ts`, `.tsx`, `.js`, `.jsx` | `function_declaration`, `class_declaration`, `method_definition`, `variable_declarator` (arrow and function expressions), `call_expression` |
| Rust | `rust` | `.rs` | `function_item`, `struct_item`, `enum_item`, `trait_item`, `impl_item`, `call_expression`, `macro_invocation` |
| Go | `go` | `.go` | `function_declaration`, `method_declaration`, `type_spec` (`struct_type` and `interface_type`), `call_expression` |

The node names and grammar versions were measured against the pinned packages — `tree-sitter 0.26.0`, `tree-sitter-python 0.25.0`, `tree-sitter-typescript 0.23.2`, `tree-sitter-rust 0.24.2`, `tree-sitter-go 0.25.0` — and parsers are constructed as `Parser(Language(capsule))`. The `javascript` alias maps to the `typescript` grammar and `jsx` to `tsx`. Three SQLite tables hold the result: `symbols`, `call_edges`, and `index_meta`. The call graph is resolved in a second pass by priority: same file, then same directory, then any global match, then a fuzzy Levenshtein match at distance ≤ 2.

Incrementality and bounds: every file's `mtime` and `size` are compared against `index_meta`; only changed files are re-parsed, removed files are dropped (including detaching inbound resolved edges), and writes are batched in transactions of 100 files. The walk stops at `code_intel_index_max_files` (5000) or `code_intel_index_timeout_s` (60) and reports `truncated`. Fail-open is per file: a syntax-error file, an oversized file (over 1 MB), an unknown extension, or a read error records an `index_meta` row and is skipped — a syntax-error file is never partially indexed. Mutating calls are serialized by an instance lock, and the connection uses WAL with a 30 s busy timeout.

Queries refresh the incremental index before they read, so a search after edits does not return stale symbols, and an unchanged tree is a cheap no-op. The four tools are:

| Tool | Input | Output | Bound |
|------|-------|--------|-------|
| `explore` | fuzzy concept, symbol name, or natural-language intent | matched symbols with source, docstring, immediate callers and callees; a suggestion when nothing matches | 10 symbols, 8000 source chars |
| `callers` | symbol name | every function or method that calls it, with call-site lines | exact name first, fuzzy fallback |
| `callees` | symbol name | every call the symbol emits, with the resolved target file and line when known | duplicate call sites collapsed |
| `impact` | symbol name | transitive callers (blast radius), with depth and the name reached through | depth 3, `truncated` flag |

## 🧬 Layer 2: ast-grep Structural Search & Rewrite

ast-grep treats the search pattern as code rather than regex, so `$NAME` matches one AST node and `$$$NAME` matches zero or more nodes. The implementation lives in `agent/tools/code_intel/ast_grep/`: `resolver.py` (binary discovery), `provisioner.py` (verified download), `install_hints.py` (recovery hints), and `runner.py` (the two tools). The standalone fallback installers are `scripts/install.sh` and `scripts/install.ps1` under the same directory.

Binary discovery is five-tiered, and every candidate must be a non-empty regular file that passes a short `--version` probe whose output contains `ast-grep`:

1. Explicit override — the `SHERRY_SG_PATH` environment variable.
2. Provisioned runtime — `~/.sherry/runtime/ast-grep/<platform>-<arch>/sg`.
3. Code-intel bin cache — `.codeintel/ast-grep/bin` (`ast-grep` then `sg`).
4. `PATH` lookup — `ast-grep` then `sg`, honoring the Windows `PATHEXT`.
5. Homebrew and Linuxbrew prefixes.

When no tier resolves, the first tool call auto-provisions the pinned `0.43.0` release: the per-platform URL and SHA-256 live in `config/features/agent_side/ast_grep.py`, the download is bounded by a 60 s timeout, a checksum mismatch is fatal (fail-closed — unverified bytes are never installed), and extraction uses the standard-library `zipfile` module because the host may not have `unzip`. The real `ast-grep` binary is preferred over the `sg` launcher, which re-execs relative to its own path and cannot run in every sandbox; the extracted binary is written atomically with mode 755. Resolution is cached per process while the file still exists.

Both tools run the `sg` CLI as a subprocess with a scrubbed environment and a 30 s timeout:

| Tool | Behavior | Safety default |
|------|----------|----------------|
| `ast_grep_search` | `sg run --json=stream` with `--strictness smart` by default; returns compact matches (file, 1-based line, text, meta-variables) | max 50 matches, 64 paths, 16 KiB pattern |
| `ast_grep_rewrite` | previews the replacement list; applies in place only with `dry_run=false` (`--update-all`) | `dry_run` defaults to `true` |

Every path is screened before the subprocess runs: without an override the canonical `resolve_project_path` gate is used, and with `SHERRY_SG_ROOT` set the same traversal predicate plus containment against that root applies — so an applying rewrite can only ever write inside the project.

## 🛰️ Layer 3: LSP Precise Retrieval

The LSP layer is `agent/tools/code_intel/lsp/`: `protocol.py` (URIs, 1-based to 0-based positions, result formatting), `resolver.py` (binary discovery), `installer.py` (allow-listed auto-install), `fallback.py` (availability to actionable message), `client.py` (JSON-RPC over stdio), `manager.py` (process lifecycle), and `tools.py` (the eight tools). The tools take `line` and `character` as 1-based values and convert to LSP-native 0-based positions internally.

| Tool | Purpose | Default behavior |
|------|---------|------------------|
| `lsp_goto_definition` | Type-aware jump to a definition | read-only |
| `lsp_find_references` | All references to a symbol | read-only |
| `lsp_workspace_symbol` | Fuzzy workspace symbol search | read-only, result count capped |
| `lsp_call_hierarchy` | Incoming callers or outgoing callees | read-only |
| `lsp_rename` | Workspace rename | previews a `WorkspaceEdit`; applies only with `dry_run=false` |
| `lsp_diagnostics` | Errors and warnings for a file | waits for the asynchronous `publishDiagnostics` notification and reports `timed_out` |
| `lsp_format` | Whole-file or range formatting | previews; applies only with `write=true`; reports `supported=false` instead of faking success |
| `lsp_status` | Honest availability of every configured server | starts nothing |

Discovery mirrors the ast-grep tiers with a marker-gated twist: an explicit absolute path, then repo-local bin directories trusted only when the matching marker file exists beside them (`pyproject.toml` to `.venv/bin`, `package.json` to `node_modules/.bin`, `Cargo.toml` to `target/debug`, `go.mod` to `bin`, walking up to the repository root), then the `~/.sherry/runtime/lsp` placement, then `PATH`, then Homebrew. Resolution is cached per `(cwd, command, platform)`.

A language server is a heavy resident subprocess, so the process-level manager bounds it: servers start lazily on the first request for a `(language, cwd)`, at most `lsp_max_concurrent_servers` (2) live at once with least-recently-used eviction, an idle sweeper reaps servers unused for `lsp_idle_shutdown_s` (300 s), and `shutdown_all` runs from an `atexit` hook — a client never leaves an orphan. Each client frames JSON-RPC with `Content-Length`, dispatches responses on a reader thread, answers server-to-client requests with an empty result, bounds every wait by a timeout, caps opened files at 32 (closing the oldest), and refuses files over 1 MB.

Availability is a three-state report: `available`, `not_installed` (configured but no binary passed discovery — an install hint and the local-install command are returned), or `not_configured` (language not in `lsp_enabled_languages`). Every unavailable path returns the tool name, the install hint, and a fallback to `explore` or `search_files`. Auto-install is off by default (`lsp_auto_install=False`); when enabled it executes only the allow-listed command for the requested language, with a 60 s timeout and a scrubbed environment.

## 🧠 Layer 4: Semantic Code Search

Semantic search embeds the Layer 1 symbol table instead of slicing arbitrary line windows, so each vector describes exactly one function, method, or class. `agent/tools/code_intel/semantic/chunker.py` builds a chunk from a header naming the symbol and its location plus the symbol body (falling back to the stored Layer 1 snippet); `agent/tools/code_intel/semantic/indexer.py` owns the `code_embeddings` table; `agent/tools/code_intel/semantic/search.py` ranks by cosine and optionally reranks.

The index is incremental by construction: it refreshes the symbol index first, drops embeddings whose symbol no longer exists, and embeds only symbols without a vector. Rows are written in batches of 16 (configurable), and a build run embeds at most 1000 chunks, at most 60 per file, each at most 2000 characters. Vectors are packed as `array('d')` blobs in the `code_embeddings` table, which also stores the `model` name and `dim`; a stored model change or a stored dimension conflict purges the index and rebuilds it in the same call, and a partially embedded index is resumable because already-embedded symbols are skipped. The embedding backend is the existing `models.build_embed_model` (selected by the `EMBEDDING_*` environment variables; local `bge-m3` by default) — no new provider is introduced.

Search self-heals the index, embeds the query, ranks every stored chunk by pure-Python cosine similarity, keeps a candidate pool of 40, and hands the top candidates to the existing `models.build_reranker_model` when one is configured. The final page is `code_intel_semantic_default_top_k` (5) by default, at most 20. Fail-open states are explicit in the payload: no indexed embeddings yields a message to build the index first; an unavailable query model yields a `degraded` message; an unconfigured reranker silently keeps cosine order with `reranked=false`; and an index or query dimension mismatch asks for a rebuild instead of scoring nonsense.

## 🧭 Role Model & Tool Surface

`CODE_INTEL_ROLES` in `agent/tools/subagent/types/functional_role.py` is the single source of truth for both the tool injection (`agent/tools/subagent/spawn/core.py`) and the prompt guidance (`agent/tools/subagent/spawn/system_prompt.py`), so the tool face and its documentation cannot drift apart:

| Functional role | Tree-sitter suite | LSP tools | ast-grep |
|-----------------|-------------------|-----------|----------|
| `researcher` | yes | yes | yes |
| `librarian` | yes | yes | yes |
| `general` | no | no | yes |
| `executor` | no | no | yes |
| `reviewer` | no | no | yes |

The main agent is outside this table entirely: the builders are never added to `_MAIN_TOOLS_BUILDERS`, and the isolation tests assert that `build_lsp_tools` is absent from that list and from the `agent.tools` namespace. The `librarian` definition at `agent/tools/subagent/roles/definitions/librarian/AGENTS.md` documents the intended external-repo workflow — clone with `terminal`, then index and search with `explore` and `semantic_code_search`, and answer with permalinks. The two-axis role model itself (depth role × functional role) is documented in the [Subagent Design page](../subagent/README.md), and each role's tool list in the [Subagent System README](../../agent/tools/subagent/README.md).

## ⚙️ Configuration

| Object | Module | Key knobs (default) |
|--------|--------|---------------------|
| `CODE_INTEL` | `config/features/agent_side/code_intel.py` | max 5000 files, 1 MB per file, 60 s build budget, batch 100, explore 10 symbols and 8000 chars, call depth 3, fuzzy score 0.3 |
| `CODE_INTEL_SEMANTIC` | `config/features/agent_side/code_intel_semantic.py` | model `bge-m3`, top-K 5 (max 20), candidate pool 40, batch 16, 1000 chunks per build, 60 per file, 2000 chars per chunk |
| `AST_GREP` | `config/features/agent_side/ast_grep.py` | pinned `0.43.0`, 50 matches, 16 KiB pattern, 30 s run timeout, 64 paths, 60 s provision timeout, 5 s version-probe timeout |
| `LSP` | `config/features/agent_side/lsp.py` | request 10 s, start 15 s, diagnostics 15 s, 2 concurrent servers, 300 s idle shutdown, 50 results, 32 opened files, 1 MB per file, auto-install off |
| `CODE_INTEL_ROLES` | `agent/tools/subagent/types/functional_role.py` | `researcher` and `librarian` |

The index database path defaults to `CODE_INTEL_DIR / "index.db"` with `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"` in `config/path.py`, and both `SHERRY_CODE_INTEL_ROOT` and `SHERRY_CODE_INTEL_DB` can override the root and the database path at call time.

## 🖥️ Environment Capability Matrix

Discovery is not capability: the matrix below is the state measured on the development host on 2026-09-23, and "discovered" means the resolver found a candidate binary — it does not prove the server starts or answers. `python` is the only language with a real end-to-end smoke; `typescript` is discovery-only; `rust` resolves to a rustup shim whose `rust-analyzer` component is not installed, so a start fails and the tool degrades to the fallback message; the remaining seven languages are `not_installed`.

| Language | Server command | Discovered on this host | Verification depth |
|----------|----------------|-------------------------|--------------------|
| `python` | `basedpyright-langserver --stdio` | yes — repository `.venv/bin` | real smoke: definition, references, diagnostics, rename preview, and status |
| `typescript` | `typescript-language-server --stdio` | yes — `PATH` | discovery only |
| `rust` | `rust-analyzer` | binary yes — toolchain component missing | start degrades to the fallback |
| `go` | `gopls` | no | install hint and fallback |
| `cpp` | `clangd` | no | install hint and fallback |
| `java` | `jdtls` | no | install hint and fallback |
| `ruby` | `ruby-lsp` | no | install hint and fallback |
| `bash` | `bash-language-server start` | no | install hint and fallback |
| `vue` | `vue-language-server --stdio` | no | install hint and fallback |
| `yaml` | `yaml-language-server --stdio` | no | install hint and fallback |

The other three engines on the same host:

| Engine | State | Evidence |
|--------|-------|----------|
| Tree-sitter index | four grammars installed (`tree-sitter 0.26.0` family) | the full code-intel suite passes (281 tests) |
| ast-grep | available from the provisioned runtime tier (`ast-grep 0.43.0`) | a basic structural search returns real matches |
| Semantic search | local `bge-m3` backend available | the real-embedding smoke returns a relevant symbol for a concept query |

Any machine that lacks these capabilities degrades instead of failing: an unresolved ast-grep triggers the verified auto-provision path, and an unavailable embedding backend makes `semantic_code_search` return a `degraded` message pointing back at `explore` and `search_files`.

## ⚠️ Limitations & Failure Modes

- **The index is caller-triggered and self-healed.** There is no background indexer: the first query builds the index, and every query refreshes it incrementally. A large repository pays the build on its first call, bounded by the file and time caps (`truncated` is reported).
- **Syntax-error, oversized, and unknown-extension files are skipped**, each with an `index_meta` row recording the reason; they are never partially indexed.
- **Embedding batches reload the model.** Each `embed_documents` call loads the backend (the local GGUF loader is invoked per batch), so batching trades load time against peak memory; the per-build and per-file caps keep this bounded.
- **An unconfigured reranker only costs ranking quality.** Without one, results stay in cosine order and the payload says `reranked=false`; with one, a reranker failure also falls back to cosine order.
- **ast-grep's first use downloads the binary.** The provision path is bounded by a 60 s timeout and refuses to install on a checksum mismatch; on an offline or asset-less platform the tool returns install hints instead.
- **LSP servers are heavy.** The manager caps them at 2 concurrent, reaps after 300 s idle, and evicts the least-recently-used server; the first request for a language pays the server start, and `lsp_diagnostics` may return `timed_out` when a server publishes nothing within its window.
- **This host's LSP coverage is partial.** Only `python` and `typescript` are discoverable; `rust` resolves but cannot start without its toolchain component; the other seven languages need an install. Auto-install is off by default, so a missing server never triggers a package-manager run.
- **`search_files` is not registered in `_MAIN_TOOLS_BUILDERS`.** The librarian definition lists it and the LSP fallback text mentions it, but the builder is absent from the candidate set, so the librarian's real retrieval path is `terminal` plus `explore` (and the other code-intel tools).
- **Writes are two-step by design.** `ast_grep_rewrite`, `lsp_rename`, and `lsp_format` only write when explicitly asked to; their previews are the safe default, and applied edits are still containment-checked.

## 🗺️ Test Map

| Area | Tests |
|------|-------|
| Symbol index, call graph, and end-to-end queries | `tests/agent/tools/code_intel/test_indexer.py`, `tests/agent/tools/code_intel/test_query.py`, `tests/agent/tools/code_intel/test_e2e.py`, `tests/agent/tools/code_intel/test_integration.py` |
| Index tools and role isolation | `tests/agent/tools/code_intel/test_tools.py`, `tests/agent/tools/code_intel/test_integration.py` |
| ast-grep discovery, provisioning, and tools | `tests/agent/tools/code_intel/ast_grep/test_resolver.py`, `tests/agent/tools/code_intel/ast_grep/test_provisioner.py`, `tests/agent/tools/code_intel/ast_grep/test_runner.py`, `tests/agent/tools/code_intel/ast_grep/test_install_hints.py`, `tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py` |
| LSP protocol, resolver, installer, fallback, client, manager, and tools | `tests/agent/tools/code_intel/lsp/test_protocol.py`, `tests/agent/tools/code_intel/lsp/test_resolver.py`, `tests/agent/tools/code_intel/lsp/test_installer.py`, `tests/agent/tools/code_intel/lsp/test_fallback.py`, `tests/agent/tools/code_intel/lsp/test_client.py`, `tests/agent/tools/code_intel/lsp/test_manager.py`, `tests/agent/tools/code_intel/lsp/test_lsp_tools.py`, `tests/agent/tools/code_intel/lsp/test_lsp_extended.py` |
| LSP role isolation and real smoke | `tests/agent/tools/code_intel/lsp/test_role_isolation.py`, `tests/agent/tools/code_intel/lsp/test_lsp_smoke.py`, `tests/agent/tools/code_intel/lsp/test_lsp_e2e.py` |
| Semantic chunking, indexing, search, and smoke | `tests/agent/tools/code_intel/semantic/test_chunker.py`, `tests/agent/tools/code_intel/semantic/test_indexer.py`, `tests/agent/tools/code_intel/semantic/test_search.py`, `tests/agent/tools/code_intel/semantic/test_semantic_e2e.py`, `tests/agent/tools/code_intel/semantic/test_semantic_smoke.py` |
| Role wiring and prompt sections | `tests/agent/tools/subagent/types/test_functional_role.py`, `tests/agent/tools/subagent/roles/test_loader.py`, `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`, `tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |

## 🔗 Related Documentation

| Page | What it covers |
|------|----------------|
| [Subagent System README](../../agent/tools/subagent/README.md) | Runtime and API reference: spawn pipeline, registry, and each role's tool list |
| [Subagent Design](../subagent/README.md) | The two-axis role model (depth role × functional role) and the spawn-privilege guards |
| [Context Engine README](../../context_engine/README.md) | The embedding storage and search pattern that the semantic layer reuses |
