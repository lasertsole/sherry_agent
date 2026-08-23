"""Verify seeded rows landed in SQLite with correct session/depth structure."""
import json
import sqlite3

DB = r"agent/tools/subagent/data/subagent_registry.db"

conn = sqlite3.connect(DB)
rows = conn.execute(
    "select run_id, data from subagent_runs where run_id like 'seed-%'"
).fetchall()
print(f"seed rows in SQLite: {len(rows)}\n")
for run_id, data in rows:
    rec = json.loads(data)
    print(
        f"  {run_id:16s} requester={rec.get('requester_session_key'):20s} "
        f"depth={rec.get('depth')} status={rec.get('execution', {}).get('status')} "
        f"outcome={rec.get('execution', {}).get('outcome', {}).get('status')} "
        f"delivery={rec.get('delivery', {}).get('status')}"
    )
conn.close()
