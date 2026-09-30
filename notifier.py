import io
import discord
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Madrid")  # Hora de las transcripciones de borrados masivos
except Exception:  # Windows sin el paquete tzdata
    LOCAL_TZ = timezone.utc

NO_MENTIONS = discord.AllowedMentions.none()


def now_utc() -> datetime:
    """Retorna la fecha y hora actual en UTC."""
    return datetime.now(timezone.utc)


def truncate(text: str, limit: int) -> str:
    """Recorta un texto a `limit` caracteres (los campos de un embed admiten 1024)."""
    return text if len(text) <= limit else text[:limit - 3] + "..."


async def send_admin_embed(
        admin_channel: discord.TextChannel,
        *,
        author_display: str,
        executor_display: str,
        channel_display: str,
        content: str,
        message_id: int,
        original_content: str = None
) -> bool:
    """
    Envía un embed al canal de administración sobre un mensaje eliminado.

    Args:
        admin_channel: Canal donde enviar la notificación
        author_display: Mención o nombre del autor del mensaje
        executor_display: Mención o nombre de quien eliminó el mensaje
        channel_display: Mención o nombre del canal
        content: Contenido del mensaje al borrarlo
        message_id: ID del mensaje eliminado
        original_content: Texto original, si el mensaje se editó antes de borrarlo

    Returns:
        True si se envió correctamente, False en caso contrario
    """
    try:
        sent_at = int(discord.utils.snowflake_time(message_id).timestamp())
        embed = discord.Embed(
            title="🗑️ Mensaje eliminado",
            description=(
                f"**Autor:** {author_display}\n"
                f"**Eliminado por:** {executor_display}\n"
                f"**Canal:** {channel_display}\n"
                f"**Enviado:** <t:{sent_at}:f> (<t:{sent_at}:R>)"
            ),
            color=discord.Color.red(),
            timestamp=now_utc()
        )

        embed.add_field(
            name="Contenido",
            value=truncate(content, 1024) if content else "*(sin contenido de texto)*",
            inline=False
        )
        if original_content:
            embed.add_field(name="✏️ Antes de editarlo", value=truncate(original_content, 1024), inline=False)

        embed.set_footer(text=f"ID del mensaje: {message_id}")

        await admin_channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        logger.info(f"Notificación de eliminación enviada para mensaje {message_id}")
        return True

    except discord.Forbidden:
        logger.error(f"Sin permisos para enviar mensajes en {admin_channel.name}")
        return False
    except Exception as e:
        logger.error(f"Error al enviar embed de notificación: {e}")
        return False


def _transcript_line(record: dict) -> str:
    when = discord.utils.snowflake_time(record["message_id"]).astimezone(LOCAL_TZ)
    author = record.get("author_name") or record.get("author_id") or "?"
    content = record.get("edited_content") or record.get("content") or ""
    return f"[{when:%d/%m/%Y %H:%M:%S}] {author}: {content}"


async def send_bulk_delete_log(
        admin_channel: discord.TextChannel,
        *,
        channel_display: str,
        executor_display: str,
        total: int,
        records: list
) -> bool:
    """
    Registra un borrado masivo (purga): resumen + transcripción de los mensajes recuperados.
    La transcripción va como .txt si el bot puede adjuntar archivos; si no, un extracto.

    Args:
        records: Mensajes recuperados de la caché/BD, en orden cronológico (con 'message_id')
    """
    try:
        embed = discord.Embed(
            title="🧹 Borrado masivo",
            description=(
                f"**Mensajes borrados:** {total}\n"
                f"**Canal:** {channel_display}\n"
                f"**Borrado por:** {executor_display}\n"
                f"**Recuperados:** {len(records)} de {total}"
            ),
            color=discord.Color.dark_red(),
            timestamp=now_utc()
        )

        transcript = "\n".join(_transcript_line(r) for r in records)
        file = None
        if transcript:
            if admin_channel.permissions_for(admin_channel.guild.me).attach_files:
                file = discord.File(io.BytesIO(transcript.encode("utf-8")), filename="borrado_masivo.txt")
            else:
                embed.add_field(name="Contenido", value=truncate(transcript, 1024), inline=False)
                embed.add_field(
                    name="ℹ️ Transcripción completa",
                    value="Dale al bot el permiso **Adjuntar archivos** en este canal para recibirla como archivo.",
                    inline=False
                )

        if file:
            await admin_channel.send(embed=embed, file=file, allowed_mentions=NO_MENTIONS)
        else:
            await admin_channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
        logger.info(f"Borrado masivo registrado: {total} mensajes")
        return True

    except discord.Forbidden:
        logger.error(f"Sin permisos para enviar mensajes en {admin_channel.name}")
        return False
    except Exception as e:
        logger.error(f"Error al registrar el borrado masivo: {e}")
        return False


async def send_info_embed(
        channel: discord.TextChannel,
        title: str,
        description: str,
        color: discord.Color = discord.Color.blue()
) -> bool:
    """
    Envía un embed informativo a un canal.

    Args:
        channel: Canal donde enviar el mensaje
        title: Título del embed
        description: Descripción del embed
        color: Color del embed

    Returns:
        True si se envió correctamente, False en caso contrario
    """
    try:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=now_utc()
        )

        await channel.send(embed=embed)
        return True

    except Exception as e:
        logger.error(f"Error al enviar embed informativo: {e}")
        return False


async def send_error_embed(
        channel: discord.TextChannel,
        error_message: str
) -> bool:
    """
    Envía un embed de error a un canal.

    Args:
        channel: Canal donde enviar el mensaje
        error_message: Mensaje de error

    Returns:
        True si se envió correctamente, False en caso contrario
    """
    return await send_info_embed(
        channel,
        "❌ Error",
        error_message,
        discord.Color.red()
    )
