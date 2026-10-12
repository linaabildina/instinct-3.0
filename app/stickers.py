"""Хранилище стикеров Алины и управление набором через Telegram-команды."""
import sqlite3
from pathlib import Path


def _connect(db_path: str):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alina_stickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL UNIQUE,
            tags TEXT NOT NULL,
            emoji TEXT,
            added_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def add_sticker(db_path: str, file_id: str, tags: str, emoji: str | None, added_by: int | None):
    clean_tags = ", ".join(dict.fromkeys(
        part.strip().lower() for part in tags.replace(";", ",").split(",") if part.strip()
    ))
    if not clean_tags:
        clean_tags = "универсальная реакция"
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO alina_stickers(file_id, tags, emoji, added_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(file_id) DO UPDATE SET tags=excluded.tags,
                   emoji=excluded.emoji, added_by=excluded.added_by""",
            (file_id, clean_tags[:300], emoji, added_by),
        )
        row = conn.execute("SELECT id FROM alina_stickers WHERE file_id=?", (file_id,)).fetchone()
        return row[0]


def list_stickers(db_path: str):
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT id, tags, emoji FROM alina_stickers ORDER BY id"
        ).fetchall()


def get_stickers(db_path: str):
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT id, file_id, tags, emoji FROM alina_stickers ORDER BY id"
        ).fetchall()


def remove_sticker(db_path: str, sticker_id: int) -> bool:
    with _connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM alina_stickers WHERE id=?", (sticker_id,))
        return cursor.rowcount > 0
