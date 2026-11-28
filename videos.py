"""
Qué mensajes llevan un vídeo o un audio (lo usan el top de clips y las reglas de moderación «solo vídeos» y
«solo audios»). Todo vale también dentro de un mensaje reenviado.

- Vídeo: un archivo de vídeo adjunto o un enlace que Discord enseña como vídeo (YouTube, Medal, Streamable...).
  Los GIF no valen: ni el archivo .gif ni los enlaces de Tenor o Giphy (Discord los enseña como «gifv»,
  aunque por dentro sean un vídeo).
- Audio: un archivo de audio adjunto o una nota de voz. Los enlaces no (no se sabe si son solo audio).
"""

import re
from typing import List

import discord

VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
AUDIO_EXTENSIONS = (".mp3", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".flac", ".aac", ".wma")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def sources(message: discord.Message) -> list:
    """El mensaje y, si es un reenvío, los mensajes reenviados (traen su texto, sus archivos y sus vistas previas)."""
    return [message, *(getattr(message, "message_snapshots", None) or [])]


def is_video_file(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if "gif" in content_type:
        return False
    return content_type.startswith("video/") or attachment.filename.lower().endswith(VIDEO_EXTENSIONS)


def is_audio_file(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return (content_type.startswith("audio/") or attachment.filename.lower().endswith(AUDIO_EXTENSIONS)
            or is_voice_message(attachment))


def is_voice_message(attachment: discord.Attachment) -> bool:
    check = getattr(attachment, "is_voice_message", None)
    return bool(check and check())


def is_video_embed(embed: discord.Embed) -> bool:
    if getattr(embed, "type", None) == "gifv":
        return False
    video = getattr(embed, "video", None)
    return getattr(embed, "type", None) == "video" or bool(getattr(video, "url", None))


def video_files(message: discord.Message) -> List[discord.Attachment]:
    return [a for source in sources(message) for a in (getattr(source, "attachments", None) or []) if is_video_file(a)]


def audio_files(message: discord.Message) -> List[discord.Attachment]:
    return [a for source in sources(message) for a in (getattr(source, "attachments", None) or []) if is_audio_file(a)]


def video_embeds(message: discord.Message) -> List[discord.Embed]:
    return [e for source in sources(message) for e in (getattr(source, "embeds", None) or []) if is_video_embed(e)]


def links(message: discord.Message) -> List[str]:
    return [url for source in sources(message) for url in URL_RE.findall(getattr(source, "content", None) or "")]


def waiting_preview(message: discord.Message) -> bool:
    """Lleva enlaces pero aún ninguna vista previa (Discord la añade unos segundos después de mandarlo)."""
    return bool(links(message)) and not any(getattr(source, "embeds", None) for source in sources(message))


def has_video(message: discord.Message) -> bool:
    return bool(video_files(message) or video_embeds(message))


def has_audio(message: discord.Message) -> bool:
    return bool(audio_files(message))
