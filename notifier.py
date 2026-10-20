"""Registros del canal de administración: mensajes borrados y borrados masivos, en tarjetas."""

import io
import logging
import time
from datetime import timezone

import discord

import tarjetas

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Madrid")  # Hora de las transcripciones de borrados masivos
except Exception:  # Windows sin el paquete tzdata
    LOCAL_TZ = timezone.utc

NO_MENTIONS = discord.AllowedMentions.none()
TEXT_LIMIT = 1500  # Caracteres de cada texto citado (la tarjeta entera admite 4000)
TRANSCRIPT_FILE = "borrado_masivo.txt"


def truncate(text: str, limit: int) -> str:
    """Recorta un texto a `limit` caracteres."""
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _quote(text: str) -> str:
    """Cita el texto (con > delante de cada línea) para que se distinga de los datos del registro."""
    return "\n".join(f"> {line}" for line in text.splitlines()) or "> *(sin texto)*"


async def send_deletion_log(
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
    Publica en el canal de administración una tarjeta sobre un mensaje eliminado.
    Si se editó antes de borrarlo, enseña también qué ponía al principio.
    Devuelve True si se envió.
    """
    try:
        sent_at = int(discord.utils.snowflake_time(message_id).timestamp())
        card = tarjetas.Card(tarjetas.DANGER)
        card.text(
            "### 🗑️ Mensaje eliminado\n"
            f"**Autor:** {author_display}\n"
            f"**Eliminado por:** {executor_display}\n"
            f"**Canal:** {channel_display}\n"
            f"**Enviado:** <t:{sent_at}:f> (<t:{sent_at}:R>)"
        )
        card.separator()
        card.text(_quote(truncate(content, TEXT_LIMIT)) if content else "*(sin contenido de texto)*")
        if original_content:
            card.text("**✏️ Antes de editarlo:**\n" + _quote(truncate(original_content, TEXT_LIMIT)))
        card.note(f"ID del mensaje: {message_id} · borrado <t:{int(time.time())}:f>")

        await admin_channel.send(view=card, allowed_mentions=NO_MENTIONS)
        logger.info(f"Notificación de eliminación enviada para mensaje {message_id}")
        return True

    except discord.Forbidden:
        logger.error(f"Sin permisos para enviar mensajes en {admin_channel.name}")
        return False
    except Exception as e:
        logger.error(f"Error al registrar el mensaje eliminado: {e}")
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
        card = tarjetas.Card(discord.Colour.dark_red())
        card.text(
            "### 🧹 Borrado masivo\n"
            f"**Mensajes borrados:** {total}\n"
            f"**Canal:** {channel_display}\n"
            f"**Borrado por:** {executor_display}\n"
            f"**Recuperados:** {len(records)} de {total}"
        )

        transcript = "\n".join(_transcript_line(r) for r in records)
        file = None
        if transcript:
            card.separator()
            if admin_channel.permissions_for(admin_channel.guild.me).attach_files:
                file = discord.File(io.BytesIO(transcript.encode("utf-8")), filename=TRANSCRIPT_FILE)
                card.attachment(TRANSCRIPT_FILE)  # En las tarjetas, los adjuntos solo se ven si se incluyen
            else:
                card.text(_quote(truncate(transcript, TEXT_LIMIT)))
                card.note("Dale al bot el permiso «Adjuntar archivos» en este canal para recibir la transcripción "
                          "completa como archivo.")
        card.note(f"Registrado <t:{int(time.time())}:f>")

        if file:
            await admin_channel.send(view=card, file=file, allowed_mentions=NO_MENTIONS)
        else:
            await admin_channel.send(view=card, allowed_mentions=NO_MENTIONS)
        logger.info(f"Borrado masivo registrado: {total} mensajes")
        return True

    except discord.Forbidden:
        logger.error(f"Sin permisos para enviar mensajes en {admin_channel.name}")
        return False
    except Exception as e:
        logger.error(f"Error al registrar el borrado masivo: {e}")
        return False
