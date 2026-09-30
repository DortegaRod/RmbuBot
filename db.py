import sqlite3
from typing import Dict, Iterable, Optional
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

# Columnas añadidas después: se crean solas en bases de datos antiguas al arrancar
EXTRA_COLUMNS = {
    "author_name": "TEXT",     # Nombre del autor al escribir (por si luego sale del servidor)
    "edited_content": "TEXT",  # Última versión si se editó (content conserva la original)
}

MESSAGE_FIELDS = "message_id, author_id, content, channel_id, created_at, author_name, edited_content"

# Copia en memoria de guild_config: evita abrir la base de datos en cada comando
_music_channels: Dict[int, int] = {}


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
    """Inicializa la base de datos, crea las tablas necesarias y actualiza las antiguas."""
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

            existing = {row[1] for row in cursor.execute("PRAGMA table_info(mensajes)")}
            for column, sql_type in EXTRA_COLUMNS.items():
                if column not in existing:
                    cursor.execute(f"ALTER TABLE mensajes ADD COLUMN {column} {sql_type}")
                    logger.info(f"Base de datos actualizada: nueva columna '{column}'")

            _music_channels.clear()
            for guild_id, channel_id in cursor.execute("SELECT guild_id, music_channel_id FROM guild_config"):
                if channel_id:
                    _music_channels[guild_id] = channel_id
        logger.info("Base de datos inicializada correctamente")
    except Exception as e:
        logger.error(f"Error al inicializar la base de datos: {e}")
        raise


def save_message(
        message_id: int,
        author_id: int,
        content: str,
        channel_id: int,
        author_name: Optional[str] = None
) -> bool:
    """
    Guarda un mensaje en la base de datos.

    Returns:
        bool: True si se guardó correctamente, False en caso contrario.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO mensajes (message_id, author_id, content, channel_id, author_name) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, author_id, content, channel_id, author_name)
            )
        return True
    except Exception as e:
        logger.error(f"Error al guardar mensaje {message_id}: {e}")
        return False


def update_message_content(
        message_id: int,
        new_content: str,
        author_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        author_name: Optional[str] = None
) -> bool:
    """
    Guarda la versión editada de un mensaje (la original se conserva en `content`).
    Si el mensaje no estaba registrado (p. ej. es anterior al bot), se registra con su texto actual.

    Returns:
        bool: True si se guardó correctamente, False en caso contrario.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE mensajes SET edited_content = ? WHERE message_id = ?", (new_content, message_id))
            if cursor.rowcount == 0 and author_id is not None:
                cursor.execute(
                    "INSERT OR IGNORE INTO mensajes (message_id, author_id, content, channel_id, author_name) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (message_id, author_id, new_content, channel_id, author_name)
                )
        return True
    except Exception as e:
        logger.error(f"Error al actualizar mensaje editado {message_id}: {e}")
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
            cursor.execute(f"SELECT {MESSAGE_FIELDS} FROM mensajes WHERE message_id = ?", (message_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error al recuperar mensaje {message_id}: {e}")
        return None


def get_messages(message_ids: Iterable[int]) -> Dict[int, dict]:
    """
    Recupera varios mensajes de una vez (para los borrados masivos).

    Returns:
        dict: message_id -> datos del mensaje (solo los que existan).
    """
    ids = list(message_ids)
    found: Dict[int, dict] = {}
    try:
        with get_db_connection() as conn:
            for start in range(0, len(ids), 500):  # SQLite limita el nº de parámetros por consulta
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(f"SELECT {MESSAGE_FIELDS} FROM mensajes WHERE message_id IN ({placeholders})", chunk)
                for row in rows:
                    found[row["message_id"]] = dict(row)
    except Exception as e:
        logger.error(f"Error al recuperar {len(ids)} mensajes: {e}")
    return found


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
        _music_channels[guild_id] = channel_id
        return True
    except Exception as e:
        logger.error(f"Error al guardar canal del servidor {guild_id}: {e}")
        return False


def get_music_channel(guild_id: int) -> Optional[int]:
    """
    Recupera el canal de comandos configurado para un servidor (desde memoria, sin tocar el disco).

    Returns:
        int | None: ID del canal, o None si el servidor no ha hecho /setup.
    """
    return _music_channels.get(guild_id)


def clear_music_channel(guild_id: int) -> bool:
    """Elimina la restricción de canal de un servidor (vuelve a permitir todos)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        _music_channels.pop(guild_id, None)
        return True
    except Exception as e:
        logger.error(f"Error al borrar config del servidor {guild_id}: {e}")
        return False
