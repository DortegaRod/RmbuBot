"""
Qué mensajes llevan un vídeo (lo usan el top de clips y la regla de moderación «solo vídeos»).

Vale un archivo de vídeo adjunto o un enlace que Discord enseña como vídeo (YouTube, Medal, Streamable...),
también dentro de un mensaje reenviado. Los GIF no valen: ni el archivo .gif ni los enlaces de Tenor o Giphy
(Discord los enseña como «gifv», aunque por dentro sean un vídeo).
"""

import re
from typing import List

import discord

VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def sources(message: discord.Message) -> list:
    """El mensaje y, si es un reenvío, los mensajes reenviados (traen su texto, sus archivos y sus vistas previas)."""
    return [message, *(getattr(message, "message_snapshots", None) or [])]


def is_video_file(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if "gif" in content_type:
        return False
    return content_type.startswith("video/") or attachment.filename.lower().endswith(VIDEO_EXTENSIONS)


def is_video_embed(embed: discord.Embed) -> bool:
    if getattr(embed, "type", None) == "gifv":
        return False
    video = getattr(embed, "video", None)
    return getattr(embed, "type", None) == "video" or bool(getattr(video, "url", None))


def video_files(message: discord.Message) -> List[discord.Attachment]:
    return [a for source in sources(message) for a in (getattr(source, "attachments", None) or []) if is_video_file(a)]


def video_embeds(message: discord.Message) -> List[discord.Embed]:
    return [e for source in sources(message) for e in (getattr(source, "embeds", None) or []) if is_video_embed(e)]


def links(message: discord.Message) -> List[str]:
    return [url for source in sources(message) for url in URL_RE.findall(getattr(source, "content", None) or "")]


def waiting_preview(message: discord.Message) -> bool:
    """Lleva enlaces pero aún ninguna vista previa (Discord la añade unos segundos después de mandarlo)."""
    return bool(links(message)) and not any(getattr(source, "embeds", None) for source in sources(message))


def has_video(message: discord.Message) -> bool:
    return bool(video_files(message) or video_embeds(message))
