from collections import OrderedDict
from typing import Optional, Tuple
from config import CACHE_MAX


# message_id -> (author_id, content)
_message_cache = OrderedDict()




def cache_message(message_id: int, author_id: int, content: str):
    _message_cache[message_id] = (author_id, content)
    # mantener LRU
    while len(_message_cache) > CACHE_MAX:
        _message_cache.popitem(last=False)




def get_cached(message_id: int) -> Optional[Tuple[int, Optional[str]]]:
    """Devuelve (author_id, content) o None."""
    val = _message_cache.get(message_id)
    if val is None:
        return None
    # mover al final (acceso reciente)
    _message_cache.move_to_end(message_id)
    return val




def remove_cached(message_id: int):
    _message_cache.pop(message_id, None)