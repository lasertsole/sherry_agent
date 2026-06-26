import time
import sqlite3
from pathlib import Path
from config import SRC_DIR, CONTEXT_ENGINE_PATH

_db: sqlite3.Connection | None = None
_db_path = Path(SRC_DIR) / "store/skill_memory/skill_memory.db"

def get_db():
    global _db
    if _db:
        return _db

    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(_db_path, check_same_thread=False)

    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode = WAL")
    _db.execute("PRAGMA foreign_keys = ON")
    _migrate(_db)

    return _db

# For testing only: close and reset the singleton
def close_db()->None:
    global _db
    if _db:
      _db.close()
      _db = None

def _migrate(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)")
    cur = db.execute("SELECT MAX(v) as v FROM _migrations").fetchone()[0]
    if cur is None:
        cur = 0
    steps = [m1_core, m2_messages, m3_signals, m4_fts5, m5_vectors, m6_communities]
    for i in range(cur, len(steps)):
        steps[i](db)
        db.execute("INSERT INTO _migrations (v,at) VALUES (?,?)", (i + 1, int(time.time())))
    db.commit()

# ─── Core tables: nodes + edges ──────────────────────────────────────
def m1_core(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gm_nodes (
            id              TEXT PRIMARY KEY,
            type            TEXT NOT NULL CHECK(type IN ('TASK','SKILL','EVENT')),
            name            TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL,
            validated_count INTEGER NOT NULL DEFAULT 1,
            source_sessions TEXT NOT NULL DEFAULT '[]',
            community_id    TEXT,
            pagerank        REAL NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_gm_nodes_name ON gm_nodes(name);
        CREATE INDEX IF NOT EXISTS ix_gm_nodes_community ON gm_nodes(community_id);

        CREATE TABLE IF NOT EXISTS gm_edges (
            id          TEXT PRIMARY KEY,
            from_id     TEXT NOT NULL REFERENCES gm_nodes(id),
            to_id       TEXT NOT NULL REFERENCES gm_nodes(id),
            type        TEXT NOT NULL CHECK(type IN ('USED_SKILL','SOLVED_BY','REQUIRES','PATCHES','CONFLICTS_WITH')),
            instruction TEXT NOT NULL,
            condition   TEXT,
            session_id  TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_gm_edges_from ON gm_edges(from_id);
        CREATE INDEX IF NOT EXISTS ix_gm_edges_to   ON gm_edges(to_id);
    """)
# ─── Message storage ──────────────────────────────────────────

def m2_messages(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gm_messages (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            turn_index  INTEGER NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_gm_msg_session ON gm_messages(session_id, turn_index);
    """)

# ─── Signal storage ───────────────────────────────────────────
def m3_signals(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gm_signals (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            turn_index  INTEGER NOT NULL,
            type        TEXT NOT NULL,
            data        TEXT NOT NULL DEFAULT '{}',
            processed   INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_gm_sig_session ON gm_signals(session_id, processed);
    """)

# ─── FTS5 full-text index ──────────────────────────────────────
def m4_fts5(db: sqlite3.Connection) -> None:
    try:
        db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS gm_nodes_fts USING fts5(
                name,
                description,
                content,
                content=gm_nodes,
                content_rowid=rowid
            );

            CREATE TRIGGER IF NOT EXISTS gm_nodes_ai AFTER INSERT ON gm_nodes BEGIN
                INSERT INTO gm_nodes_fts(rowid, name, description, content)
                VALUES (NEW.rowid, NEW.name, NEW.description, NEW.content);
            END;

            CREATE TRIGGER IF NOT EXISTS gm_nodes_ad AFTER DELETE ON gm_nodes BEGIN
                INSERT INTO gm_nodes_fts(gm_nodes_fts, rowid, name, description, content)
                VALUES ('delete', OLD.rowid, OLD.name, OLD.description, OLD.content);
            END;

            CREATE TRIGGER IF NOT EXISTS gm_nodes_au AFTER UPDATE ON gm_nodes BEGIN
                INSERT INTO gm_nodes_fts(gm_nodes_fts, rowid, name, description, content)
                VALUES ('delete', OLD.rowid, OLD.name, OLD.description, OLD.content);
                INSERT INTO gm_nodes_fts(rowid, name, description, content)
                VALUES (NEW.rowid, NEW.name, NEW.description, NEW.content);
            END;
            
            CREATE VIRTUAL TABLE IF NOT EXISTS gm_nodes_fts_trigram USING fts5(
                name,
                description,
                content,
                content=gm_nodes,
                content_rowid=rowid,
                tokenize='trigram'
            );

            CREATE TRIGGER IF NOT EXISTS gm_nodes_ai AFTER INSERT ON gm_nodes BEGIN
                INSERT INTO gm_nodes_fts_trigram(rowid, name, description, content)
                VALUES (NEW.rowid, NEW.name, NEW.description, NEW.content);
            END;

            CREATE TRIGGER IF NOT EXISTS gm_nodes_ad AFTER DELETE ON gm_nodes BEGIN
                INSERT INTO gm_nodes_fts_trigram(gm_nodes_fts_trigram, rowid, name, description, content)
                VALUES ('delete', OLD.rowid, OLD.name, OLD.description, OLD.content);
            END;

            CREATE TRIGGER IF NOT EXISTS gm_nodes_au AFTER UPDATE ON gm_nodes BEGIN
                INSERT INTO gm_nodes_fts_trigram(gm_nodes_fts_trigram, rowid, name, description, content)
                VALUES ('delete', OLD.rowid, OLD.name, OLD.description, OLD.content);
                INSERT INTO gm_nodes_fts_trigram(rowid, name, description, content)
                VALUES (NEW.rowid, NEW.name, NEW.description, NEW.content);
            END;
        """)
    except Exception as e:
        print(f"[WARN] FTS5 not available, falling back to LIKE search: {e}")

# ─── Vector storage ───────────────────────────────────────────
def m5_vectors(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gm_vectors (
            node_id      TEXT PRIMARY KEY REFERENCES gm_nodes(id),
            content_hash TEXT NOT NULL,
            embedding    BLOB NOT NULL
        );
    """)

# ─── Community summary storage ────────────────────────────────
def m6_communities(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS gm_communities (
            id          TEXT PRIMARY KEY,
            summary     TEXT NOT NULL,
            node_count  INTEGER NOT NULL DEFAULT 0,
            node_ids    TEXT NOT NULL DEFAULT '[]',
            embedding   BLOB,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        );
    """)