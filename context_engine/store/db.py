import threading
import time
import sqlite3
from collections.abc import Callable
from pathlib import Path
from loguru import logger
from config import SRC_DIR
from config.features import MES_MEMORY

_db_path: Path = SRC_DIR / "store/mes_memory/mes_memory.db"
_db: sqlite3.Connection | None = None
# Guards singleton creation (audit #15: unlocked double-check leaked connections).
_db_lock = threading.Lock()

# Busy-wait budget before "database is locked"; 1.0s starved under contention.
SQLITE_BUSY_TIMEOUT_S = MES_MEMORY["busy_timeout_s"]
# Retries for transient "database is locked" during connect/migrate.
_CONNECT_ATTEMPTS = MES_MEMORY["connect_attempts"]
_RETRY_DELAY_S = MES_MEMORY["retry_delay_s"]

type MigrationStep = Callable[[sqlite3.Connection], None]


def _migrate(db: sqlite3.Connection) -> None:
    columns = [row[1] for row in db.execute("PRAGMA table_info(_migrations)")]
    if "v" in columns:
        # Legacy index-keyed tracking table: its watermark is a position in the
        # former steps list and cannot be translated to names. Replace it
        # without pre-seeding any applied name, so the idempotent baseline
        # (every DDL statement is IF NOT EXISTS) re-runs once and records
        # itself. ``_self_heal_schema`` runs first on the connect path, so a
        # legacy ``messages`` table already carries every column the replayed
        # baseline's indexes reference.
        db.execute("DROP TABLE _migrations")
    db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, at INTEGER NOT NULL)"
    )
    applied = {row[0] for row in db.execute("SELECT name FROM _migrations").fetchall()}
    # Names, not list positions, decide what has run: every step whose name is
    # absent runs, so one inserted anywhere in _migration_steps() — even before
    # the position a database has already passed — still executes. The former
    # index-keyed resume (MAX(v) over the positional list) skipped exactly such
    # insertions permanently, losing the reasoning_tokens column in production.
    # List order is still significant: a step may depend on its predecessors.
    for name, step in _migration_steps():
        if name in applied:
            continue
        step(db)
        db.execute(
            "INSERT OR REPLACE INTO _migrations (name, at) VALUES (?, ?)",
            (name, int(time.time())),
        )
        db.commit()
    db.commit()


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _connect_with_retry() -> sqlite3.Connection:
    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(
                _db_path.resolve(),
                check_same_thread=False,
                timeout=SQLITE_BUSY_TIMEOUT_S,
                isolation_level=None,
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            # Converge the additive schema of a legacy database first: the
            # baseline replays once the legacy tracking table is replaced, and
            # its ``CREATE INDEX`` statements reference ``messages`` columns
            # that ``CREATE TABLE IF NOT EXISTS`` cannot add to an existing
            # table. The heal is a no-op on an up-to-date database.
            _self_heal_schema(db)
            _migrate(db)
            return db
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:  # noqa: S110
                    pass
            if attempt == _CONNECT_ATTEMPTS:
                raise
            time.sleep(_RETRY_DELAY_S * attempt)
    raise sqlite3.OperationalError("database is locked")


def build_schema_v1(db: sqlite3.Connection) -> None:
    """Create the complete MesMemory schema — the single migration baseline (v1).

    One call creates every table, index, FTS5 virtual table and trigger the
    current code expects, so a fresh database is fully provisioned by the
    single v1 step. The DDL below is the verbatim union of the former
    incremental chain (v1..v18): messages base columns plus every additive
    column (images/audios/videos, model tokens, origin, reasoning_tokens,
    idempotency_key, context_eligible, parent_message_id, compacted,
    compaction_checkpoint_id), all named indexes, both FTS5 tables with their
    six sync triggers, and the auxiliary tables (message_embeddings,
    compression_locks, events, context_epoch, session_leafs,
    compaction_checkpoints).
    """
    db.executescript("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_num INTEGER NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        tool_call_id TEXT,
        tool_calls TEXT,
        tool_status TEXT,
        tool_name TEXT,
        timestamp TEXT NOT NULL,
        ts_ms INTEGER NOT NULL,
        finish_reason TEXT,
        reasoning TEXT,
        reasoning_content TEXT,
        images TEXT,
        audios TEXT,
        videos TEXT,
        model_name TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        origin TEXT,
        reasoning_tokens INTEGER,
        idempotency_key TEXT,
        context_eligible INTEGER NOT NULL DEFAULT 1,
        parent_message_id INTEGER,
        compacted INTEGER NOT NULL DEFAULT 0,
        compaction_checkpoint_id INTEGER
    );
    
    CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(session_id, timestamp);
    CREATE INDEX IF NOT EXISTS idx_messages_turn_num ON messages(session_id, turn_num);
    CREATE INDEX IF NOT EXISTS idx_messages_session_role ON messages(session_id, role);
    CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(session_id, parent_message_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency
        ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL;
    -- compacted / context_eligible are deliberately NOT indexed (2026-09 SQLite
    -- index audit): both are low-cardinality booleans filtered AFTER
    -- idx_messages_turn_num has narrowed the scan to a turn range, so a
    -- dedicated index would add write cost for no measurable read gain.
    
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        content
    );
    
    CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
        );
    END;
    
    CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
        DELETE FROM messages_fts WHERE rowid = old.id;
    END;
    
    CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
        DELETE FROM messages_fts WHERE rowid = old.id;
        INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
        );
    END;
    
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
        content,
        tokenize='trigram'
    );
    
    CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts_trigram(rowid, content) VALUES (
            new.id,
            COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
        );
    END;
    
    CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
        DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    END;
    
    CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
        DELETE FROM messages_fts_trigram WHERE rowid = old.id;
        INSERT INTO messages_fts_trigram(rowid, content) VALUES (
            new.id,
            COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
        );
    END;
    
    CREATE TABLE IF NOT EXISTS message_embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER NOT NULL UNIQUE,
        embedding BLOB NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (message_id) REFERENCES messages(id)
    );
    
    CREATE INDEX IF NOT EXISTS idx_embeddings_session ON message_embeddings(session_id);
    
    CREATE TABLE IF NOT EXISTS compression_locks (
        session_id TEXT PRIMARY KEY,
        holder TEXT NOT NULL,
        acquired_at REAL NOT NULL,
        ttl INTEGER NOT NULL DEFAULT 300,
        renew_count INTEGER DEFAULT 0
    );
    
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        type TEXT NOT NULL,
        data TEXT NOT NULL,
        seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, seq)
    );
    
    CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
    
    CREATE TABLE IF NOT EXISTS context_epoch (
        session_id TEXT PRIMARY KEY,
        baseline TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        baseline_seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS session_leafs (
        session_id TEXT PRIMARY KEY,
        leaf_message_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS compaction_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        checkpoint_seq INTEGER NOT NULL,
        pre_compaction_turn INTEGER NOT NULL,
        post_compaction_turn INTEGER NOT NULL,
        summary_text TEXT,
        created_at TEXT NOT NULL
    );
    
    CREATE INDEX IF NOT EXISTS idx_compaction_session
        ON compaction_checkpoints(session_id, checkpoint_seq);
    """)
    db.commit()


# ---------------------------------------------------------------------------
# Self-healing DDL (additive, idempotent) — runs unconditionally on connect.
#
# Legacy databases carry a positional (``_migrations.v``) watermark from the
# former incremental chain; ``_migrate`` replaces that tracking table and the
# idempotent baseline re-runs. ``CREATE TABLE IF NOT EXISTS`` cannot add
# columns to an already-existing ``messages`` table, so the heal runs BEFORE
# ``_migrate`` on every connect: it adds any missing ``messages`` columns with
# guarded ALTERs and re-creates every index / auxiliary table with
# ``IF NOT EXISTS``, leaving the replayed baseline's index DDL satisfiable. A
# fresh database has no ``messages`` table yet, hence the early return before
# ``build_schema_v1`` provisions it. No statement ever drops or rewrites rows.
# ---------------------------------------------------------------------------

# (column_name, ALTER TABLE DDL) for the additive columns a legacy ``messages``
# table may lack. Duplicate-column errors on already-migrated databases are the
# expected no-op.
_HEAL_MESSAGE_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("ts_ms", "ALTER TABLE messages ADD COLUMN ts_ms INTEGER NOT NULL DEFAULT 0"),
    ("images", "ALTER TABLE messages ADD COLUMN images TEXT"),
    ("audios", "ALTER TABLE messages ADD COLUMN audios TEXT"),
    ("videos", "ALTER TABLE messages ADD COLUMN videos TEXT"),
    ("model_name", "ALTER TABLE messages ADD COLUMN model_name TEXT"),
    ("input_tokens", "ALTER TABLE messages ADD COLUMN input_tokens INTEGER"),
    ("output_tokens", "ALTER TABLE messages ADD COLUMN output_tokens INTEGER"),
    ("origin", "ALTER TABLE messages ADD COLUMN origin TEXT"),
    ("reasoning_tokens", "ALTER TABLE messages ADD COLUMN reasoning_tokens INTEGER"),
    ("idempotency_key", "ALTER TABLE messages ADD COLUMN idempotency_key TEXT"),
    (
        "context_eligible",
        "ALTER TABLE messages ADD COLUMN context_eligible INTEGER NOT NULL DEFAULT 1",
    ),
    ("parent_message_id", "ALTER TABLE messages ADD COLUMN parent_message_id INTEGER"),
    ("compacted", "ALTER TABLE messages ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0"),
    (
        "compaction_checkpoint_id",
        "ALTER TABLE messages ADD COLUMN compaction_checkpoint_id INTEGER",
    ),
)

# Named indexes from build_schema_v1, re-created with IF NOT EXISTS.
# compacted / context_eligible are deliberately absent (2026-09 index audit):
# low-cardinality booleans already filtered after idx_messages_turn_num narrows
# the scan; see the rationale next to the messages indexes in build_schema_v1.
_HEAL_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_messages_turn_num ON messages(session_id, turn_num)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_role ON messages(session_id, role)",
    "CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(session_id, parent_message_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency "
    "ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL",
)

# Auxiliary tables and their indexes from build_schema_v1 (verbatim DDL).
_HEAL_AUX_TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS message_embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER NOT NULL UNIQUE,
        embedding BLOB NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (message_id) REFERENCES messages(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_embeddings_session ON message_embeddings(session_id);",
    """
    CREATE TABLE IF NOT EXISTS compression_locks (
        session_id TEXT PRIMARY KEY,
        holder TEXT NOT NULL,
        acquired_at REAL NOT NULL,
        ttl INTEGER NOT NULL DEFAULT 300,
        renew_count INTEGER DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        type TEXT NOT NULL,
        data TEXT NOT NULL,
        seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, seq)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);",
    """
    CREATE TABLE IF NOT EXISTS context_epoch (
        session_id TEXT PRIMARY KEY,
        baseline TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        baseline_seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_leafs (
        session_id TEXT PRIMARY KEY,
        leaf_message_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS compaction_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        checkpoint_seq INTEGER NOT NULL,
        pre_compaction_turn INTEGER NOT NULL,
        post_compaction_turn INTEGER NOT NULL,
        summary_text TEXT,
        created_at TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_compaction_session "
    "ON compaction_checkpoints(session_id, checkpoint_seq);",
)


def _self_heal_schema(db: sqlite3.Connection) -> None:
    """Converge a legacy MesMemory database to the current schema, additively.

    The baseline cannot add columns to an existing ``messages`` table, so a
    former-chain database (incomplete schema) stays broken without this pass.
    The heal runs unconditionally before ``_migrate`` on every connect: missing
    ``messages`` columns are added with guarded ALTERs, and every index /
    auxiliary table from ``build_schema_v1`` is re-created with ``IF NOT
    EXISTS``. Existing rows and tables are never dropped or rewritten, so the
    pass is safe to repeat on every connect.
    """
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if exists is None:
        # Fresh database: _migrate provisions the complete schema right after.
        return

    existing_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    for column, ddl in _HEAL_MESSAGE_COLUMN_DDL:
        if column in existing_columns:
            continue
        try:
            db.execute(ddl)
            logger.info("mes_memory self-heal: added messages.{}", column)
        except sqlite3.OperationalError:
            # Duplicate column name (concurrent add) - expected no-op.
            pass
    for ddl in _HEAL_INDEX_DDL:
        db.execute(ddl)
    for ddl in _HEAL_AUX_TABLE_DDL:
        db.execute(ddl)
    db.commit()


def _migration_steps() -> list[tuple[str, MigrationStep]]:
    """Ordered ``(name, step)`` pairs; names are stable and never renamed.

    ``_migrate`` runs every step whose name is not yet recorded, so adding a
    step anywhere in this list reaches every database that has not applied
    that name — unlike the former index-keyed resume, which silently skipped a
    step inserted before a database's high-water mark. Order is still
    significant (a step may depend on its predecessors), so new steps should
    be appended; names, not positions, decide what has run.

    ``build_schema_v1`` is the complete baseline: a fresh database is fully
    provisioned by this single step, recorded as ``0001_initial_schema``.
    """
    return [("0001_initial_schema", build_schema_v1)]


def get_db_path() -> Path:
    """Public read access to the MesMemory database file path."""
    return _db_path


def get_db():
    global _db
    if _db is not None:
        return _db

    with _db_lock:
        if _db is not None:
            return _db

        _db_path.parent.mkdir(parents=True, exist_ok=True)
        _db = _connect_with_retry()

    return _db
