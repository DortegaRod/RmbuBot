import discord
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    """Retorna la fecha y hora actual en UTC."""
    return datetime.now(timezone.utc)


async def send_admin_embed(
        admin_channel: discord.TextChannel,
        *,
        author_display: str,
        executor_display: str,
        channel_display: str,
        content: str,
        message_id: int
) -> bool:
    """
    Envía un embed al canal de administración sobre un mensaje eliminado.

    Args:
        admin_channel: Canal donde enviar la notificación
        author_display: Mención o nombre del autor del mensaje
        executor_display: Mención o nombre de quien eliminó el mensaje
        channel_display: Mención o nombre del canal
        content: Contenido del mensaje eliminado
        message_id: ID del mensaje eliminado

    Returns:
        True si se envió correctamente, False en caso contrario
    """
    try:
        # Truncar contenido si es muy largo
        max_content_length = 1024
        if len(content) > max_content_length:
            content = content[:max_content_length - 3] + "..."

        embed = discord.Embed(
            title="🗑️ Mensaje eliminado",
            description=(
                f"**Autor:** {author_display}\n"
                f"**Eliminado por:** {executor_display}\n"
                f"**Canal:** {channel_display}"
            ),
            color=discord.Color.red(),
            timestamp=now_utc()
        )

        embed.add_field(
            name="Contenido",
            value=content if content else "*(sin contenido de texto)*",
            inline=False
        )

        embed.set_footer(text=f"ID del mensaje: {message_id}")

        await admin_channel.send(embed=embed)
        logger.info(f"Notificación de eliminación enviada para mensaje {message_id}")
        return True

    except discord.Forbidden:
        logger.error(f"Sin permisos para enviar mensajes en {admin_channel.name}")
        return False
    except Exception as e:
        logger.error(f"Error al enviar embed de notificación: {e}")
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