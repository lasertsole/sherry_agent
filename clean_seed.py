"""Remove synthetic seed-* run records from SQLite (keeps script files).

Run after the 后台任务 tab cluster demo is confirmed. The in-memory registry in
a running server is unaffected; deleting from SQLite means the next backend
restart will not restore these synthetic rows.
"""

import sqlite3

DB = r"agent/tools/subagent/data/subagent_registry.db"

conn = sqlite3.connect(DB)
cur = conn.execute("delete from subagent_runs where run_id like 'seed-%'")
conn.commit()
print(f"Deleted {cur.rowcount} seed-* rows from {DB}")
conn.close()
