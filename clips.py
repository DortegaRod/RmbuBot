"""
Top de clips: en los canales que elige un admin con /clips, el bot mantiene como último mensaje un top de
los clips más votados de siempre.

- Vale un canal de texto o un foro. En un foro, cada publicación tiene su propio top, con sus clips, como
  último mensaje de esa publicación (las publicaciones nuevas lo tienen solas). Aquí, al canal de texto o a
  la publicación donde va un top se le llama «sitio».
- Un clip es un mensaje con un vídeo adjunto o un enlace que Discord enseña como vídeo, también reenviado (de una
  persona, no de un bot). Los GIF no cuentan (ver videos.py).
- Si se edita un clip, el top se actualiza con su título nuevo.
- El top tiene todos los clips (también los que no tienen votos), 10 por página. Cada puesto lleva un botón
  ▶️ Ver que lleva al clip. Las flechas abren las otras páginas solo para quien las pulsa.
- Se vota reaccionando con el emoji del canal (por defecto ✅; se puede elegir otro, también del servidor).
  Cuenta cada reacción con ese emoji, también la del autor; las de los bots, no.
- Cuando alguien escribe en un sitio, el bot borra su top y lo vuelve a publicar abajo (espera unos segundos
  sin mensajes nuevos, para no repetirlo con cada mensaje de una ráfaga). Cuando cambian los votos, edita el
  top donde está.
- Al activar un canal y al arrancar el bot repasa el historial (en un foro, el de cada publicación, también
  las archivadas): cuenta los clips y votos que ya había y quita los clips borrados mientras estaba apagado.
  Publicar el top en una publicación archivada la vuelve a abrir (Discord la archiva otra vez sola). Al
  arrancar, una archivada que ya tiene su top abajo no se toca: nadie ha escrito en ella.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Union
from urllib.parse import urlparse

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.utils import escape_markdown

import db
import tarjetas
import videos
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)

PAGE_SIZE = 10           # Clips por página del top
DEFAULT_EMOJI = "✅"     # Reacción que vota, si el admin no elige otra
REPOST_DELAY = 3.0       # Segundos sin mensajes nuevos antes de volver a publicar el top abajo
REFRESH_DELAY = 2.0      # Segundos que se agrupan los cambios de votos antes de editar el top
HISTORY_LIMIT = 5000     # Mensajes del historial que se repasan por sitio (al activarlo y al arrancar)
LABEL_LENGTH = 60        # Largo máximo de la descripción de un clip en el top
COLOUR = discord.Colour.purple()
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
URL_RE = videos.URL_RE
MENTION_RE = re.compile(r"<(?:@[!&]?|#)\d+>")  # En un reenvío no hay clean_content: las menciones se quitan
LINE_MARK_RE = re.compile(r"^\s*(?:#{1,3}|-#|>{1,3}|[-*+]|\d+\.)\s+", re.MULTILINE)  # Títulos, citas, listas
CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
TIMESTAMP_RE = re.compile(r"<t:(-?\d+)(?::[tTdDfFR])?>")

Place = Union[discord.TextChannel, discord.Thread]  # Donde va un top: un canal de texto o una publicación


# ==========================================
# QUÉ ES UN CLIP
# ==========================================

def plain_text(message: discord.Message) -> str:
    """
    El texto del mensaje sin formato, para usarlo como texto de un enlace: las menciones pasan a @nombre
    (Discord no deja menciones dentro de un enlace y lo rompe), sin títulos, citas ni listas, y sin enlaces.
    """
    text = getattr(message, "clean_content", None) or message.content or ""
    text = MENTION_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = LINE_MARK_RE.sub("", text)
    text = CUSTOM_EMOJI_RE.sub(r":\1:", text)
    text = TIMESTAMP_RE.sub("", text)
    text = re.sub(r"[*~`|]+|__", "", text)  # Negrita, cursiva, tachado, código y spoilers (los _ sueltos son de nombres)
    return " ".join(text.split())


def clip_label(message: discord.Message) -> Optional[str]:
    """
    Descripción corta del clip: su texto (o el del mensaje reenviado); si no tiene, el título de la vista previa
    del vídeo (p. ej. el de YouTube), el nombre del archivo o la web del enlace. None si no es un clip.
    """
    if message.author.bot or message.is_system() or not videos.has_video(message):
        return None
    text = next((found for found in map(plain_text, videos.sources(message)) if found), "")
    if not text:
        titles = [embed.title for embed in videos.video_embeds(message) if getattr(embed, "title", None)]
        files = videos.video_files(message)
        links = videos.links(message)
        if titles:
            text = " ".join(titles[0].split())
        elif files:
            text = files[0].filename
        elif links:
            text = f"Clip de {urlparse(links[0]).netloc.removeprefix('www.')}"
        else:
            text = "Clip"
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
    """Un canal elegido con /clips."""
    guild_id: int
    emoji: str
    forum: bool = False


@dataclass
class TopSpot:
    """Un sitio con top: de qué canal elegido es (él mismo, o el foro de la publicación) y su mensaje de top."""
    parent_id: int
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(clips_canales)")}
        if "foro" not in columns:
            conn.execute("ALTER TABLE clips_canales ADD COLUMN foro INTEGER NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS clips_sitios (
            place_id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL,
            top_message_id INTEGER
        )""")
        # Antes el top de cada canal se guardaba en clips_canales (solo había canales de texto)
        conn.execute("INSERT OR IGNORE INTO clips_sitios (place_id, parent_id, top_message_id) "
                     "SELECT channel_id, channel_id, top_message_id FROM clips_canales WHERE foro = 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS clips (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            votes INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            label TEXT NOT NULL
        )""")  # channel_id: el sitio del clip (el canal de texto o la publicación del foro)
        conn.execute("CREATE INDEX IF NOT EXISTS clips_por_canal ON clips (channel_id, votes)")


def load_channels() -> Dict[int, ClipChannel]:
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT channel_id, guild_id, emoji, foro FROM clips_canales").fetchall()
    return {channel_id: ClipChannel(guild_id, emoji, bool(forum)) for channel_id, guild_id, emoji, forum in rows}


def load_spots() -> Dict[int, TopSpot]:
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT place_id, parent_id, top_message_id FROM clips_sitios").fetchall()
    return {place_id: TopSpot(parent_id, top_id) for place_id, parent_id, top_id in rows}


def save_channel(channel_id: int, config: Optional[ClipChannel]):
    """Guarda un canal elegido; con None lo quita, con sus sitios y sus clips."""
    with db.get_db_connection() as conn:
        if config is None:
            conn.execute("DELETE FROM clips WHERE channel_id IN "
                         "(SELECT place_id FROM clips_sitios WHERE parent_id = ?)", (channel_id,))
            conn.execute("DELETE FROM clips_sitios WHERE parent_id = ?", (channel_id,))
            conn.execute("DELETE FROM clips_canales WHERE channel_id = ?", (channel_id,))
        else:
            conn.execute("INSERT INTO clips_canales (channel_id, guild_id, emoji, foro) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(channel_id) DO UPDATE SET emoji = excluded.emoji, foro = excluded.foro",
                         (channel_id, config.guild_id, config.emoji, int(config.forum)))


def save_spot(place_id: int, spot: Optional[TopSpot]):
    """Guarda un sitio; con None lo quita, con sus clips (p. ej. si se borra la publicación)."""
    with db.get_db_connection() as conn:
        if spot is None:
            conn.execute("DELETE FROM clips WHERE channel_id = ?", (place_id,))
            conn.execute("DELETE FROM clips_sitios WHERE place_id = ?", (place_id,))
        else:
            conn.execute("INSERT INTO clips_sitios (place_id, parent_id, top_message_id) VALUES (?, ?, ?) "
                         "ON CONFLICT(place_id) DO UPDATE SET top_message_id = excluded.top_message_id",
                         (place_id, spot.parent_id, spot.top_message_id))


def save_clips(place_id: int, clips: Iterable[Clip]):
    with db.get_db_connection() as conn:
        conn.executemany("INSERT INTO clips (message_id, channel_id, author_id, votes, created_at, label) "
                         "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET votes = excluded.votes, "
                         "label = excluded.label",
                         [(c.message_id, place_id, c.author_id, c.votes, c.created_at, c.label) for c in clips])


def forget_clips(message_ids: Iterable[int]) -> int:
    """Quita clips borrados. Devuelve cuántos había."""
    ids = list(message_ids)
    if not ids:
        return 0
    with db.get_db_connection() as conn:
        return conn.execute(f"DELETE FROM clips WHERE message_id IN ({','.join('?' * len(ids))})", ids).rowcount


def replace_scanned(place_id: int, clips: List[Clip], oldest_scanned: Optional[float], complete: bool):
    """
    Guarda lo encontrado al repasar el historial y quita los clips que ya no están: todos los que falten si se
    repasó el sitio entero; si no, solo los que falten de la parte repasada.
    """
    with db.get_db_connection() as conn:
        found = [clip.message_id for clip in clips]
        marks = ",".join("?" * len(found)) or "NULL"
        if complete:
            conn.execute(f"DELETE FROM clips WHERE channel_id = ? AND message_id NOT IN ({marks})", (place_id, *found))
        elif oldest_scanned is not None:
            conn.execute(f"DELETE FROM clips WHERE channel_id = ? AND created_at >= ? AND message_id NOT IN ({marks})",
                         (place_id, oldest_scanned, *found))
    save_clips(place_id, clips)


def is_clip(message_id: int) -> bool:
    with db.get_db_connection() as conn:
        return conn.execute("SELECT 1 FROM clips WHERE message_id = ?", (message_id,)).fetchone() is not None


def count_clips(place_id: int) -> int:
    with db.get_db_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM clips WHERE channel_id = ?", (place_id,)).fetchone()[0]


def clips_page(place_id: int, page: int, per_page: int = PAGE_SIZE) -> List[Clip]:
    """Una página de todos los clips de un sitio (también los que no tienen votos), de más a menos votados."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT message_id, author_id, votes, created_at, label FROM clips WHERE channel_id = ? "
                            "ORDER BY votes DESC, created_at ASC LIMIT ? OFFSET ?",
                            (place_id, per_page, (page - 1) * per_page)).fetchall()
    return [Clip(*row) for row in rows]


def clip_from(message: discord.Message, emoji: str) -> Optional[Clip]:
    label = clip_label(message)
    if label is None:
        return None
    return Clip(message.id, message.author.id, count_votes(message, emoji), message.created_at.timestamp(), label)


# ==========================================
# LA TARJETA DEL TOP
# ==========================================

def clip_url(guild_id: int, place_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{place_id}/{message_id}"


def _clip_rows(card: tarjetas.Card, guild_id: int, place_id: int, emoji: str, clips: List[Clip], first: int):
    """
    Un bloque por clip: puesto, título y votos, con un botón ▶️ Ver que lleva al mensaje. Con el enlace en un
    botón, el título puede llevar cualquier cosa (dentro del texto de un enlace, Discord rompe los emojis).
    """
    for position, clip in enumerate(clips, first):
        medal = MEDALS.get(position, f"`#{position}`")
        votes = "voto" if clip.votes == 1 else "votos"
        text = f"{medal} **{escape_markdown(clip.label)}**\n-# {emoji} {clip.votes} {votes}"
        button = ui.Button(style=discord.ButtonStyle.link, label="Ver", emoji="▶️",
                           url=clip_url(guild_id, place_id, clip.message_id))
        card.box.add_item(ui.Section(ui.TextDisplay(text), accessory=button))


def top_card(guild_id: int, place_id: int, emoji: str, clips: List[Clip], page: int = 1,
             pages: int = 1) -> tarjetas.Card:
    """Una página del top (la 1 es la que va abajo del todo). Si hay más de una, flechas para pasar de página."""
    card = tarjetas.Card(COLOUR)
    card.text("## 🎬 Top clips")
    _clip_rows(card, guild_id, place_id, emoji, clips, (page - 1) * PAGE_SIZE + 1)
    if not clips:
        card.text("Aún no hay clips.")
    if pages > 1:
        card.buttons(ClipsPageButton(place_id, page - 1, emoji="◀️", disabled=page <= 1),
                     ClipsPageButton(place_id, page, label=f"{page} / {pages}", disabled=True, tag="n"),
                     ClipsPageButton(place_id, page + 1, emoji="▶️", disabled=page >= pages))
    return card


class ClipsPageButton(ui.DynamicItem[ui.Button], template=r"reimbou:clips:(?P<place>\d+):(?P<page>\d+)(?::n)?"):
    """
    Las flechas del top. Desde el top público, abren la página solo para quien pulsa; dentro de esa página (que
    ya solo ve esa persona), cambian de página en el mismo mensaje.
    """

    def __init__(self, place_id: int, page: int, *, label: Optional[str] = None, emoji=None,
                 disabled: bool = False, tag: str = ""):
        super().__init__(ui.Button(label=label, emoji=emoji, disabled=disabled, style=discord.ButtonStyle.secondary,
                                   custom_id=f"reimbou:clips:{place_id}:{page}" + (f":{tag}" if tag else "")))
        self.place_id, self.page = place_id, page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(int(match["place"]), int(match["page"]), label=item.label, emoji=item.emoji, disabled=item.disabled)

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._open)

    async def _open(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("ClipsCog")
        card = await cog.card(self.place_id, self.page) if cog else None
        if card is None:
            return await interaction.response.send_message("⌛ Este top ya no está activo.", ephemeral=True)
        if interaction.message is not None and interaction.message.flags.ephemeral:
            await tarjetas.update(interaction, card)
        else:
            await interaction.response.send_message(view=card, ephemeral=True, allowed_mentions=NO_MENTIONS)


# ==========================================
# COG
# ==========================================

class ClipsCog(commands.Cog):
    """/clips y el top de cada sitio."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channels: Dict[int, ClipChannel] = {}             # Canal elegido -> configuración
        self.spots: Dict[int, TopSpot] = {}                    # Sitio -> su canal elegido y su mensaje de top
        self.locks: Dict[int, asyncio.Lock] = {}               # Sitio -> uno a uno al repasar, publicar o editar
        self.repost_tasks: Dict[int, asyncio.Task] = {}        # Sitio -> volver a publicar el top (pendiente)
        self.refresh_tasks: Dict[int, asyncio.Task] = {}       # Sitio -> editar el top (pendiente)
        self.tasks: set = set()                                # Repasos en marcha (para cancelarlos al descargar)
        self.threads: Dict[int, discord.Thread] = {}           # Publicaciones archivadas (no están en la caché)

    async def cog_load(self):
        init_db()
        self.channels = await asyncio.to_thread(load_channels)
        self.spots = await asyncio.to_thread(load_spots)
        self.bot.add_dynamic_items(ClipsPageButton)
        self._spawn(self._startup())

    async def cog_unload(self):
        for task in [*self.tasks, *self.repost_tasks.values(), *self.refresh_tasks.values()]:
            if not task.done():
                task.cancel()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _startup(self):
        await self.bot.wait_until_ready()
        for channel_id in list(self.channels):
            try:
                await self.sync_channel(channel_id)
            except Exception:
                logger.exception(f"🎬 Clips: no pude repasar el canal {channel_id}")

    # ---------- Sitios ----------

    def _guild(self, parent_id: int) -> Optional[discord.Guild]:
        config = self.channels.get(parent_id)
        return self.bot.get_guild(config.guild_id) if config else None

    def _place(self, place_id: int) -> Optional[Place]:
        spot = self.spots.get(place_id)
        guild = self._guild(spot.parent_id) if spot else None
        place = (guild.get_channel_or_thread(place_id) if guild else None) or self.threads.get(place_id)
        return place if isinstance(place, (discord.TextChannel, discord.Thread)) else None

    def _parent_of(self, place_id: int, guild_id: Optional[int], thread_parent: Optional[int] = None) -> Optional[int]:
        """
        El canal elegido al que pertenece un sitio, o None si no tiene top. Si es una publicación de un foro
        elegido que aún no conocía, la apunta como sitio.
        """
        spot = self.spots.get(place_id)
        if spot is not None:
            return spot.parent_id
        if thread_parent is None and guild_id is not None:
            guild = self.bot.get_guild(guild_id)
            thread = guild.get_channel_or_thread(place_id) if guild else None
            thread_parent = thread.parent_id if isinstance(thread, discord.Thread) else None
        config = self.channels.get(thread_parent) if thread_parent else None
        if config is None or not config.forum:
            return None
        self.spots[place_id] = TopSpot(thread_parent)
        return thread_parent

    # ---------- Repasar ----------

    async def sync_channel(self, channel_id: int, everything: bool = False):
        """
        Repasa un canal elegido: él mismo o, si es un foro, cada publicación. Al arrancar se saltan las
        archivadas que ya tienen su top abajo (no han cambiado); con `everything` (/clips) se repasan todas.
        """
        config, guild = self.channels.get(channel_id), self._guild(channel_id)
        channel = guild.get_channel(channel_id) if guild else None
        if config is None or channel is None:
            return
        if isinstance(channel, discord.ForumChannel):
            threads = {thread.id: thread for thread in channel.threads}  # Las abiertas
            try:
                async for thread in channel.archived_threads(limit=None):
                    threads.setdefault(thread.id, thread)
            except discord.HTTPException as e:
                logger.warning(f"🎬 Clips: no pude ver las publicaciones archivadas de #{channel} ({e})")
            for thread in threads.values():
                if thread.archived:
                    self.threads[thread.id] = thread  # Para poder publicar, editar o borrar su top
                spot = self.spots.get(thread.id)
                if (not everything and thread.archived and spot and spot.top_message_id
                        and thread.last_message_id == spot.top_message_id):
                    continue  # Archivada y con su top abajo: no ha cambiado nada
                if self._parent_of(thread.id, guild.id, channel.id) is not None:
                    await self.sync_place(thread.id)
        else:
            self.spots.setdefault(channel_id, TopSpot(channel_id))
            await self.sync_place(channel_id)

    async def sync_place(self, place_id: int):
        """Cuenta los clips y votos del historial de un sitio y deja el top como último mensaje."""
        async with self._lock(place_id):
            place, spot = self._place(place_id), self.spots.get(place_id)
            config = self.channels.get(spot.parent_id) if spot else None
            if place is None or config is None:
                return
            clips: List[Clip] = []
            scanned, oldest = 0, None
            try:
                async for message in place.history(limit=HISTORY_LIMIT):
                    scanned += 1
                    oldest = message.created_at.timestamp()
                    clip = clip_from(message, config.emoji)
                    if clip is not None:
                        clips.append(clip)
            except discord.HTTPException as e:
                logger.warning(f"🎬 Clips: no pude leer el historial de #{place} ({e})")
                return
            await asyncio.to_thread(replace_scanned, place_id, clips, oldest, scanned < HISTORY_LIMIT)
            await asyncio.to_thread(save_spot, place_id, spot)
            logger.info(f"🎬 Clips: #{place} repasado ({scanned} mensajes, {len(clips)} clips)")
        if place.last_message_id is not None and place.last_message_id == spot.top_message_id:
            await self.refresh(place_id)
        else:
            await self.repost(place_id)

    # ---------- Publicar y editar el top ----------

    def _lock(self, place_id: int) -> asyncio.Lock:
        return self.locks.setdefault(place_id, asyncio.Lock())

    async def card(self, place_id: int, page: int = 1) -> Optional[tarjetas.Card]:
        """Una página del top de un sitio (la 1 es la que va abajo); None si ese sitio ya no tiene top."""
        spot = self.spots.get(place_id)
        config = self.channels.get(spot.parent_id) if spot else None
        if config is None:
            return None
        total = await asyncio.to_thread(count_clips, place_id)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(1, page), pages)
        clips = await asyncio.to_thread(clips_page, place_id, page)
        return top_card(config.guild_id, place_id, config.emoji, clips, page, pages)

    def _schedule(self, tasks: Dict[int, asyncio.Task], place_id: int, delay: float, action):
        """Hace `action` dentro de `delay` segundos; si ya había una pendiente, empieza a contar de nuevo."""
        pending = tasks.get(place_id)
        if pending and not pending.done():
            pending.cancel()

        async def later():
            await asyncio.sleep(delay)
            tasks.pop(place_id, None)  # Desde aquí ya no se cancela: no puede quedar un top a medio publicar
            await action(place_id)
        tasks[place_id] = asyncio.create_task(later())

    def schedule_repost(self, place_id: int):
        self._schedule(self.repost_tasks, place_id, REPOST_DELAY, self.repost)

    def schedule_refresh(self, place_id: int):
        pending = self.repost_tasks.get(place_id)
        if pending and not pending.done():
            return  # Ya se va a publicar de nuevo, con los votos al día
        self._schedule(self.refresh_tasks, place_id, REFRESH_DELAY, self.refresh)

    async def repost(self, place_id: int, again: bool = False):
        """Publica el top abajo del todo y borra el anterior (con `again`, aunque ya sea el último mensaje)."""
        async with self._lock(place_id):
            place, spot, card = self._place(place_id), self.spots.get(place_id), await self.card(place_id)
            if place is None or spot is None or card is None:
                return
            if not again and place.last_message_id is not None and place.last_message_id == spot.top_message_id:
                return  # Ya es el último mensaje
            try:
                message = await place.send(view=card, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as e:
                logger.warning(f"🎬 Clips: no pude publicar el top en #{place} ({e})")
                return
            old_id, spot.top_message_id = spot.top_message_id, message.id
            await asyncio.to_thread(save_spot, place_id, spot)
            if old_id is not None:
                await self._delete(place, old_id)

    async def refresh(self, place_id: int):
        """Actualiza el top donde está; si ya no existe, lo publica de nuevo."""
        async with self._lock(place_id):
            place, spot, card = self._place(place_id), self.spots.get(place_id), await self.card(place_id)
            if place is None or spot is None or card is None:
                return
            archived = isinstance(place, discord.Thread) and place.archived
            if spot.top_message_id is not None and not archived:
                try:
                    await place.get_partial_message(spot.top_message_id).edit(view=card, allowed_mentions=NO_MENTIONS)
                    return
                except discord.NotFound:
                    spot.top_message_id = None
                except discord.HTTPException as e:
                    logger.warning(f"🎬 Clips: no pude actualizar el top de #{place} ({e})")
                    return
        # Sin top, o en una publicación archivada (no se puede editar en ellas): se publica de nuevo
        await self.repost(place_id, again=True)

    @staticmethod
    async def _delete(place: Place, message_id: int):
        try:
            await place.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"🎬 Clips: no pude borrar el top anterior de #{place} ({e})")

    # ---------- Mensajes y reacciones ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.id == self.bot.user.id:
            return
        place_id = message.channel.id
        known = place_id in self.spots
        thread_parent = message.channel.parent_id if isinstance(message.channel, discord.Thread) else None
        parent_id = self._parent_of(place_id, message.guild.id, thread_parent)
        if parent_id is None:
            return
        if not known:
            # Publicación nueva (o archivada que vuelve a tener vida): se repasa entera y sale su top
            self._spawn(self.sync_place(place_id))
            return
        clip = clip_from(message, self.channels[parent_id].emoji)
        if clip is not None:
            await asyncio.to_thread(save_clips, place_id, [clip])
        self.schedule_repost(place_id)

    async def recount(self, place_id: int, message_id: int):
        """
        Vuelve a leer un mensaje (pidiéndoselo a Discord): sus votos y su título. Si ha dejado de ser un clip
        (lo han editado y ya no tiene vídeo), sale del top.
        """
        spot, place = self.spots.get(place_id), self._place(place_id)
        config = self.channels.get(spot.parent_id) if spot else None
        if config is None or place is None or message_id == spot.top_message_id:
            return
        try:
            message = await place.fetch_message(message_id)
        except discord.HTTPException:
            return
        clip = clip_from(message, config.emoji)
        if clip is None:
            if await asyncio.to_thread(forget_clips, [message_id]):
                self.schedule_refresh(place_id)
            return
        await asyncio.to_thread(save_clips, place_id, [clip])
        self.schedule_refresh(place_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        """Al editar un clip (o un mensaje que pasa a serlo), el top se actualiza con su título nuevo."""
        if payload.channel_id not in self.spots:
            return
        data = payload.data
        if (data.get("author") or {}).get("bot"):
            return
        may_be_clip = bool(URL_RE.search(data.get("content") or "") or data.get("attachments") or data.get("embeds")
                           or data.get("message_snapshots"))
        if may_be_clip or await asyncio.to_thread(is_clip, payload.message_id):
            await self.recount(payload.channel_id, payload.message_id)

    async def _vote_changed(self, place_id: int, guild_id: Optional[int], message_id: int,
                            emoji: Union[str, discord.PartialEmoji]):
        """Ha cambiado una reacción: si es la que vota, se recuentan los votos de ese mensaje."""
        known = place_id in self.spots
        parent_id = self._parent_of(place_id, guild_id)
        if parent_id is None or emoji_key(emoji) != emoji_key(self.channels[parent_id].emoji):
            if not known:
                self.spots.pop(place_id, None)  # Aún no hay que apuntar la publicación: no ha votado nadie
            return
        if not known:
            self._spawn(self.sync_place(place_id))  # Publicación que no conocía: se repasa entera
            return
        await self.recount(place_id, message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._vote_changed(payload.channel_id, payload.guild_id, payload.message_id, payload.emoji)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._vote_changed(payload.channel_id, payload.guild_id, payload.message_id, payload.emoji)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        if payload.channel_id in self.spots and await asyncio.to_thread(is_clip, payload.message_id):
            await self.recount(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent):
        await self._vote_changed(payload.channel_id, payload.guild_id, payload.message_id, payload.emoji)

    async def _deleted(self, place_id: int, message_ids: List[int]):
        spot = self.spots.get(place_id)
        if spot is None:
            return
        if spot.top_message_id in message_ids:
            spot.top_message_id = None  # Alguien ha borrado el top: vuelve a salir abajo
            await asyncio.to_thread(save_spot, place_id, spot)
            self.schedule_repost(place_id)
        if await asyncio.to_thread(forget_clips, message_ids):
            self.schedule_refresh(place_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self._deleted(payload.channel_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        await self._deleted(payload.channel_id, list(payload.message_ids))

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        """Si se borra una publicación con top, se olvidan ella y sus clips."""
        self.threads.pop(payload.thread_id, None)
        if self.spots.pop(payload.thread_id, None) is not None:
            self._cancel(payload.thread_id)
            await asyncio.to_thread(save_spot, payload.thread_id, None)

    def _cancel(self, place_id: int):
        for tasks in (self.repost_tasks, self.refresh_tasks):
            task = tasks.pop(place_id, None)
            if task and not task.done():
                task.cancel()

    # ---------- /clips (admins) ----------

    def _list_card(self, guild: discord.Guild) -> tarjetas.Card:
        lines = [f"{'💬' if config.forum else '🎬'} <#{channel_id}> · se vota con {config.emoji}"
                 + (" · un top en cada publicación" if config.forum else "")
                 for channel_id, config in self.channels.items() if config.guild_id == guild.id]
        card = tarjetas.Card(tarjetas.INFO)
        card.text("### 🎬 Canales de clips\n" + ("\n".join(lines) or "Ningún canal todavía."))
        card.note(f"El último mensaje es siempre el top de clips más votados (en un foro, el de cada "
                  f"publicación). Añade uno con {tarjetas.cmd('clips')}.")
        return card

    @app_commands.command(name="clips", description="Canales con el top de clips más votados (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de texto o foro de clips (vacío = ver los que hay)",
                           emoji=f"Reacción que vota (por defecto {DEFAULT_EMOJI}; vale un emoji del servidor)",
                           quitar="Que ese canal deje de tener top de clips")
    async def clips(self, interaction: discord.Interaction,
                    canal: Optional[Union[discord.TextChannel, discord.ForumChannel]] = None,
                    emoji: Optional[str] = None, quitar: bool = False):
        guild = interaction.guild
        if canal is None:
            return await interaction.response.send_message(view=self._list_card(guild), ephemeral=True,
                                                           allowed_mentions=NO_MENTIONS)
        if quitar:
            self.channels.pop(canal.id, None)
            places = [place_id for place_id, spot in self.spots.items() if spot.parent_id == canal.id]
            await interaction.response.send_message(f"✅ {canal.mention} ya no tiene top de clips.", ephemeral=True)
            for place_id in places:
                spot = self.spots.pop(place_id)
                self._cancel(place_id)
                place = guild.get_channel_or_thread(place_id) or self.threads.pop(place_id, None)
                if spot.top_message_id and isinstance(place, (discord.TextChannel, discord.Thread)):
                    await self._delete(place, spot.top_message_id)
            await asyncio.to_thread(save_channel, canal.id, None)
            return

        chosen = (emoji or "").strip() or (self.channels[canal.id].emoji if canal.id in self.channels
                                            else DEFAULT_EMOJI)
        parsed = discord.PartialEmoji.from_str(chosen)
        if parsed.id is None and (len(chosen) > 8 or re.search(r"[A-Za-z<>:]", chosen) or chosen.isdigit()):
            return await interaction.response.send_message(
                "❌ Eso no parece un emoji. Escribe solo el emoji (por ejemplo ✅ o uno del servidor).", ephemeral=True
            )
        forum = isinstance(canal, discord.ForumChannel)
        perms = canal.permissions_for(guild.me)
        can_send = perms.send_messages_in_threads if forum else perms.send_messages
        if not (perms.view_channel and can_send and perms.read_message_history):
            where = "Enviar mensajes en hilos y publicaciones" if forum else "Enviar mensajes"
            return await interaction.response.send_message(
                f"❌ En {canal.mention} necesito **Ver canal**, **{where}** y **Leer el historial**.", ephemeral=True
            )
        config = ClipChannel(guild.id, str(parsed), forum)
        self.channels[canal.id] = config
        await asyncio.to_thread(save_channel, canal.id, config)
        where = (f"cada publicación de {canal.mention} tendrá su propio top como último mensaje "
                 f"(también las nuevas)" if forum else "el top será siempre el último mensaje")
        await interaction.response.send_message(
            f"✅ {canal.mention} tiene ahora top de clips: se vota reaccionando con {config.emoji} a cualquier "
            f"clip (un vídeo o un enlace a un vídeo; los GIF no), y {where}. Estoy contando los clips que ya hay.",
            ephemeral=True, allowed_mentions=NO_MENTIONS
        )
        await self.sync_channel(canal.id, everything=True)
