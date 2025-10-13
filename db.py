import sqlite3
from typing import Optional
from config import DB_PATH

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mensajes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER UNIQUE,
    author_id INTEGER,
    content TEXT,
    channel_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(CREATE_TABLE_SQL)
    conn.commit()
    conn.close()

def save_message(message_id: int, author_id: int, content: str, channel_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO mensajes (message_id, author_id, content, channel_id) VALUES (?, ?, ?, ?)",
        (message_id, author_id, content, channel_id))
    conn.commit()
    conn.close()

def get_message(message_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT message_id, author_id, content, channel_id, created_at FROM mensajes WHERE message_id = ?",
        (message_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "message_id": row[0],
        "author_id": row[1],
        "content": row[2],
        "channel_id": row[3],
        "created_at": row[4]
    }
