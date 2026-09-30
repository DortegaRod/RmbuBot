"""
Atribución de borrados mediante el Registro de Auditoría de Discord.

Cómo registra Discord los borrados (y por qué el código es así):
- Si alguien borra SU PROPIO mensaje, Discord NO crea ninguna entrada.
- Si un moderador borra varios mensajes de la misma persona en el mismo canal en poco
  tiempo, Discord no crea entradas nuevas: incrementa el contador (`extra.count`) de la
  existente, que conserva su fecha original.
Por eso se recuerda cuántos borrados de cada entrada se han atribuido ya: si el contador
sube, hay un borrado nuevo de ese moderador.
"""

import logging
from collections import OrderedDict
from typing import Optional, Tuple

import discord

from config import AUDIT_LOOKBACK_SECONDS

logger = logging.getLogger(__name__)

_MAX_TRACKED = 500
_consumed: "OrderedDict[int, int]" = OrderedDict()        # entry.id -> borrados ya atribuidos
_consumed_bulk: "OrderedDict[int, bool]" = OrderedDict()  # Entradas de borrado masivo ya usadas


def _remember(store: OrderedDict, key: int, value) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > _MAX_TRACKED:
        store.popitem(last=False)


def _entry_count(entry: discord.AuditLogEntry) -> int:
    return getattr(entry.extra, "count", None) or 1


def _age_seconds(entry: discord.AuditLogEntry) -> float:
    return (discord.utils.utcnow() - entry.created_at).total_seconds()


async def prime(guild: discord.Guild) -> None:
    """
    Memoriza los contadores actuales al arrancar, para que tras un reinicio no se
    atribuyan borrados antiguos a borrados nuevos.
    """
    try:
        async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.message_delete):
            _remember(_consumed, entry.id, _entry_count(entry))
        async for entry in guild.audit_logs(limit=25, action=discord.AuditLogAction.message_bulk_delete):
            _remember(_consumed_bulk, entry.id, True)
    except discord.Forbidden:
        logger.warning("⚠️ Sin permiso 'Ver el registro de auditoría': no se sabrá quién borra los mensajes")
    except discord.HTTPException as e:
        logger.warning(f"No pude leer el registro de auditoría: {e}")


async def find_deleter(
        guild: discord.Guild,
        channel_id: int,
        author_id: Optional[int]
) -> Tuple[Optional[discord.abc.User], bool]:
    """
    Busca qué moderador borró un mensaje de `author_id` en `channel_id`.

    Returns:
        (moderador, auditoría_disponible). Si no hay moderador pero la auditoría estaba
        disponible, lo borró el propio autor (Discord no registra los autoborrados).
    """
    try:
        async for entry in guild.audit_logs(limit=25, action=discord.AuditLogAction.message_delete):
            channel = getattr(entry.extra, "channel", None)
            if channel is None or channel.id != channel_id or getattr(entry.target, "id", None) != author_id:
                continue

            count = _entry_count(entry)
            seen = _consumed.get(entry.id)
            if seen is None:
                if _age_seconds(entry) <= AUDIT_LOOKBACK_SECONDS:
                    _remember(_consumed, entry.id, 1)  # Entrada nueva: este es su primer borrado
                    return entry.user, True
                _remember(_consumed, entry.id, count)  # Entrada antigua: no corresponde a este borrado
            elif count > seen:
                _remember(_consumed, entry.id, seen + 1)  # El contador subió: borrado nuevo
                return entry.user, True
        return None, True
    except discord.Forbidden:
        return None, False
    except discord.HTTPException as e:
        logger.warning(f"No pude consultar el registro de auditoría: {e}")
        return None, False


async def find_bulk_deleter(guild: discord.Guild, channel_id: int) -> Tuple[Optional[discord.abc.User], bool]:
    """
    Busca quién hizo un borrado masivo (purga) en `channel_id`.

    Returns:
        (responsable, auditoría_disponible)
    """
    try:
        async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.message_bulk_delete):
            if getattr(entry.target, "id", None) != channel_id or entry.id in _consumed_bulk:
                continue
            if _age_seconds(entry) <= AUDIT_LOOKBACK_SECONDS * 3:
                _remember(_consumed_bulk, entry.id, True)
                return entry.user, True
        return None, True
    except discord.Forbidden:
        return None, False
    except discord.HTTPException as e:
        logger.warning(f"No pude consultar el registro de auditoría (borrado masivo): {e}")
        return None, False
