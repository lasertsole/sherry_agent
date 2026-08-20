import time
import sqlite3
from pathlib import Path
from config import SRC_DIR

_db_path: Path = SRC_DIR / "store/mes_memory/mes_memory.db"
_db: sqlite3.Connection | None = None

def _migrate(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS _migrations (v INTEGER PRIMARY KEY, at INTEGER NOT NULL)")
    cur = db.execute("SELECT MAX(v) as v FROM _migrations").fetchone()[0]
    if cur is None:
        cur = 0
    steps = [build_messages_tb, build_messages_fts_tb, build_messages_fts_trigram_tb, add_images_column, add_audio_video_columns]
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

def build_messages_tb(db: sqlite3.Connection) -> None:
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
        finish_reason TEXT,
        reasoning TEXT,
        reasoning_content TEXT,
        images TEXT,
        audios TEXT,
        videos TEXT
    );
    
    CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(session_id, timestamp);
    CREATE INDEX IF NOT EXISTS idx_messages_turn_num ON messages(session_id, turn_num);""")

def add_images_column(db: sqlite3.Connection) -> None:
    """Add an `images` column to the messages table (JSON-encoded list of paths).

    This is a one-time additive migration for databases created before the
    column existed. It uses try/except to ignore the error raised when the
    column is already present.
    """
    try:
        db.execute("ALTER TABLE messages ADD COLUMN images TEXT")
    except sqlite3.OperationalError:
        # Column already exists — nothing to do.
        pass

def add_audio_video_columns(db: sqlite3.Connection) -> None:
    """Add `audios` and `videos` columns to the messages table.

    Both are JSON-encoded lists (base64 for user messages, persistent file
    paths for AI messages), mirroring the existing `images` column. This is a
    one-time additive migration for databases created before these columns
    existed; try/except ignores the error raised when they already present.
    """
    try:
        db.execute("ALTER TABLE messages ADD COLUMN audios TEXT")
    except sqlite3.OperationalError:
        # Column already exists — nothing to do.
        pass
    try:
        db.execute("ALTER TABLE messages ADD COLUMN videos TEXT")
    except sqlite3.OperationalError:
        # Column already exists — nothing to do.
        pass

def build_messages_fts_tb(db: sqlite3.Connection) -> None:
    db.executescript("""
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
    """)

def build_messages_fts_trigram_tb(db: sqlite3.Connection) -> None:
    db.executescript("""
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
    """)