"""
Playlists del servidor: cada persona crea las suyas, añade y quita canciones, y las pone en la cola.

- Al crearla se elige si es solo de quien la crea o colaborativa (cualquiera añade y quita canciones).
  Cambiarle el nombre, hacerla colaborativa o borrarla, solo quien la creó. Escucharla, todo el mundo.
- Van por persona: desde cualquier cuenta vinculada (cuentas.py) se gestionan las mismas playlists.
- Hasta 10 playlists por persona y 100 canciones en cada una (la cola también es de 100).
- Se añaden canciones con /playlist añadir (una búsqueda, un enlace o una playlist de YouTube entera)
  o con el botón ➕ de la tarjeta «Sonando ahora».
- Al reproducirla, sus canciones van al final de la cola, en orden o aleatorias.
"""

import logging
import math
import random
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, Union

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.utils import escape_markdown

import controls
import cuentas
import db
import tarjetas
from music import Song, SearchError, search_youtube

logger = logging.getLogger(__name__)

MAX_SONGS = 100                 # Canciones por playlist (como la cola)
MAX_PLAYLISTS_PER_PERSON = 10
MAX_NAME_LENGTH = 40
PAGE_SIZE = 10
NO_MENTIONS = discord.AllowedMentions.none()


# ==========================================
# BASE DE DATOS
# ==========================================

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS playlists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        colaborativa INTEGER NOT NULL DEFAULT 0,
        creada TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS playlist_canciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id INTEGER NOT NULL,
        titulo TEXT NOT NULL,
        url TEXT NOT NULL,
        duracion REAL,
        miniatura TEXT NOT NULL DEFAULT '',
        added_by INTEGER NOT NULL,
        added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS playlist_canciones_orden ON playlist_canciones (playlist_id, id)",
)


def init_db():
    with db.get_db_connection() as conn:
        for sql in _SCHEMA:
            conn.execute(sql)


@dataclass
class Playlist:
    id: int
    guild_id: int
    owner_id: int
    nombre: str
    colaborativa: bool
    canciones: int = 0
    duracion: float = 0.0


@dataclass
class Track:
    """Una canción guardada en una playlist."""
    id: int
    titulo: str
    url: str
    duracion: Optional[float]
    miniatura: str
    added_by: int

    def to_song(self) -> Song:
        # El audio se saca al reproducirla: las URLs de audio de YouTube caducan
        return Song(title=self.titulo, webpage_url=self.url, thumbnail=self.miniatura, duration=self.duracion)


_SELECT_PLAYLISTS = (
    "SELECT p.id, p.guild_id, p.owner_id, p.nombre, p.colaborativa, COUNT(c.id), COALESCE(SUM(c.duracion), 0) "
    "FROM playlists p LEFT JOIN playlist_canciones c ON c.playlist_id = p.id "
)
_SELECT_TRACKS = "SELECT id, titulo, url, duracion, miniatura, added_by FROM playlist_canciones "


def _to_playlist(row) -> Playlist:
    return Playlist(row[0], row[1], row[2], row[3], bool(row[4]), row[5], row[6])


def get_playlist(playlist_id: int) -> Optional[Playlist]:
    with db.get_db_connection() as conn:
        row = conn.execute(_SELECT_PLAYLISTS + "WHERE p.id = ? GROUP BY p.id", (playlist_id,)).fetchone()
    return _to_playlist(row) if row else None


def guild_playlists(guild_id: int) -> List[Playlist]:
    with db.get_db_connection() as conn:
        rows = conn.execute(_SELECT_PLAYLISTS + "WHERE p.guild_id = ? GROUP BY p.id "
                                                "ORDER BY p.nombre COLLATE NOCASE, p.id", (guild_id,)).fetchall()
    return [_to_playlist(row) for row in rows]


def create_playlist(guild_id: int, owner_id: int, nombre: str, colaborativa: bool) -> int:
    with db.get_db_connection() as conn:
        cursor = conn.execute("INSERT INTO playlists (guild_id, owner_id, nombre, colaborativa) VALUES (?, ?, ?, ?)",
                              (guild_id, owner_id, nombre, int(colaborativa)))
        return cursor.lastrowid


def update_playlist(playlist_id: int, *, nombre: Optional[str] = None, colaborativa: Optional[bool] = None):
    with db.get_db_connection() as conn:
        if nombre is not None:
            conn.execute("UPDATE playlists SET nombre = ? WHERE id = ?", (nombre, playlist_id))
        if colaborativa is not None:
            conn.execute("UPDATE playlists SET colaborativa = ? WHERE id = ?", (int(colaborativa), playlist_id))


def delete_playlist(playlist_id: int):
    with db.get_db_connection() as conn:
        conn.execute("DELETE FROM playlist_canciones WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


def playlist_tracks(playlist_id: int) -> List[Track]:
    with db.get_db_connection() as conn:
        rows = conn.execute(_SELECT_TRACKS + "WHERE playlist_id = ? ORDER BY id", (playlist_id,)).fetchall()
    return [Track(*row) for row in rows]


def add_tracks(playlist_id: int, songs: List[Song], added_by: int) -> Tuple[int, int, int]:
    """Añade canciones sin repetir ni pasar del límite. Devuelve (añadidas, ya estaban, no cupieron)."""
    added = repeated = left_out = 0
    with db.get_db_connection() as conn:
        urls = {row[0] for row in conn.execute("SELECT url FROM playlist_canciones WHERE playlist_id = ?",
                                               (playlist_id,))}
        room = MAX_SONGS - len(urls)
        for song in songs:
            if song.webpage_url in urls:
                repeated += 1
            elif added >= room:
                left_out += 1
            else:
                conn.execute("INSERT INTO playlist_canciones (playlist_id, titulo, url, duracion, miniatura, added_by) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (playlist_id, song.title, song.webpage_url, song.duration, song.thumbnail or "", added_by))
                urls.add(song.webpage_url)
                added += 1
    return added, repeated, left_out


def remove_track(playlist_id: int, track_id: int) -> Optional[Track]:
    with db.get_db_connection() as conn:
        row = conn.execute(_SELECT_TRACKS + "WHERE id = ? AND playlist_id = ?", (track_id, playlist_id)).fetchone()
        if row:
            conn.execute("DELETE FROM playlist_canciones WHERE id = ?", (track_id,))
    return Track(*row) if row else None


# ==========================================
# PERMISOS (por persona: las cuentas vinculadas cuentan como una)
# ==========================================

Check = Callable[[int, int, Playlist], bool]


def is_owner(guild_id: int, user_id: int, playlist: Playlist) -> bool:
    return cuentas.principal_of(guild_id, user_id) == cuentas.principal_of(guild_id, playlist.owner_id)


def can_edit(guild_id: int, user_id: int, playlist: Playlist) -> bool:
    return playlist.colaborativa or is_owner(guild_id, user_id, playlist)


def anyone(guild_id: int, user_id: int, playlist: Playlist) -> bool:
    return True


def person_playlists(guild_id: int, user_id: int) -> List[Playlist]:
    person = cuentas.principal_of(guild_id, user_id)
    return [p for p in guild_playlists(guild_id) if cuentas.principal_of(guild_id, p.owner_id) == person]


def _name(playlist: Playlist) -> str:
    return escape_markdown(playlist.nombre)


def _count(n: int) -> str:
    return f"{n} {'canción' if n == 1 else 'canciones'}"


def _edit_denied(playlist: Playlist) -> str:
    return f"🔒 «{_name(playlist)}» no es colaborativa: solo quien la creó puede cambiar sus canciones."


def _owner_denied(playlist: Playlist) -> str:
    return f"🔒 Solo quien creó «{_name(playlist)}» puede hacer eso."


def clean_name(nombre: str) -> str:
    """Sin espacios de sobra ni # delante (el # marca las elecciones de la lista que sale al escribir)."""
    return " ".join(nombre.split()).lstrip("#").strip()[:MAX_NAME_LENGTH]


def _owner_name(guild: Optional[discord.Guild], playlist: Playlist) -> str:
    member = guild.get_member(playlist.owner_id) if guild else None
    return member.display_name if member else "alguien que ya no está"


def playlist_choices(interaction: discord.Interaction, current: str, check: Check) -> List[app_commands.Choice[str]]:
    """Opciones de la lista que sale al escribir el nombre: primero las tuyas."""
    guild_id, user_id = interaction.guild_id, interaction.user.id
    text = (current or "").casefold().strip()
    found = [p for p in guild_playlists(guild_id) if check(guild_id, user_id, p) and text in p.nombre.casefold()]
    found.sort(key=lambda p: (not is_owner(guild_id, user_id, p), p.nombre.casefold()))
    choices = []
    for p in found[:25]:
        label = f"{p.nombre} · de {_owner_name(interaction.guild, p)} · {_count(p.canciones)}"
        if p.colaborativa:
            label += " · colaborativa"
        choices.append(app_commands.Choice(name=label[:100], value=f"#{p.id}"))
    return choices


def resolve(interaction: discord.Interaction, value: str, check: Check = anyone,
            denied: Optional[Callable[[Playlist], str]] = None) -> Union[Playlist, str]:
    """La playlist elegida (de la lista o escrita a mano), o el motivo por el que no se puede usar."""
    guild_id, user_id = interaction.guild_id, interaction.user.id
    value = (value or "").strip()
    playlist = None
    if value.startswith("#") and value[1:].isdigit():
        playlist = get_playlist(int(value[1:]))
        if playlist is not None and playlist.guild_id != guild_id:
            playlist = None
    elif value:
        matches = [p for p in guild_playlists(guild_id) if p.nombre.casefold() == value.casefold()]
        mine = [p for p in matches if is_owner(guild_id, user_id, p)]
        if len(mine) == 1:
            playlist = mine[0]
        elif len(matches) == 1:
            playlist = matches[0]
        elif matches:
            return "❌ Hay varias playlists con ese nombre: elígela de la lista que sale al escribir."
    if playlist is None:
        return "❌ No encuentro esa playlist: elígela de la lista que sale al escribir."
    if not check(guild_id, user_id, playlist):
        return denied(playlist) if denied else "🔒 No puedes usar esa playlist."
    return playlist


def pick_track(tracks: List[Track], value: str) -> Optional[Track]:
    """La canción elegida: de la lista que sale al escribir, por su número o por su título exacto."""
    value = (value or "").strip()
    if value.startswith("#") and value[1:].isdigit():
        return next((t for t in tracks if t.id == int(value[1:])), None)
    if value.isdigit():
        index = int(value) - 1
        return tracks[index] if 0 <= index < len(tracks) else None
    matches = [t for t in tracks if t.titulo.casefold() == value.casefold()]
    return matches[0] if len(matches) == 1 else None


# ==========================================
# TARJETAS
# ==========================================

def _length(seconds: Optional[float]) -> str:
    return f" · `{controls.fmt_time(seconds)}`" if seconds else ""


def playlist_card(guild: discord.Guild, playlist: Playlist, viewer_id: int, page: Union[int, str]) -> tarjetas.Card:
    """Una playlist: sus canciones por páginas, botones para ponerla y (si puedes) un menú para quitar canciones."""
    tracks = playlist_tracks(playlist.id)
    pages = max(1, math.ceil(len(tracks) / PAGE_SIZE))
    page = tarjetas.clamp_page(page, pages)
    mode = "🤝 colaborativa" if playlist.colaborativa else "🔒 solo la cambia quien la creó"
    summary = f"**{len(tracks)}** {'canción' if len(tracks) == 1 else 'canciones'}"
    total = sum(track.duracion or 0 for track in tracks)
    if total:
        summary += f" · ⏱️ {controls.fmt_time(total)}"

    card = tarjetas.Card(tarjetas.MUSIC)
    card.section(f"## 🎶 {_name(playlist)}\nDe <@{playlist.owner_id}> · {mode}\n{summary}",
                 tracks[0].miniatura if tracks else None)
    card.separator()
    if not tracks:
        card.text(f"Aún no tiene canciones. Añádelas con {tarjetas.cmd('playlist añadir')} o con ➕ en la tarjeta "
                  f"de la canción que suena.")
        return card

    start = (page - 1) * PAGE_SIZE
    shown = tracks[start:start + PAGE_SIZE]
    card.text("\n".join(f"`{n}.` {escape_markdown(track.titulo[:90])}{_length(track.duracion)}"
                        for n, track in enumerate(shown, start + 1)))
    if pages > 1:
        card.note(f"Página {page} de {pages}")
    card.buttons(*tarjetas.page_controls("playlist", viewer_id, page, pages, arg=playlist.id))
    card.buttons(PlaylistPlayButton(playlist.id, "orden"), PlaylistPlayButton(playlist.id, "azar"))
    if can_edit(guild.id, viewer_id, playlist):
        options = [discord.SelectOption(label=f"{n}. {track.titulo}"[:100], value=str(track.id))
                   for n, track in enumerate(shown, start + 1)]
        card.buttons(RemoveMenu(playlist.id, viewer_id, page, options))
    return card


def lists_card(guild: discord.Guild, viewer_id: int, page: Union[int, str],
               user_id: Optional[int] = None) -> Union[tarjetas.Card, str]:
    """Las playlists del servidor (o las de una persona), por páginas."""
    playlists = guild_playlists(guild.id)
    if user_id:
        person = cuentas.principal_of(guild.id, user_id)
        playlists = [p for p in playlists if cuentas.principal_of(guild.id, p.owner_id) == person]
    if not playlists:
        if user_id:
            return f"📭 <@{user_id}> aún no tiene playlists."
        return f"📭 Aún no hay playlists. Crea la primera con {tarjetas.cmd('playlist crear')}."
    pages = max(1, math.ceil(len(playlists) / PAGE_SIZE))
    page = tarjetas.clamp_page(page, pages)
    start = (page - 1) * PAGE_SIZE
    lines = []
    for p in playlists[start:start + PAGE_SIZE]:
        line = f"🎶 **{_name(p)}** · de <@{p.owner_id}> · {_count(p.canciones)}"
        if p.colaborativa:
            line += " · 🤝"
        lines.append(line)

    card = tarjetas.Card(tarjetas.MUSIC)
    card.text(f"## 🎶 Playlists de <@{user_id}>" if user_id else "## 🎶 Playlists del servidor")
    card.text("\n".join(lines))
    card.separator()
    footer = f"{len(playlists)} en total · ábrelas con {tarjetas.cmd('playlist ver')}"
    card.note(f"Página {page} de {pages} · {footer}" if pages > 1 else footer)
    card.buttons(*tarjetas.page_controls("playlists", viewer_id, page, pages, arg=user_id or None))
    return card


async def _render_playlist(interaction: discord.Interaction, owner_id: int, page: Union[int, str],
                           arg: Optional[int] = None):
    playlist = get_playlist(arg) if arg else None
    if playlist is None or playlist.guild_id != interaction.guild_id:
        return "⌛ Esa playlist ya no existe."
    return playlist_card(interaction.guild, playlist, owner_id, page)


async def _render_lists(interaction: discord.Interaction, owner_id: int, page: Union[int, str],
                        arg: Optional[int] = None):
    return lists_card(interaction.guild, owner_id, page, arg)


tarjetas.register_pages("playlist", _render_playlist)
tarjetas.register_pages("playlists", _render_lists)


# ==========================================
# REPRODUCIR Y GUARDAR LA QUE SUENA
# ==========================================

async def play_playlist(interaction: discord.Interaction, playlist: Playlist, shuffle: bool):
    """Pone la playlist al final de la cola de quien la pide."""
    if await controls.deny_join(interaction):
        return
    songs = [track.to_song() for track in playlist_tracks(playlist.id)]
    if not songs:
        return await interaction.response.send_message(f"📭 «{_name(playlist)}» aún no tiene canciones.",
                                                       ephemeral=True)
    if shuffle:
        random.shuffle(songs)
    await interaction.response.defer()
    notes = [f"Playlist de <@{playlist.owner_id}>"] + (["🔀 En orden aleatorio"] if shuffle else [])
    await controls.enqueue_songs(interaction, songs, title=f"🎶 «{_name(playlist)}» a la cola", notes=notes)


class SaveMenu(tarjetas.Card):
    """Menú (solo para quien pulsó ➕) para guardar la canción que suena en una de sus playlists."""

    def __init__(self, guild: discord.Guild, user_id: int, song: Song, playlists: List[Playlist]):
        super().__init__(tarjetas.MUSIC, timeout=300)
        self.song = song
        self.text(f"### ➕ Guardar en una playlist\n**{controls.song_label(song, link=False)}**")
        options = []
        for p in playlists:
            whose = "tuya" if is_owner(guild.id, user_id, p) else f"de {_owner_name(guild, p)}"
            options.append(discord.SelectOption(label=p.nombre[:100], value=str(p.id),
                                                description=f"{whose} · {_count(p.canciones)}"[:100]))
        self.select = ui.Select(placeholder="Elige la playlist", options=options)
        self.select.callback = self.save
        self.buttons(self.select)

    async def save(self, interaction: discord.Interaction):
        self.stop()
        playlist = get_playlist(int(self.select.values[0]))
        if playlist is None:
            return await tarjetas.update(interaction, tarjetas.simple(tarjetas.DANGER, "⌛ Esa playlist ya no existe."))
        if not can_edit(interaction.guild_id, interaction.user.id, playlist):
            return await tarjetas.update(interaction, tarjetas.simple(tarjetas.DANGER, _edit_denied(playlist)))
        added, repeated, _ = add_tracks(playlist.id, [self.song], interaction.user.id)
        if added:
            result = tarjetas.simple(tarjetas.SUCCESS, f"✅ Guardada en «{_name(playlist)}».")
        elif repeated:
            result = tarjetas.simple(tarjetas.INFO, f"ℹ️ Ya estaba en «{_name(playlist)}».")
        else:
            result = tarjetas.simple(tarjetas.WARNING, f"📭 «{_name(playlist)}» ya tiene {MAX_SONGS} canciones.")
        await tarjetas.update(interaction, result)


async def open_save_menu(interaction: discord.Interaction, song: Song):
    """Lo que hace el botón ➕ de la tarjeta «Sonando ahora» (lo registra el cog en controls.py)."""
    guild_id, user_id = interaction.guild_id, interaction.user.id
    editable = [p for p in guild_playlists(guild_id) if can_edit(guild_id, user_id, p)]
    editable.sort(key=lambda p: (not is_owner(guild_id, user_id, p), p.nombre.casefold()))
    if not editable:
        return await interaction.response.send_message(
            f"➕ Aún no tienes playlists: crea una con {tarjetas.cmd('playlist crear')} y vuelve a pulsar ➕.",
            ephemeral=True
        )
    await interaction.response.send_message(view=SaveMenu(interaction.guild, user_id, song, editable[:25]),
                                            ephemeral=True)


class PlaylistPlayButton(ui.DynamicItem[ui.Button], template=r"reimbou:plp:(?P<id>\d+):(?P<mode>orden|azar)"):
    """Botones «Reproducir» y «Aleatoria» de la tarjeta de una playlist (persistentes)."""

    def __init__(self, playlist_id: int, mode: str):
        shuffle = mode == "azar"
        super().__init__(ui.Button(label="Aleatoria" if shuffle else "Reproducir", emoji="🔀" if shuffle else "▶️",
                                   style=discord.ButtonStyle.secondary if shuffle else discord.ButtonStyle.success,
                                   custom_id=f"reimbou:plp:{playlist_id}:{mode}"))
        self.playlist_id, self.shuffle = playlist_id, shuffle

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(int(match["id"]), match["mode"])

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._play)

    async def _play(self, interaction: discord.Interaction):
        playlist = get_playlist(self.playlist_id)
        if playlist is None or playlist.guild_id != interaction.guild_id:
            return await interaction.response.send_message("⌛ Esa playlist ya no existe.", ephemeral=True)
        await play_playlist(interaction, playlist, self.shuffle)


class RemoveMenu(ui.DynamicItem[ui.Select], template=r"reimbou:plq:(?P<id>\d+):(?P<owner>\d+):(?P<page>\d+)"):
    """Menú «Quitar una canción» de la tarjeta de una playlist (persistente)."""

    def __init__(self, playlist_id: int, owner_id: int, page: int, options: List[discord.SelectOption]):
        super().__init__(ui.Select(custom_id=f"reimbou:plq:{playlist_id}:{owner_id}:{page}",
                                   placeholder="🗑️ Quitar una canción de esta página…", options=options))
        self.playlist_id, self.owner_id, self.page = playlist_id, owner_id, page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Select, match):
        return cls(int(match["id"]), int(match["owner"]), int(match["page"]), item.options)

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._remove)

    async def _remove(self, interaction: discord.Interaction):
        playlist = get_playlist(self.playlist_id)
        if playlist is None or playlist.guild_id != interaction.guild_id:
            return await interaction.response.send_message("⌛ Esa playlist ya no existe.", ephemeral=True)
        if not can_edit(interaction.guild_id, interaction.user.id, playlist):
            return await interaction.response.send_message(_edit_denied(playlist), ephemeral=True)
        remove_track(playlist.id, int(self.item.values[0]))
        # La tarjeta se queda en la misma página, ya sin esa canción
        await tarjetas.update(interaction, playlist_card(interaction.guild, get_playlist(playlist.id),
                                                         self.owner_id, self.page))


class DeleteConfirm(tarjetas.Card):
    """Pregunta antes de borrar una playlist (solo la ve quien la va a borrar)."""

    def __init__(self, playlist: Playlist):
        super().__init__(tarjetas.DANGER, timeout=120)
        self.playlist = playlist
        self.text(f"### 🗑️ ¿Borrar «{_name(playlist)}»?\nTiene {_count(playlist.canciones)}. No se puede deshacer.")
        delete = ui.Button(label="Borrar", emoji="🗑️", style=discord.ButtonStyle.danger)
        cancel = ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        delete.callback, cancel.callback = self.delete, self.cancel
        self.buttons(delete, cancel)

    async def delete(self, interaction: discord.Interaction):
        self.stop()
        delete_playlist(self.playlist.id)
        await tarjetas.update(interaction, tarjetas.simple(tarjetas.DANGER,
                                                           f"🗑️ Playlist «{_name(self.playlist)}» borrada."))

    async def cancel(self, interaction: discord.Interaction):
        self.stop()
        await tarjetas.update(interaction, tarjetas.simple(discord.Colour.dark_grey(),
                                                           "Cancelado: la playlist sigue ahí."))


def _added_text(playlist: Playlist, songs: List[Song], added: int, repeated: int, left_out: int) -> str:
    name = _name(playlist)
    if len(songs) == 1:
        title = controls.song_label(songs[0], link=False)
        if added:
            return f"✅ Añadida a «{name}»: **{title}**."
        if repeated:
            return f"ℹ️ **{title}** ya estaba en «{name}»."
        return f"📭 «{name}» ya tiene {MAX_SONGS} canciones, el máximo."
    parts = [f"✅ **{added}** {'canción añadida' if added == 1 else 'canciones añadidas'} a «{name}»."]
    if repeated:
        parts.append(f"{repeated} ya {'estaba' if repeated == 1 else 'estaban'}.")
    if left_out:
        parts.append(f"{left_out} no {'cupo' if left_out == 1 else 'cupieron'} (máximo {MAX_SONGS}).")
    return " ".join(parts)


# ==========================================
# COMANDOS
# ==========================================

@app_commands.guild_only()
class PlaylistsCog(commands.GroupCog, group_name="playlist",
                   group_description="Playlists del servidor: crea las tuyas, añade canciones y ponlas"):
    """Comandos /playlist crear, añadir, quitar, ver, reproducir, lista, editar y borrar."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        init_db()
        self.bot.add_dynamic_items(PlaylistPlayButton, RemoveMenu)
        controls.on_save_song = open_save_menu

    async def cog_unload(self):
        controls.on_save_song = None

    @app_commands.command(name="crear", description="Crea una playlist (solo tuya o colaborativa)")
    @app_commands.describe(nombre="Nombre de la playlist",
                           colaborativa="Si cualquiera puede añadir y quitar canciones (por defecto, solo tú)")
    async def crear(self, interaction: discord.Interaction, nombre: app_commands.Range[str, 1, MAX_NAME_LENGTH],
                    colaborativa: bool = False):
        name = clean_name(nombre)
        if not name:
            return await interaction.response.send_message("❌ Ponle un nombre a la playlist.", ephemeral=True)
        mine = person_playlists(interaction.guild_id, interaction.user.id)
        if len(mine) >= MAX_PLAYLISTS_PER_PERSON:
            return await interaction.response.send_message(
                f"❌ Ya tienes {MAX_PLAYLISTS_PER_PERSON} playlists, el máximo. Borra alguna con "
                f"{tarjetas.cmd('playlist borrar')}.", ephemeral=True
            )
        if any(p.nombre.casefold() == name.casefold() for p in mine):
            return await interaction.response.send_message(
                f"❌ Ya tienes una playlist que se llama «{escape_markdown(name)}».", ephemeral=True
            )
        create_playlist(interaction.guild_id, interaction.user.id, name, colaborativa)
        kind = "colaborativa: cualquiera puede añadir y quitar canciones" if colaborativa else "solo la cambias tú"
        await interaction.response.send_message(
            f"✅ Playlist **«{escape_markdown(name)}»** creada ({kind}).\n"
            f"-# Añade canciones con {tarjetas.cmd('playlist añadir')} o con ➕ en la tarjeta de la canción que suena.",
            ephemeral=True
        )

    @app_commands.command(name="añadir", description="Añade a una playlist una canción, o una playlist de YouTube entera")
    @app_commands.describe(playlist="La playlist",
                           cancion="Nombre de la canción, o enlace de YouTube (vale una playlist entera)")
    async def anadir(self, interaction: discord.Interaction, playlist: str, cancion: str):
        found = resolve(interaction, playlist, can_edit, _edit_denied)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        if found.canciones >= MAX_SONGS:
            return await interaction.response.send_message(
                f"📭 «{_name(found)}» ya tiene {MAX_SONGS} canciones, el máximo. Quita alguna con "
                f"{tarjetas.cmd('playlist quitar')}.", ephemeral=True
            )
        if "spotify.com" in cancion or cancion.strip().startswith("spotify:"):
            return await interaction.response.send_message(
                "❌ Spotify no deja sacar su música: escribe el nombre de la canción y la busco en YouTube.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            songs = await search_youtube(cancion, limit=MAX_SONGS)
        except SearchError as e:
            return await interaction.followup.send(f"❌ No pude abrir eso: {e}.", ephemeral=True)
        songs = [song for song in songs if not song.is_radio]
        if not songs:
            return await interaction.followup.send("❌ No encontré resultados.", ephemeral=True)
        added, repeated, left_out = add_tracks(found.id, songs, interaction.user.id)
        await interaction.followup.send(_added_text(found, songs, added, repeated, left_out), ephemeral=True)

    @app_commands.command(name="quitar", description="Quita una canción de una playlist")
    @app_commands.describe(playlist="La playlist", cancion="La canción que quieres quitar (o su número)")
    async def quitar(self, interaction: discord.Interaction, playlist: str, cancion: str):
        found = resolve(interaction, playlist, can_edit, _edit_denied)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        track = pick_track(playlist_tracks(found.id), cancion)
        if track is None:
            return await interaction.response.send_message(
                f"❌ No encuentro esa canción en «{_name(found)}»: elígela de la lista que sale al escribir.",
                ephemeral=True
            )
        remove_track(found.id, track.id)
        await interaction.response.send_message(
            f"🗑️ Quitada de «{_name(found)}»: **{escape_markdown(track.titulo[:100])}**.", ephemeral=True
        )

    @app_commands.command(name="ver", description="Muestra las canciones de una playlist, con botones para ponerla")
    @app_commands.describe(playlist="La playlist")
    async def ver(self, interaction: discord.Interaction, playlist: str):
        found = resolve(interaction, playlist)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        await interaction.response.send_message(view=playlist_card(interaction.guild, found, interaction.user.id, 1),
                                                allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="reproducir", description="Pone una playlist al final de la cola")
    @app_commands.describe(playlist="La playlist", aleatorio="Mezclar sus canciones (por defecto, en orden)")
    async def reproducir(self, interaction: discord.Interaction, playlist: str, aleatorio: bool = False):
        found = resolve(interaction, playlist)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        await play_playlist(interaction, found, aleatorio)

    @app_commands.command(name="lista", description="Las playlists del servidor, o las de alguien")
    @app_commands.describe(usuario="De quién (por defecto, todas)")
    async def lista(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        result = lists_card(interaction.guild, interaction.user.id, 1, usuario.id if usuario else None)
        if isinstance(result, str):
            return await interaction.response.send_message(result, ephemeral=True, allowed_mentions=NO_MENTIONS)
        await interaction.response.send_message(view=result, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="editar", description="Cambia el nombre de una playlist tuya o si es colaborativa")
    @app_commands.describe(playlist="Tu playlist", nombre="Nombre nuevo",
                           colaborativa="Si cualquiera puede añadir y quitar canciones")
    async def editar(self, interaction: discord.Interaction, playlist: str,
                     nombre: Optional[app_commands.Range[str, 1, MAX_NAME_LENGTH]] = None,
                     colaborativa: Optional[bool] = None):
        found = resolve(interaction, playlist, is_owner, _owner_denied)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        if nombre is None and colaborativa is None:
            return await interaction.response.send_message(
                "❌ Indica un nombre nuevo, si es colaborativa, o las dos cosas.", ephemeral=True
            )
        name = None
        if nombre is not None:
            name = clean_name(nombre)
            if not name:
                return await interaction.response.send_message("❌ Ponle un nombre a la playlist.", ephemeral=True)
            if any(p.id != found.id and p.nombre.casefold() == name.casefold()
                   for p in person_playlists(interaction.guild_id, interaction.user.id)):
                return await interaction.response.send_message(
                    f"❌ Ya tienes otra playlist que se llama «{escape_markdown(name)}».", ephemeral=True
                )
        update_playlist(found.id, nombre=name, colaborativa=colaborativa)
        changes = []
        if name is not None and name != found.nombre:
            changes.append(f"ahora se llama **«{escape_markdown(name)}»**")
        if colaborativa is not None:
            changes.append("es colaborativa: cualquiera puede añadir y quitar canciones" if colaborativa
                           else "ya solo la cambias tú")
        text = f"✅ «{_name(found)}» {' y '.join(changes)}." if changes else "✅ No había nada que cambiar."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="borrar", description="Borra una playlist tuya")
    @app_commands.describe(playlist="Tu playlist")
    async def borrar(self, interaction: discord.Interaction, playlist: str):
        found = resolve(interaction, playlist, is_owner, _owner_denied)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        await interaction.response.send_message(view=DeleteConfirm(found), ephemeral=True)

    # ---------- Listas que salen al escribir ----------

    @anadir.autocomplete("playlist")
    async def _anadir_playlists(self, interaction: discord.Interaction, current: str):
        return playlist_choices(interaction, current, can_edit)

    @quitar.autocomplete("playlist")
    async def _quitar_playlists(self, interaction: discord.Interaction, current: str):
        return playlist_choices(interaction, current, can_edit)

    @quitar.autocomplete("cancion")
    async def _quitar_canciones(self, interaction: discord.Interaction, current: str):
        found = resolve(interaction, getattr(interaction.namespace, "playlist", None) or "", can_edit)
        if isinstance(found, str):
            return []
        text = (current or "").casefold().strip()
        choices = []
        for n, track in enumerate(playlist_tracks(found.id), 1):
            label = f"{n}. {track.titulo}"
            if text and text not in label.casefold():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=f"#{track.id}"))
            if len(choices) == 25:
                break
        return choices

    @ver.autocomplete("playlist")
    async def _ver_playlists(self, interaction: discord.Interaction, current: str):
        return playlist_choices(interaction, current, anyone)

    @reproducir.autocomplete("playlist")
    async def _reproducir_playlists(self, interaction: discord.Interaction, current: str):
        return playlist_choices(interaction, current, anyone)

    @editar.autocomplete("playlist")
    async def _editar_playlists(self, interaction: discord.Interaction, current: str):
        return playlist_choices(interaction, current, is_owner)

    @borrar.autocomplete("playlist")
    async def _borrar_playlists(self, interaction: discord.Interaction, current: str):
        return playlist_choices(interaction, current, is_owner)
