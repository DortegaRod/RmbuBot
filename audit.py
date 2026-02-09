import discord
from typing import Optional
from datetime import datetime, timezone
from config import AUDIT_LOOKBACK_SECONDS
import logging

logger = logging.getLogger(__name__)


async def find_audit_entry_for_channel(
        guild: discord.Guild,
        channel_id: int,
        limit: int = 20
) -> Optional[discord.AuditLogEntry]:
    """
    Busca una entrada reciente de message_delete o message_bulk_delete relacionada con channel_id.

    Args:
        guild: El servidor de Discord
        channel_id: ID del canal donde se eliminó el mensaje
        limit: Número máximo de entradas a revisar

    Returns:
        AuditLogEntry si se encuentra, None en caso contrario
    """
    now = datetime.now(timezone.utc)

    # Buscar en message_delete
    try:
        async for entry in guild.audit_logs(
                limit=limit,
                action=discord.AuditLogAction.message_delete
        ):
            # Verificar que la entrada sea reciente
            delta = (now - entry.created_at).total_seconds()
            if delta < 0 or delta > AUDIT_LOOKBACK_SECONDS:
                continue

            # Verificar si la entrada está relacionada con el canal
            extra = getattr(entry, "extra", None)
            if extra and hasattr(extra, "channel"):
                channel = getattr(extra, "channel", None)
                if channel and getattr(channel, "id", None) == channel_id:
                    logger.debug(f"Entrada de auditoría encontrada: {entry.user} eliminó mensaje en canal {channel_id}")
                    return entry

            # Fallback: si tiene target y es reciente
            if getattr(entry, "target", None) and delta <= AUDIT_LOOKBACK_SECONDS:
                logger.debug(f"Entrada de auditoría (fallback) encontrada: {entry.user}")
                return entry

    except discord.Forbidden:
        logger.warning("Permisos insuficientes para acceder al registro de auditoría")
        return None
    except Exception as e:
        logger.error(f"Error al buscar en audit log (message_delete): {e}")
        # Continuar con bulk_delete

    # Buscar en message_bulk_delete
    try:
        async for entry in guild.audit_logs(
                limit=10,
                action=discord.AuditLogAction.message_bulk_delete
        ):
            delta = (now - entry.created_at).total_seconds()
            if delta < 0 or delta > AUDIT_LOOKBACK_SECONDS:
                continue

            extra = getattr(entry, "extra", None)
            if extra and hasattr(extra, "channel"):
                channel = getattr(extra, "channel", None)
                if channel and getattr(channel, "id", None) == channel_id:
                    logger.debug(
                        f"Entrada de bulk delete encontrada: {entry.user} eliminó mensajes en canal {channel_id}")
                    return entry

    except discord.Forbidden:
        logger.warning("Permisos insuficientes para acceder al registro de auditoría (bulk)")
        return None
    except Exception as e:
        logger.error(f"Error al buscar en audit log (bulk_delete): {e}")

    logger.debug(f"No se encontró entrada de auditoría para el canal {channel_id}")
    return None


async def get_recent_audit_entries(
        guild: discord.Guild,
        action: discord.AuditLogAction,
        limit: int = 10
) -> list[discord.AuditLogEntry]:
    """
    Obtiene entradas recientes del registro de auditoría.

    Args:
        guild: El servidor de Discord
        action: Acción a buscar
        limit: Número máximo de entradas

    Returns:
        Lista de entradas de auditoría
    """
    entries = []
    try:
        async for entry in guild.audit_logs(limit=limit, action=action):
            entries.append(entry)
    except discord.Forbidden:
        logger.warning(f"Permisos insuficientes para acceder al registro de auditoría ({action})")
    except Exception as e:
        logger.error(f"Error al obtener entradas de auditoría: {e}")

    return entries