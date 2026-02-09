from collections import OrderedDict
from typing import Optional, Tuple
from config import CACHE_MAX
import logging

logger = logging.getLogger(__name__)

# Cache: message_id -> (author_id, content)
_message_cache: OrderedDict[int, Tuple[int, str]] = OrderedDict()


def cache_message(message_id: int, author_id: int, content: str) -> None:
    """
    Almacena un mensaje en el cache LRU.

    Args:
        message_id: ID del mensaje
        author_id: ID del autor
        content: Contenido del mensaje
    """
    try:
        _message_cache[message_id] = (author_id, content)

        # Mantener LRU - eliminar los más antiguos
        while len(_message_cache) > CACHE_MAX:
            oldest_id, _ = _message_cache.popitem(last=False)
            logger.debug(f"Mensaje {oldest_id} eliminado del cache (LRU)")
    except Exception as e:
        logger.error(f"Error al cachear mensaje {message_id}: {e}")


def get_cached(message_id: int) -> Optional[Tuple[int, str]]:
    """
    Recupera un mensaje del cache.

    Args:
        message_id: ID del mensaje a buscar

    Returns:
        Tupla (author_id, content) o None si no está en cache
    """
    try:
        val = _message_cache.get(message_id)
        if val is None:
            return None

        # Mover al final (acceso reciente - LRU)
        _message_cache.move_to_end(message_id)
        return val
    except Exception as e:
        logger.error(f"Error al recuperar del cache mensaje {message_id}: {e}")
        return None


def remove_cached(message_id: int) -> bool:
    """
    Elimina un mensaje del cache.

    Args:
        message_id: ID del mensaje a eliminar

    Returns:
        True si se eliminó, False si no estaba en cache
    """
    try:
        if message_id in _message_cache:
            _message_cache.pop(message_id)
            return True
        return False
    except Exception as e:
        logger.error(f"Error al eliminar del cache mensaje {message_id}: {e}")
        return False


def clear_cache() -> int:
    """
    Limpia todo el cache.

    Returns:
        Número de elementos eliminados
    """
    try:
        count = len(_message_cache)
        _message_cache.clear()
        logger.info(f"Cache limpiado: {count} mensajes eliminados")
        return count
    except Exception as e:
        logger.error(f"Error al limpiar el cache: {e}")
        return 0


def get_cache_stats() -> dict:
    """
    Obtiene estadísticas del cache.

    Returns:
        Diccionario con estadísticas del cache
    """
    return {
        "size": len(_message_cache),
        "max_size": CACHE_MAX,
        "usage_percent": (len(_message_cache) / CACHE_MAX * 100) if CACHE_MAX > 0 else 0
    }