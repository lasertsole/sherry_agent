import time
import sqlite3
from pathlib import Path
from config import SRC_DIR

_db_path: Path = SRC_DIR / "store/rag/rag.db"
_db: sqlite3.Connection | None = None


def _migrate(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)")
    cur = db.execute("SELECT MAX(v) as v FROM _migrations").fetchone()[0]
    if cur is None:
        cur = 0
    steps = [build_nodes_tb, build_edges_tb, build_nodes_fts_tb, build_nodes_fts_trigram_tb]
    for i in range(cur, len(steps)):
        steps[i](db)
        db.execute("INSERT INTO _migrations (v,at) VALUES (?,?)", (i + 1, int(time.time())))
    db.commit()


def get_db():
    global _db
    if _db:
        return _db

    _db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = sqlite3.connect(
        _db_path.resolve(),
        check_same_thread=False,
        timeout=1.0,
        isolation_level=None,
    )

    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA foreign_keys=ON")

    _migrate(_db)

    return _db


def build_nodes_tb(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id INTEGER PRIMARY KEY,
            text TEXT NOT NULL UNIQUE,
            embedding_json TEXT NOT NULL,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)


def build_edges_tb(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            bridge_relation TEXT NOT NULL,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES nodes(node_id),
            FOREIGN KEY (target_id) REFERENCES nodes(node_id)
        );
        
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
    """)


def build_nodes_fts_tb(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
            text,
            content='nodes',
            content_rowid='node_id'
        );
        
        CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, text) VALUES (new.node_id, new.text);
        END;
        
        CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, text) VALUES('delete', old.node_id, old.text);
        END;
        
        CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, text) VALUES('delete', old.node_id, old.text);
            INSERT INTO nodes_fts(rowid, text) VALUES (new.node_id, new.text);
        END;
    """)


def build_nodes_fts_trigram_tb(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts_trigram USING fts5(
            text,
            content='nodes',
            content_rowid='node_id',
            tokenize='trigram'
        );
        
        CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts_trigram(rowid, text) VALUES (new.node_id, new.text);
        END;
        
        CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts_trigram(nodes_fts_trigram, rowid, text) VALUES('delete', old.node_id, old.text);
        END;
        
        CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
            INSERT INTO nodes_fts_trigram(nodes_fts_trigram, rowid, text) VALUES('delete', old.node_id, old.text);
            INSERT INTO nodes_fts_trigram(rowid, text) VALUES (new.node_id, new.text);
        END;
    """)