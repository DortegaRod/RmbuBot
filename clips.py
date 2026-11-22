"""
Top de clips: en los canales que elige un admin con /clips, el bot mantiene como último mensaje un top 10
de los clips más votados de siempre.

- Un clip es un mensaje con un enlace o un vídeo adjunto (de una persona, no de un bot).
- Se vota reaccionando con el emoji del canal (por defecto ✅; se puede elegir otro, también del servidor).
  Cuenta cada reacción con ese emoji, también la del autor; las de los bots, no.
- Cuando alguien escribe en el canal, el bot borra su top y lo vuelve a publicar abajo (espera unos segundos
  sin mensajes nuevos, para no repetirlo con cada mensaje de una ráfaga). Cuando cambian los votos, edita el
  top donde está.
- Al activar un canal y al arrancar el bot repasa el historial del canal: cuenta los clips y votos que ya
  había y quita los clips borrados mientras estaba apagado.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Union
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

import db
import tarjetas
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)

TOP_SIZE = 10            # Clips en el top
DEFAULT_EMOJI = "✅"     # Reacción que vota, si el admin no elige otra
REPOST_DELAY = 3.0       # Segundos sin mensajes nuevos antes de volver a publicar el top abajo
REFRESH_DELAY = 2.0      # Segundos que se agrupan los cambios de votos antes de editar el top
HISTORY_LIMIT = 5000     # Mensajes del historial que se repasan por canal (al activarlo y al arrancar)
LABEL_LENGTH = 60        # Largo máximo de la descripción de un clip en el top
COLOUR = discord.Colour.purple()
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".gif")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# ==========================================
# QUÉ ES UN CLIP
# ==========================================

def _is_video(attachment: discord.Attachment) -> bool:
    content_type = attachment.content_type or ""
    return content_type.startswith("video/") or attachment.filename.lower().endswith(VIDEO_EXTENSIONS)


def clip_label(message: discord.Message) -> Optional[str]:
    """Descripción corta del clip (su texto, el nombre del vídeo o la web del enlace); None si no es un clip."""
    if message.author.bot or message.is_system():
        return None
    links = URL_RE.findall(message.content or "")
    videos = [attachment for attachment in message.attachments if _is_video(attachment)]
    if not links and not videos:
        return None
    text = " ".join(URL_RE.sub(" ", message.content or "").split())
    if not text:
        text = videos[0].filename if videos else f"Clip de {urlparse(links[0]).netloc.removeprefix('www.')}"
    return text if len(text) <= LABEL_LENGTH else text[:LABEL_LENGTH - 1].rstrip() + "…"


def emoji_key(emoji: Union[str, discord.PartialEmoji, discord.Emoji]) -> str:
    """Clave para comparar emojis: el ID si es propio; si no, el emoji sin el selector de variante."""
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji)
    if getattr(emoji, "id", None):
        return str(emoji.id)
    return (getattr(emoji, "name", None) or str(emoji)).replace("️", "")


def count_votes(message: discord.Message, emoji: str) -> int:
    """Votos de un mensaje: sus reacciones con ese emoji, sin la del propio bot."""
    key = emoji_key(emoji)
    for reaction in message.reactions:
        if emoji_key(reaction.emoji) == key:
            return reaction.count - (1 if reaction.me else 0)
    return 0


# ==========================================
# BASE DE DATOS
# ==========================================

@dataclass
class ClipChannel:
    guild_id: int
    emoji: str
    top_message_id: Optional[int] = None


@dataclass
class Clip:
    message_id: int
    author_id: int
    votes: int
    created_at: float
    label: str


def init_db():
    with db.get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS clips_canales (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            top_message_id INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS clips (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            votes INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            label TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS clips_por_canal ON clips (channel_id, votes)")


def load_channels() -> Dict[int, ClipChannel]:
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT channel_id, guild_id, emoji, top_message_id FROM clips_canales").fetchall()
    return {channel_id: ClipChannel(guild_id, emoji, top_id) for channel_id, guild_id, emoji, top_id in rows}


def save_channel(channel_id: int, config: Optional[ClipChannel]):
    """Guarda la configuración de un canal; con None lo quita, con sus clips."""
    with db.get_db_connection() as conn:
        if config is None:
            conn.execute("DELETE FROM clips_canales WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM clips WHERE channel_id = ?", (channel_id,))
        else:
            conn.execute("INSERT INTO clips_canales (channel_id, guild_id, emoji, top_message_id) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(channel_id) DO UPDATE SET emoji = excluded.emoji, "
                         "top_message_id = excluded.top_message_id",
                         (channel_id, config.guild_id, config.emoji, config.top_message_id))


def save_clips(channel_id: int, clips: Iterable[Clip]):
    with db.get_db_connection() as conn:
        conn.executemany("INSERT INTO clips (message_id, channel_id, author_id, votes, created_at, label) "
                         "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET votes = excluded.votes, "
                         "label = excluded.label",
                         [(c.message_id, channel_id, c.author_id, c.votes, c.created_at, c.label) for c in clips])


def forget_clips(message_ids: Iterable[int]) -> int:
    """Quita clips borrados. Devuelve cuántos había."""
    ids = list(message_ids)
    if not ids:
        return 0
    with db.get_db_connection() as conn:
        return conn.execute(f"DELETE FROM clips WHERE message_id IN ({','.join('?' * len(ids))})", ids).rowcount


def replace_scanned(channel_id: int, clips: List[Clip], oldest_scanned: Optional[float], complete: bool):
    """
    Guarda lo encontrado al repasar el historial y quita los clips que ya no están: todos los que falten si se
    repasó el canal entero; si no, solo los que falten de la parte repasada.
    """
    with db.get_db_connection() as conn:
        found = [clip.message_id for clip in clips]
        marks = ",".join("?" * len(found)) or "NULL"
        if complete:
            conn.execute(f"DELETE FROM clips WHERE channel_id = ? AND message_id NOT IN ({marks})", (channel_id, *found))
        elif oldest_scanned is not None:
            conn.execute(f"DELETE FROM clips WHERE channel_id = ? AND created_at >= ? AND message_id NOT IN ({marks})",
                         (channel_id, oldest_scanned, *found))
    save_clips(channel_id, clips)


def is_clip(message_id: int) -> bool:
    with db.get_db_connection() as conn:
        return conn.execute("SELECT 1 FROM clips WHERE message_id = ?", (message_id,)).fetchone() is not None


def top_clips(channel_id: int, limit: int = TOP_SIZE) -> List[Clip]:
    """Los más votados (con al menos un voto); a igualdad, el que se publicó antes."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT message_id, author_id, votes, created_at, label FROM clips "
                            "WHERE channel_id = ? AND votes > 0 ORDER BY votes DESC, created_at ASC LIMIT ?",
                            (channel_id, limit)).fetchall()
    return [Clip(*row) for row in rows]


def clip_from(message: discord.Message, emoji: str) -> Optional[Clip]:
    label = clip_label(message)
    if label is None:
        return None
    return Clip(message.id, message.author.id, count_votes(message, emoji), message.created_at.timestamp(), label)


# ==========================================
# LA TARJETA DEL TOP
# ==========================================

def _link_text(label: str) -> str:
    """El texto de un enlace [texto](url), sin formato ni corchetes que lo rompan."""
    return escape_markdown(label).replace("[", "(").replace("]", ")")


def top_card(guild_id: int, channel_id: int, emoji: str, clips: List[Clip]) -> tarjetas.Card:
    card = tarjetas.Card(COLOUR)
    lines = []
    for position, clip in enumerate(clips, 1):
        url = f"https://discord.com/channels/{guild_id}/{channel_id}/{clip.message_id}"
        medal = MEDALS.get(position, f"**{position}.**")
        votes = "voto" if clip.votes == 1 else "votos"
        lines.append(f"{medal} [{_link_text(clip.label)}]({url})\n"
                     f"-# {emoji} **{clip.votes}** {votes} · de <@{clip.author_id}> · <t:{int(clip.created_at)}:d>")
    card.text(f"## 🎬 Top {TOP_SIZE} clips\n" + ("\n".join(lines) or "Aún no hay clips con votos."))
    card.separator()
    card.note(f"Vota un clip reaccionando con {emoji}. Cuenta cualquier mensaje con un enlace o un vídeo.")
    return card


# ==========================================
# COG
# ==========================================

class ClipsCog(commands.Cog):
    """/clips y el top de cada canal de clips."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channels: Dict[int, ClipChannel] = {}             # Canal -> configuración
        self.locks: Dict[int, asyncio.Lock] = {}               # Canal -> uno a uno al publicar o editar el top
        self.repost_tasks: Dict[int, asyncio.Task] = {}        # Canal -> volver a publicar el top (pendiente)
        self.refresh_tasks: Dict[int, asyncio.Task] = {}       # Canal -> editar el top (pendiente)
        self.startup_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        init_db()
        self.channels = await asyncio.to_thread(load_channels)
        self.startup_task = asyncio.create_task(self._startup())

    async def cog_unload(self):
        for task in [self.startup_task, *self.repost_tasks.values(), *self.refresh_tasks.values()]:
            if task and not task.done():
                task.cancel()

    async def _startup(self):
        await self.bot.wait_until_ready()
        for channel_id in list(self.channels):
            try:
                await self.sync_channel(channel_id)
            except Exception:
                logger.exception(f"🎬 Clips: no pude repasar el canal {channel_id}")

    def _channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        config = self.channels.get(channel_id)
        guild = self.bot.get_guild(config.guild_id) if config else None
        channel = guild.get_channel(channel_id) if guild else None
        return channel if isinstance(channel, discord.TextChannel) else None

    # ---------- Repasar el canal ----------

    async def sync_channel(self, channel_id: int):
        """Cuenta los clips y votos del historial y deja el top como último mensaje."""
        channel, config = self._channel(channel_id), self.channels.get(channel_id)
        if channel is None or config is None:
            return
        clips: List[Clip] = []
        scanned, oldest = 0, None
        try:
            async for message in channel.history(limit=HISTORY_LIMIT):
                scanned += 1
                oldest = message.created_at.timestamp()
                clip = clip_from(message, config.emoji)
                if clip is not None:
                    clips.append(clip)
        except discord.HTTPException as e:
            logger.warning(f"🎬 Clips: no pude leer el historial de #{channel} ({e})")
            return
        await asyncio.to_thread(replace_scanned, channel_id, clips, oldest, scanned < HISTORY_LIMIT)
        logger.info(f"🎬 Clips: #{channel} repasado ({scanned} mensajes, {len(clips)} clips)")
        if channel.last_message_id is not None and channel.last_message_id == config.top_message_id:
            await self.refresh(channel_id)
        else:
            await self.repost(channel_id)

    # ---------- Publicar y editar el top ----------

    def _lock(self, channel_id: int) -> asyncio.Lock:
        return self.locks.setdefault(channel_id, asyncio.Lock())

    async def _card(self, channel_id: int) -> Optional[tarjetas.Card]:
        config = self.channels.get(channel_id)
        if config is None:
            return None
        clips = await asyncio.to_thread(top_clips, channel_id)
        return top_card(config.guild_id, channel_id, config.emoji, clips)

    def _schedule(self, tasks: Dict[int, asyncio.Task], channel_id: int, delay: float, action):
        """Hace `action` dentro de `delay` segundos; si ya había una pendiente, empieza a contar de nuevo."""
        pending = tasks.get(channel_id)
        if pending and not pending.done():
            pending.cancel()

        async def later():
            await asyncio.sleep(delay)
            tasks.pop(channel_id, None)
            await action(channel_id)
        tasks[channel_id] = asyncio.create_task(later())

    def schedule_repost(self, channel_id: int):
        self._schedule(self.repost_tasks, channel_id, REPOST_DELAY, self.repost)

    def schedule_refresh(self, channel_id: int):
        pending = self.repost_tasks.get(channel_id)
        if pending and not pending.done():
            return  # Ya se va a publicar de nuevo, con los votos al día
        self._schedule(self.refresh_tasks, channel_id, REFRESH_DELAY, self.refresh)

    async def repost(self, channel_id: int):
        """Publica el top abajo del todo y borra el anterior."""
        async with self._lock(channel_id):
            channel, config, card = self._channel(channel_id), self.channels.get(channel_id), await self._card(channel_id)
            if channel is None or config is None or card is None:
                return
            if channel.last_message_id is not None and channel.last_message_id == config.top_message_id:
                return  # Ya es el último mensaje
            try:
                message = await channel.send(view=card, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as e:
                logger.warning(f"🎬 Clips: no pude publicar el top en #{channel} ({e})")
                return
            old_id, config.top_message_id = config.top_message_id, message.id
            await asyncio.to_thread(save_channel, channel_id, config)
            if old_id is not None:
                await self._delete(channel, old_id)

    async def refresh(self, channel_id: int):
        """Actualiza el top donde está; si ya no existe, lo publica de nuevo."""
        async with self._lock(channel_id):
            channel, config, card = self._channel(channel_id), self.channels.get(channel_id), await self._card(channel_id)
            if channel is None or config is None or card is None:
                return
            if config.top_message_id is not None:
                try:
                    await channel.get_partial_message(config.top_message_id).edit(view=card,
                                                                                  allowed_mentions=NO_MENTIONS)
                    return
                except discord.NotFound:
                    config.top_message_id = None
                except discord.HTTPException as e:
                    logger.warning(f"🎬 Clips: no pude actualizar el top de #{channel} ({e})")
                    return
        await self.repost(channel_id)

    @staticmethod
    async def _delete(channel: discord.TextChannel, message_id: int):
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"🎬 Clips: no pude borrar el top anterior de #{channel} ({e})")

    # ---------- Mensajes y reacciones ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        config = self.channels.get(message.channel.id)
        if config is None or message.author.id == self.bot.user.id:
            return
        clip = clip_from(message, config.emoji)
        if clip is not None:
            await asyncio.to_thread(save_clips, message.channel.id, [clip])
        self.schedule_repost(message.channel.id)

    async def recount(self, channel_id: int, message_id: int):
        """Vuelve a contar los votos de un mensaje (pidiéndoselo a Discord) y actualiza el top."""
        config, channel = self.channels.get(channel_id), self._channel(channel_id)
        if config is None or channel is None or message_id == config.top_message_id:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            return
        clip = clip_from(message, config.emoji)
        if clip is None:
            return
        await asyncio.to_thread(save_clips, channel_id, [clip])
        self.schedule_refresh(channel_id)

    def _votes_with(self, channel_id: int, emoji: Union[str, discord.PartialEmoji]) -> bool:
        config = self.channels.get(channel_id)
        return config is not None and emoji_key(emoji) == emoji_key(config.emoji)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self._votes_with(payload.channel_id, payload.emoji):
            await self.recount(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if self._votes_with(payload.channel_id, payload.emoji):
            await self.recount(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        if payload.channel_id in self.channels and await asyncio.to_thread(is_clip, payload.message_id):
            await self.recount(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent):
        if self._votes_with(payload.channel_id, payload.emoji):
            await self.recount(payload.channel_id, payload.message_id)

    async def _deleted(self, channel_id: int, message_ids: List[int]):
        config = self.channels.get(channel_id)
        if config is None:
            return
        if config.top_message_id in message_ids:
            config.top_message_id = None  # Alguien ha borrado el top: vuelve a salir abajo
            await asyncio.to_thread(save_channel, channel_id, config)
            self.schedule_repost(channel_id)
        if await asyncio.to_thread(forget_clips, message_ids):
            self.schedule_refresh(channel_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self._deleted(payload.channel_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        await self._deleted(payload.channel_id, list(payload.message_ids))

    # ---------- /clips (admins) ----------

    def _list_card(self, guild: discord.Guild) -> tarjetas.Card:
        lines = [f"🎬 <#{channel_id}> · se vota con {config.emoji}"
                 for channel_id, config in self.channels.items() if config.guild_id == guild.id]
        card = tarjetas.Card(tarjetas.INFO)
        card.text("### 🎬 Canales de clips\n" + ("\n".join(lines) or "Ningún canal todavía."))
        card.note(f"En cada uno, el último mensaje es siempre el top {TOP_SIZE} de clips más votados. "
                  f"Añade uno con {tarjetas.cmd('clips')}.")
        return card

    @app_commands.command(name="clips", description="Canales con el top 10 de clips más votados (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de clips (vacío = ver los que hay)",
                           emoji=f"Reacción que vota (por defecto {DEFAULT_EMOJI}; vale un emoji del servidor)",
                           quitar="Que ese canal deje de tener top de clips")
    async def clips(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None,
                    emoji: Optional[str] = None, quitar: bool = False):
        guild = interaction.guild
        if canal is None:
            return await interaction.response.send_message(view=self._list_card(guild), ephemeral=True,
                                                           allowed_mentions=NO_MENTIONS)
        if quitar:
            config = self.channels.pop(canal.id, None)
            for tasks in (self.repost_tasks, self.refresh_tasks):
                task = tasks.pop(canal.id, None)
                if task and not task.done():
                    task.cancel()
            await asyncio.to_thread(save_channel, canal.id, None)
            if config and config.top_message_id:
                await self._delete(canal, config.top_message_id)
            return await interaction.response.send_message(f"✅ {canal.mention} ya no tiene top de clips.",
                                                           ephemeral=True)

        chosen = (emoji or "").strip() or (self.channels[canal.id].emoji if canal.id in self.channels
                                            else DEFAULT_EMOJI)
        parsed = discord.PartialEmoji.from_str(chosen)
        if parsed.id is None and (len(chosen) > 8 or re.search(r"[A-Za-z<>:]", chosen) or chosen.isdigit()):
            return await interaction.response.send_message(
                "❌ Eso no parece un emoji. Escribe solo el emoji (por ejemplo ✅ o uno del servidor).", ephemeral=True
            )
        perms = canal.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.read_message_history):
            return await interaction.response.send_message(
                f"❌ En {canal.mention} necesito **Ver canal**, **Enviar mensajes** y **Leer el historial**.",
                ephemeral=True
            )
        previous = self.channels.get(canal.id)
        config = ClipChannel(guild.id, str(parsed), previous.top_message_id if previous else None)
        self.channels[canal.id] = config
        await asyncio.to_thread(save_channel, canal.id, config)
        await interaction.response.send_message(
            f"✅ {canal.mention} tiene ahora top de clips: se vota reaccionando con {config.emoji} a cualquier "
            f"mensaje con un enlace o un vídeo, y el top {TOP_SIZE} será siempre el último mensaje. Estoy "
            f"contando los clips que ya hay.", ephemeral=True, allowed_mentions=NO_MENTIONS
        )
        await self.sync_channel(canal.id)
