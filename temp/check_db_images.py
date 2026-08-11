"""Verify DB persistence of media: check for new messages rows with non-NULL images.

Reads the last 10 rows (ordered by id desc) and reports id, session_id, role,
a content preview, and whether images is NULL or a non-empty JSON array.
"""
import sqlite3
import json

DB = r"C:\app\code\project\EMA_AI_agent\src\store\mes_memory\mes_memory.db"

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT COUNT(*), MAX(id) FROM messages")
print("rows,max_id =", cur.fetchone())

cur.execute(
    "SELECT id, session_id, role, substr(content, 1, 50), images, timestamp "
    "FROM messages ORDER BY id DESC LIMIT 10"
)
for row in cur.fetchall():
    rid, session_id, role, content, images, ts = row
    img_preview = "NULL" if images is None else images
    if isinstance(images, str) and images:
        try:
            parsed = json.loads(images)
            img_preview = f"non-NULL len={len(parsed)} {parsed[:1]}"
        except Exception:
            img_preview = f"non-NULL raw={images[:80]!r}"
    print(f"id={rid} session={session_id} role={role} ts={ts} | {content!r} | images={img_preview}")

conn.close()
