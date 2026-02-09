import sqlite3
from typing import Optional
from contextlib import contextmanager
from config import DB_PATH
import logging

logger = logging.getLogger(__name__)

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

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_message_id ON mensajes(message_id);
"""


@contextmanager
def get_db_connection():
    """Context manager para conexiones a la base de datos."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error en base de datos: {e}")
        raise
    finally:
        conn.close()


def init_db():
    """Inicializa la base de datos y crea las tablas necesarias."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(CREATE_TABLE_SQL)
            cursor.execute(CREATE_INDEX_SQL)
        logger.info("Base de datos inicializada correctamente")
    except Exception as e:
        logger.error(f"Error al inicializar la base de datos: {e}")
        raise


def save_message(message_id: int, author_id: int, content: str, channel_id: int) -> bool:
    """
    Guarda un mensaje en la base de datos.

    Returns:
        bool: True si se guardó correctamente, False en caso contrario.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO mensajes (message_id, author_id, content, channel_id) VALUES (?, ?, ?, ?)",
                (message_id, author_id, content, channel_id)
            )
        return True
    except Exception as e:
        logger.error(f"Error al guardar mensaje {message_id}: {e}")
        return False


def get_message(message_id: int) -> Optional[dict]:
    """
    Recupera un mensaje de la base de datos.

    Returns:
        dict | None: Diccionario con los datos del mensaje o None si no existe.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT message_id, author_id, content, channel_id, created_at FROM mensajes WHERE message_id = ?",
                (message_id,)
            )
            row = cursor.fetchone()

            if not row:
                return None

            return {
                "message_id": row[0],
                "author_id": row[1],
                "content": row[2],
                "channel_id": row[3],
                "created_at": row[4]
            }
    except Exception as e:
        logger.error(f"Error al recuperar mensaje {message_id}: {e}")
        return None


def delete_old_messages(days: int = 30) -> int:
    """
    Elimina mensajes antiguos de la base de datos.

    Args:
        days: Número de días a mantener.

    Returns:
        int: Número de mensajes eliminados.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM mensajes WHERE created_at < datetime('now', ? || ' days')",
                (f'-{days}',)
            )
            deleted_count = cursor.rowcount
        logger.info(f"Eliminados {deleted_count} mensajes antiguos")
        return deleted_count
    except Exception as e:
        logger.error(f"Error al eliminar mensajes antiguos: {e}")
        return 0