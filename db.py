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

# Configuración persistente por servidor (canal de comandos de /setup)
CREATE_GUILD_CONFIG_SQL = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    music_channel_id INTEGER
);
"""


@contextmanager
def get_db_connection():
    """Context manager para conexiones a la base de datos."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # Espera hasta 5s si la BD está bloqueada por otra escritura (evita 'database is locked')
    conn.execute("PRAGMA busy_timeout=5000")
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
            # WAL: permite lecturas concurrentes con la escritura y mejora el
            # rendimiento al guardar muchos mensajes. Es una propiedad del fichero,
            # basta con activarla una vez.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(CREATE_TABLE_SQL)
            cursor.execute(CREATE_INDEX_SQL)
            cursor.execute(CREATE_GUILD_CONFIG_SQL)
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


# ==========================================
# CONFIGURACIÓN POR SERVIDOR (comando /setup)
# ==========================================

def set_music_channel(guild_id: int, channel_id: int) -> bool:
    """
    Fija (o actualiza) el canal de comandos de un servidor.

    Returns:
        bool: True si se guardó correctamente.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO guild_config (guild_id, music_channel_id) VALUES (?, ?)",
                (guild_id, channel_id)
            )
        return True
    except Exception as e:
        logger.error(f"Error al guardar canal del servidor {guild_id}: {e}")
        return False


def get_music_channel(guild_id: int) -> Optional[int]:
    """
    Recupera el canal de comandos configurado para un servidor.

    Returns:
        int | None: ID del canal, o None si el servidor no ha hecho /setup.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT music_channel_id FROM guild_config WHERE guild_id = ?",
                (guild_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Error al recuperar canal del servidor {guild_id}: {e}")
        return None


def clear_music_channel(guild_id: int) -> bool:
    """Elimina la restricción de canal de un servidor (vuelve a permitir todos)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        return True
    except Exception as e:
        logger.error(f"Error al borrar config del servidor {guild_id}: {e}")
        return False