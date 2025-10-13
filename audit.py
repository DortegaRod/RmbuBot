import discord
from typing import Optional
from datetime import datetime, timezone
from config import AUDIT_LOOKBACK_SECONDS




async def find_audit_entry_for_channel(guild: discord.Guild, channel_id: int, limit: int = 20) -> Optional[discord.AuditLogEntry]:
    """Busca una entrada reciente de message_delete (o bulk) relacionada con channel_id.
    Devuelve la AuditLogEntry o None.
    """
    now = datetime.now(timezone.utc)
    try:
        async for entry in guild.audit_logs(limit=limit, action=discord.AuditLogAction.message_delete):
            # entry.created_at es timezone-aware
            delta = (now - entry.created_at).total_seconds()
            if delta < 0 or delta > AUDIT_LOOKBACK_SECONDS:
                continue
            extra = getattr(entry, "extra", None)
            if extra and hasattr(extra, "channel") and getattr(extra.channel, "id", None) == channel_id:
                return entry
            # fallback: si tiene target y es reciente, devolver
            if getattr(entry, "target", None) and delta <= AUDIT_LOOKBACK_SECONDS:
                return entry
    except discord.Forbidden:
        # permiso insuficiente
        return None
    except Exception:
        # no fallar por completo si hay otro error
        return None
    # bulk delete
    try:
        async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.message_bulk_delete):
            now = datetime.now(timezone.utc)
            delta = (now - entry.created_at).total_seconds()
            if 0 <= delta <= AUDIT_LOOKBACK_SECONDS:
               extra = getattr(entry, "extra", None)
               if extra and hasattr(extra, "channel") and getattr(extra.channel, "id", None) == channel_id:
                   return entry
    except Exception:
        return None
    return None