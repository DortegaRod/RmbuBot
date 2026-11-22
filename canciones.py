"""
Top de canciones: /top-canciones enseña las canciones que más han sonado con el bot en el servidor, 10 por
página; con usuario:@alguien, las que más ha pedido esa persona (con sus cuentas vinculadas).

- Cuenta una reproducción cada vez que una canción empieza a sonar, aunque la salten. Las repeticiones de un
  bucle no cuentan (ni el reintento cuando una canción se corta nada más empezar). Las radios no entran.
- Las canciones se agrupan por su enlace (los de YouTube, por el vídeo, aunque el enlace se escriba distinto);
  se enseña el título con el que sonó la última vez.
- Lo de antes de existir este top se lee una vez del canal de comandos (/setup): cada canción que sonó dejó
  ahí su tarjeta «🎶 Sonó» (desde el 22/10/2026; antes, la tarjeta se borraba). Cada reproducción guarda el
  mensaje de su tarjeta, para no contarla dos veces.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.utils import escape_markdown

import cuentas
import db
import tarjetas
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)

PAGE_SIZE = 10          # Canciones por página
TITLE_LENGTH = 100      # Largo máximo del título en el top
IMPORT_BATCH = 500      # Al leer el historial, se guarda lo encontrado cada tantos mensajes
COLOUR = tarjetas.MUSIC
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")
PLAYED_HEAD = "### 🎶 Sonó"  # Así empieza la tarjeta que deja en el canal cada canción que ha sonado
TITLE_RE = re.compile(r"\*\*\[(?P<title>.+)\]\((?P<url>https?://[^\s)]+)\)\*\*")
REQUESTER_RE = re.compile(r"Pedida por <@!?(\d+)>")
UNESCAPE_RE = re.compile(r"\\(.)")  # escape_markdown pone una \ delante de * _ ~ | > ...


@dataclass
class Play:
    message_id: Optional[int]   # La tarjeta de la canción en el canal (None si no se pudo publicar)
    url: str
    title: str
    requester_id: Optional[int]
    played_at: float


@dataclass
class TopSong:
    url: str
    title: str
    plays: int


# ==========================================
# BASE DE DATOS
# ==========================================

def init_db():
    with db.get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS canciones_sonadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            message_id INTEGER UNIQUE,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            requester_id INTEGER,
            played_at REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS canciones_por_servidor ON canciones_sonadas (guild_id, url)")
        # Servidores cuyo canal ya se leyó (las tarjetas «Sonó» de antes de existir el top)
        conn.execute("CREATE TABLE IF NOT EXISTS canciones_importadas (guild_id INTEGER PRIMARY KEY)")


def save_plays(guild_id: int, plays: Iterable[Play]) -> int:
    """Guarda reproducciones; las de una tarjeta ya guardada se saltan. Devuelve cuántas eran nuevas."""
    with db.get_db_connection() as conn:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO canciones_sonadas (guild_id, message_id, url, title, requester_id, "
                         "played_at) VALUES (?, ?, ?, ?, ?, ?)",
                         [(guild_id, p.message_id, p.url, p.title, p.requester_id, p.played_at) for p in plays])
        return conn.total_changes - before


def is_imported(guild_id: int) -> bool:
    with db.get_db_connection() as conn:
        return conn.execute("SELECT 1 FROM canciones_importadas WHERE guild_id = ?", (guild_id,)).fetchone() is not None


def mark_imported(guild_id: int):
    with db.get_db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO canciones_importadas (guild_id) VALUES (?)", (guild_id,))


def _who(requesters: Optional[List[int]]) -> Tuple[str, tuple]:
    if not requesters:
        return "", ()
    return f" AND requester_id IN ({','.join('?' * len(requesters))})", tuple(requesters)


def count_songs(guild_id: int, requesters: Optional[List[int]] = None) -> Tuple[int, int]:
    """(canciones distintas, reproducciones) de un servidor, o solo de lo que pidieron esas cuentas."""
    where, args = _who(requesters)
    with db.get_db_connection() as conn:
        songs, plays = conn.execute(f"SELECT COUNT(DISTINCT url), COUNT(*) FROM canciones_sonadas "
                                    f"WHERE guild_id = ?{where}", (guild_id, *args)).fetchone()
    return songs, plays


def top_songs(guild_id: int, page: int, requesters: Optional[List[int]] = None,
              per_page: int = PAGE_SIZE) -> List[TopSong]:
    """
    Una página del top: de más a menos veces; a igualdad, la que sonó más recientemente. El título es el de la
    última vez que sonó (en SQLite, una columna junto a MAX() sale de la fila del máximo).
    """
    where, args = _who(requesters)
    with db.get_db_connection() as conn:
        rows = conn.execute(f"SELECT url, title, COUNT(*), MAX(played_at) FROM canciones_sonadas "
                            f"WHERE guild_id = ?{where} GROUP BY url ORDER BY COUNT(*) DESC, MAX(played_at) DESC "
                            f"LIMIT ? OFFSET ?", (guild_id, *args, per_page, (page - 1) * per_page)).fetchall()
    return [TopSong(url, title, plays) for url, title, plays, _ in rows]


# ==========================================
# QUÉ CANCIÓN ES
# ==========================================

def song_key(url: str) -> str:
    """El enlace con el que se agrupa una canción: los de YouTube, por el vídeo; los demás, sin lo de tras «?»."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    video = None
    if host in YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            video = (parse_qs(parsed.query).get("v") or [None])[0]
        elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
            video = parsed.path.split("/")[2]
    elif host == "youtu.be":
        video = parsed.path.lstrip("/").split("/")[0]
    if video:
        return f"https://www.youtube.com/watch?v={video}"
    return f"{parsed.scheme}://{host}{parsed.path}" if host else url.strip()


def _texts(components) -> Iterable[str]:
    """Los textos de los componentes de un mensaje (las tarjetas los llevan dentro de contenedores y secciones)."""
    for component in components or ():
        content = getattr(component, "content", None)
        if isinstance(content, str):
            yield content
        yield from _texts(getattr(component, "children", None))


def play_from_card(message: discord.Message) -> Optional[Play]:
    """La reproducción que registra una tarjeta «🎶 Sonó» del bot; None si el mensaje no es una."""
    for text in _texts(message.components):
        if not text.startswith(PLAYED_HEAD):
            continue
        match = TITLE_RE.search(text)
        if match is None:
            return None
        requester = REQUESTER_RE.search(text)
        return Play(message.id, song_key(match["url"]), UNESCAPE_RE.sub(r"\1", match["title"]),
                    int(requester[1]) if requester else None, message.created_at.timestamp())
    return None


async def record_play(player, song):
    """
    Gancho del motor de música: apunta la canción que acaba de empezar. Cada canción cuenta una vez: ni sus
    repeticiones en bucle ni el reintento si se corta nada más empezar.
    """
    if song.is_radio or song.counted:
        return
    song.counted = True
    message = player.now_playing_message
    play = Play(message.id if message is not None else None, song_key(song.webpage_url), song.title,
                song.requester.id if song.requester else None, discord.utils.utcnow().timestamp())
    await asyncio.to_thread(save_plays, player.guild.id, [play])


# ==========================================
# LA TARJETA DEL TOP
# ==========================================

def top_card(owner_id: int, who_id: int, who_name: Optional[str], songs: List[TopSong], page: int, pages: int,
             total_songs: int, total_plays: int, reading: bool = False) -> tarjetas.Card:
    card = tarjetas.Card(COLOUR)
    card.text(f"## 🎶 Lo más pedido por {escape_markdown(who_name)}" if who_name else "## 🎶 Top canciones")
    for position, song in enumerate(songs, (page - 1) * PAGE_SIZE + 1):
        medal = MEDALS.get(position, f"`#{position}`")
        times = "vez" if song.plays == 1 else "veces"
        button = ui.Button(style=discord.ButtonStyle.link, label="Escuchar", emoji="🎧", url=song.url)
        card.box.add_item(ui.Section(ui.TextDisplay(f"{medal} **{escape_markdown(song.title[:TITLE_LENGTH])}**\n"
                                                    f"-# 🔁 {song.plays} {times}"), accessory=button))
    if not songs:
        card.text("Aún no ha sonado ninguna canción." if not who_name else "Aún no ha pedido ninguna canción.")
    else:
        card.note(f"{total_plays} reproducciones · {total_songs} canciones distintas")
    if reading:
        card.note("⏳ Aún estoy leyendo del canal las canciones que ya habían sonado: vuelve a mirar en un rato.")
    if pages > 1:
        card.buttons(TopSongsButton(owner_id, who_id, page - 1, emoji="◀️", disabled=page <= 1),
                     TopSongsButton(owner_id, who_id, page, label=f"{page} / {pages}", disabled=True),
                     TopSongsButton(owner_id, who_id, page + 1, emoji="▶️", disabled=page >= pages))
    return card


async def build_top(guild: discord.Guild, owner_id: int, who_id: int, page: int,
                    reading: bool = False) -> tarjetas.Card:
    """Una página del top del servidor (who_id = 0) o de lo que pidió una persona."""
    requesters = await asyncio.to_thread(cuentas.group_of, guild.id, who_id) if who_id else None
    total_songs, total_plays = await asyncio.to_thread(count_songs, guild.id, requesters)
    pages = max(1, -(-total_songs // PAGE_SIZE))
    page = min(max(1, page), pages)
    songs = await asyncio.to_thread(top_songs, guild.id, page, requesters)
    who_name = None
    if who_id:
        member = guild.get_member(who_id)
        who_name = member.display_name if member else "alguien que ya no está"
    return top_card(owner_id, who_id, who_name, songs, page, pages, total_songs, total_plays, reading)


class TopSongsButton(ui.DynamicItem[ui.Button],
                     template=r"reimbou:canc:(?P<owner>\d+):(?P<who>\d+):(?P<page>\d+)"):
    """
    Flechas del top. Quien lo pidió mueve su mensaje; si pulsa otra persona, le abre su propia copia (que solo
    ve ella) en esa página, como en /ranking.
    """

    def __init__(self, owner_id: int, who_id: int, page: int, *, label: Optional[str] = None, emoji=None,
                 disabled: bool = False):
        super().__init__(ui.Button(label=label, emoji=emoji, disabled=disabled, style=discord.ButtonStyle.secondary,
                                   custom_id=f"reimbou:canc:{owner_id}:{who_id}:{page}"))
        self.owner_id, self.who_id, self.page = owner_id, who_id, page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(int(match["owner"]), int(match["who"]), int(match["page"]), label=item.label, emoji=item.emoji,
                   disabled=item.disabled)

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._turn_page)

    async def _turn_page(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("CancionesCog")
        reading = cog is not None and interaction.guild_id in cog.reading
        card = await build_top(interaction.guild, interaction.user.id, self.who_id, self.page, reading)
        if interaction.user.id == self.owner_id:
            await tarjetas.update(interaction, card)
        else:
            await interaction.response.send_message(view=card, ephemeral=True, allowed_mentions=NO_MENTIONS)


# ==========================================
# COG
# ==========================================

class CancionesCog(commands.Cog):
    """/top-canciones y la lectura, una vez, de las canciones que ya habían sonado."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.task: Optional[asyncio.Task] = None
        self.reading: set = set()  # Servidores cuyo canal se está leyendo ahora

    async def cog_load(self):
        init_db()
        self.bot.add_dynamic_items(TopSongsButton)
        self.task = asyncio.create_task(self._import_all())

    async def cog_unload(self):
        if self.task and not self.task.done():
            self.task.cancel()

    async def _import_all(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.import_history(guild)
            except Exception:
                logger.exception(f"🎶 Canciones: no pude leer las que ya habían sonado en {guild.name}")

    async def import_history(self, guild: discord.Guild):
        """Lee del canal de comandos las tarjetas «🎶 Sonó» de antes de existir el top (una sola vez)."""
        if await asyncio.to_thread(is_imported, guild.id):
            return
        channel_id = db.get_music_channel(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            # Sin canal de comandos no se sabe dónde están (se vuelve a mirar al arrancar)
            logger.info(f"🎶 Canciones: {guild.name} no tiene canal de comandos (/setup): no leo las de antes")
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            logger.warning(f"🎶 Canciones: no puedo leer el historial de #{channel.name}")
            return
        logger.info(f"🎶 Canciones: leyendo las tarjetas «Sonó» de #{channel.name}...")
        self.reading.add(guild.id)
        try:
            found = new = read = 0
            plays: List[Play] = []
            async for message in channel.history(limit=None):
                read += 1
                if message.author.id == self.bot.user.id:
                    play = play_from_card(message)
                    if play is not None:
                        plays.append(play)
                if len(plays) >= IMPORT_BATCH or (plays and read % IMPORT_BATCH == 0):
                    found, new = found + len(plays), new + await asyncio.to_thread(save_plays, guild.id, plays)
                    plays = []  # Se guarda por tandas: el top se va llenando mientras se lee
            found, new = found + len(plays), new + await asyncio.to_thread(save_plays, guild.id, plays)
        finally:
            self.reading.discard(guild.id)
        if found:
            await asyncio.to_thread(mark_imported, guild.id)
        logger.info(f"🎶 Canciones: leídos {read} mensajes de #{channel.name}: {found} tarjetas «Sonó» ({new} nuevas)"
                    + ("" if found else "; ninguna, así que se vuelve a mirar al arrancar"))

    @app_commands.command(name="top-canciones", description="Las canciones que más han sonado con el bot")
    @app_commands.guild_only()
    @app_commands.describe(usuario="Solo las que ha pedido esta persona", pagina="Página del top (10 por página)")
    async def top_canciones(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None,
                            pagina: app_commands.Range[int, 1, 1000] = 1):
        card = await build_top(interaction.guild, interaction.user.id, usuario.id if usuario else 0, pagina,
                               interaction.guild_id in self.reading)
        await interaction.response.send_message(view=card, allowed_mentions=NO_MENTIONS)
