"""
Controles de música compartidos por los comandos de barra y los botones:
permisos ("controlar solo desde la llamada"), conexión de voz, poner canciones en la cola
(/play, /radio y las playlists) y las tarjetas de música ("Sonando ahora" con botones,
/current, /queue por páginas y lo que se añade a la cola).
"""

import asyncio
import logging
import math
import time
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Union

import discord
from discord.utils import escape_markdown

import db
import tarjetas
from config import MAX_QUEUE_SIZE
from music import (music_manager, disconnect_player, play_next, MusicPlayer, Song, LOOP_LABELS,
                   LOOP_OFF, LOOP_CURRENT)

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
QUEUE_PAGE_SIZE = 10

# Última acción sobre la música de cada servidor («⏸️ Pausada por @x»): la tarjeta la enseña
_last_action: Dict[int, str] = {}

# Lo registra playlists.py: abre el menú para guardar en una playlist la canción que suena (botón ➕)
on_save_song: Optional[Callable[[discord.Interaction, Song], Awaitable[None]]] = None


# ==========================================
# FORMATO
# ==========================================

def fmt_time(seconds: float) -> str:
    """Formatea segundos como M:SS o H:MM:SS."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(elapsed: float, total: float, length: int = 18) -> str:
    """Devuelve una barra tipo ─────🔘──────── según el progreso."""
    if not total or total <= 0:
        return ""
    frac = max(0.0, min(1.0, elapsed / total))
    pos = int(frac * (length - 1))
    return "─" * pos + "🔘" + "─" * (length - 1 - pos)


def song_label(song: Song, link: bool = True) -> str:
    """Título listo para Discord: escapado (los * o _ del título rompían el formato) y con enlace."""
    title = escape_markdown(song.title[:100])
    if song.is_radio:
        return f"📻 {title}"  # Sin enlace: apuntaría al stream de audio en bruto
    return f"[{title}]({song.webpage_url})" if link else title


def _length(song: Song) -> str:
    return "🔴 En directo" if song.is_radio or not song.duration else f"`{fmt_time(song.duration)}`"


# ==========================================
# PERMISOS: CONTROLAR SOLO DESDE LA LLAMADA
# ==========================================

def is_dj(member: discord.Member) -> bool:
    """Los moderadores (Gestionar servidor o Mover miembros) pueden controlar la música desde cualquier sitio."""
    perms = member.guild_permissions
    return perms.manage_guild or perms.move_members


def control_error(member: discord.Member) -> Optional[str]:
    """Motivo por el que `member` no puede controlar la música, o None si puede."""
    voice_client = member.guild.voice_client
    if voice_client is None or not voice_client.is_connected() or is_dj(member):
        return None
    if member.voice is None or member.voice.channel != voice_client.channel:
        return f"🎧 Tienes que estar en {voice_client.channel.mention} para controlar la música."
    return None


def join_error(member: discord.Member) -> Optional[str]:
    """Impide llevarse al bot a otro canal mientras está poniendo música para otras personas."""
    voice_client = member.guild.voice_client
    if voice_client is None or not voice_client.is_connected() or is_dj(member):
        return None
    if member.voice is not None and member.voice.channel == voice_client.channel:
        return None
    player = music_manager.peek(member.guild.id)
    busy = player is not None and (player.current is not None or len(player.queue) > 0)
    listeners = [m for m in voice_client.channel.members if not m.bot]
    if busy and listeners:
        return f"🎧 Estoy poniendo música en {voice_client.channel.mention} para otras personas. Únete a ese canal."
    return None


# ==========================================
# COMPROBACIONES Y CONEXIÓN DE VOZ (comandos y botones)
# ==========================================

async def deny_wrong_channel(interaction: discord.Interaction) -> bool:
    """
    Comprueba si la interacción ocurre en el canal de comandos configurado con /setup.
    Si NO está permitido, responde con un aviso efímero y devuelve True (denegar).
    Si el servidor no ha hecho /setup, se permite en cualquier canal.
    """
    configured = db.get_music_channel(interaction.guild_id)
    if configured is not None and interaction.channel_id != configured:
        await interaction.response.send_message(
            f"❌ Usa los comandos del bot en <#{configured}>.", ephemeral=True
        )
        return True
    return False


async def deny_control(interaction: discord.Interaction) -> bool:
    """Comprobaciones de los comandos que controlan la música: canal de /setup y estar en la llamada."""
    if await deny_wrong_channel(interaction):
        return True
    error = control_error(interaction.user)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return True
    return False


async def deny_join(interaction: discord.Interaction) -> bool:
    """Comprobaciones previas de /play y /radio."""
    if await deny_wrong_channel(interaction):
        return True
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)
        return True
    error = join_error(interaction.user)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return True
    player = music_manager.peek(interaction.guild_id)
    if player is not None and player.free_slots() == 0:
        await interaction.response.send_message(
            f"📭 La cola está llena ({MAX_QUEUE_SIZE} canciones). Usa `/clear` o espera a que avance.", ephemeral=True
        )
        return True
    return False


async def connect_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """Conecta (o mueve) al bot al canal de voz del usuario. Si falla, lo explica y devuelve None."""
    voice = interaction.user.voice
    if voice is None or voice.channel is None:
        await interaction.followup.send("❌ Ya no estás en un canal de voz.")
        return None
    channel = voice.channel
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.connect and perms.speak):
        await interaction.followup.send(f"❌ No tengo permiso para conectarme y hablar en {channel.mention}.")
        return None

    vc = interaction.guild.voice_client
    try:
        if vc is not None and not vc.is_connected():
            await vc.disconnect(force=True)  # Conexión anterior colgada
            vc = None
        # Lógica de conexión y movimiento de canal
        if vc is None:
            vc = await channel.connect(self_deaf=True, timeout=20)
        elif vc.channel != channel:
            await vc.move_to(channel)
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ No pude conectarme al canal de voz (tiempo agotado).")
        return None
    except Exception as e:
        logger.exception("Error al conectar al canal de voz")
        await interaction.followup.send(f"❌ Error de conexión: {e}")
        return None
    return vc


# ==========================================
# ACCIONES
# ==========================================

def request_skip(guild: discord.Guild) -> Optional[Song]:
    """Salta la canción actual (aunque el bucle sea de canción). Devuelve la saltada, o None."""
    voice_client = guild.voice_client
    player = music_manager.peek(guild.id)
    if voice_client is None or player is None or not (voice_client.is_playing() or voice_client.is_paused()):
        return None
    skipped = player.current
    player.skip_requested = True
    voice_client.stop()  # Al detener el reproductor, se dispara automáticamente el evento 'after'
    return skipped


def pause(guild: discord.Guild) -> bool:
    voice_client = guild.voice_client
    player = music_manager.peek(guild.id)
    if voice_client is None or player is None or not voice_client.is_playing():
        return False
    voice_client.pause()
    player.mark_paused()
    return True


def resume(guild: discord.Guild) -> bool:
    voice_client = guild.voice_client
    player = music_manager.peek(guild.id)
    if voice_client is None or player is None or not voice_client.is_paused():
        return False
    voice_client.resume()
    player.mark_resumed()
    return True


def note_action(guild: discord.Guild, text: str):
    """Apunta qué se ha hecho y quién: la tarjeta «Sonando ahora» lo enseña en vez de llenar el canal."""
    _last_action[guild.id] = text


def loop_action(member: discord.abc.User, mode: int) -> str:
    if mode == LOOP_OFF:
        return f"➡️ Bucle quitado por {member.mention}"
    if mode == LOOP_CURRENT:
        return f"🔂 Bucle de canción puesto por {member.mention}"
    return f"🔁 Bucle de cola puesto por {member.mention}"


# Avisos públicos (los usan tanto los comandos como los botones)
def skip_text(member: discord.abc.User, song: Song) -> str:
    return f"⏭️ {member.mention} ha saltado **{song_label(song, link=False)}**."


def stop_text(member: discord.abc.User) -> str:
    return f"⏹️ {member.mention} ha parado la música."


# ==========================================
# TARJETAS
# ==========================================

def _paused(player: MusicPlayer) -> bool:
    voice_client = player.guild.voice_client
    return voice_client is not None and voice_client.is_paused()


def _headline(player: MusicPlayer, song: Song) -> str:
    if _paused(player):
        return "⏸️ En pausa"
    return "📻 Sonando ahora" if song.is_radio else "🎶 Sonando ahora"


def _title_line(song: Song) -> str:
    # En las radios, el 📻 ya va en el título de la tarjeta
    return f"**{escape_markdown(song.title[:100]) if song.is_radio else song_label(song)}**"


def _requested_by(song: Song) -> str:
    if song.requester is None:
        return ""
    return f"{'Puesta' if song.is_radio else 'Pedida'} por {song.requester.mention}"


def _status(player: MusicPlayer, song: Optional[Song] = None) -> str:
    """Bucle, cola y volumen. La canción que va a empezar (aún en la cola) no cuenta como «en cola»."""
    waiting = sum(1 for queued in player.queue if queued is not song)
    return f"Bucle: {LOOP_LABELS[player.loop_mode]} · {waiting} en cola · 🔊 {round(player.volume * 100)}%"


def build_card(player: MusicPlayer, song: Song) -> tarjetas.Card:
    """Tarjeta «Sonando ahora» con los botones; se publica al empezar cada canción."""
    card = tarjetas.Card(tarjetas.RADIO if song.is_radio else tarjetas.MUSIC)
    details = " · ".join(filter(None, [_length(song), _requested_by(song)]))
    card.section(f"### {_headline(player, song)}\n{_title_line(song)}\n{details}", song.thumbnail, song.title[:200])
    card.separator()
    card.note("\n".join(filter(None, [_status(player, song), _last_action.get(player.guild.id)])))
    paused = _paused(player)
    buttons = [
        NowButton("pause", emoji="▶️" if paused else "⏸️", label="Seguir" if paused else "Pausa",
                  style=discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary),
        NowButton("skip", emoji="⏭️", label="Saltar", style=discord.ButtonStyle.primary),
        NowButton("stop", emoji="⏹️", label="Parar", style=discord.ButtonStyle.danger),
        NowButton("loop", emoji="🔁", label="Bucle"),
    ]
    if not song.is_radio:
        buttons.append(NowButton("save", emoji="➕", label="Guardar"))  # En una playlist
    card.buttons(*buttons)
    return card


def build_log_card(song: Song) -> tarjetas.Card:
    """Cómo se queda la tarjeta de una canción cuando ya ha sonado: como registro, en gris y sin botones."""
    card = tarjetas.Card(tarjetas.PLAYED)
    details = " · ".join(filter(None, [_length(song), _requested_by(song)]))
    head = "📻 Sonó" if song.is_radio else "🎶 Sonó"
    card.section(f"### {head}\n{_title_line(song)}\n{details}", song.thumbnail, song.title[:200])
    return card


def build_current_card(player: MusicPlayer, song: Song, live_title: Optional[str] = None) -> tarjetas.Card:
    """/current: barra de progreso en canciones; en radios, la canción que están emitiendo."""
    card = tarjetas.Card(tarjetas.RADIO if song.is_radio else tarjetas.MUSIC)
    lines = [f"### {_headline(player, song)}", _title_line(song), _requested_by(song)]
    card.section("\n".join(filter(None, lines)), song.thumbnail, song.title[:200])
    if song.is_radio or not song.duration:
        live = "🔴 **En directo**"
        if live_title:
            live += f"\n🎵 En antena: **{escape_markdown(live_title)}**"
        card.text(live)
    else:
        elapsed = min(player.get_elapsed(), song.duration)
        icon = "⏸️" if _paused(player) else "▶️"
        card.text(f"{icon} `{fmt_time(elapsed)}` {progress_bar(elapsed, song.duration)} `{fmt_time(song.duration)}`")
    card.separator()
    card.note(_status(player, song))
    return card


def _seconds_until(player: MusicPlayer, position: int) -> Optional[float]:
    """Segundos que faltan para que suene la canción de la posición `position` de la cola, si se puede saber."""
    current = player.current
    if (current is None or current.is_radio or not current.duration or _paused(player)
            or player.loop_mode == LOOP_CURRENT):
        return None
    wait = max(0.0, current.duration - player.get_elapsed())
    for song in list(player.queue)[:position - 1]:
        if song.is_radio or not song.duration:
            return None
        wait += song.duration
    return wait


def queued_card(player: MusicPlayer, song: Song, position: int) -> tarjetas.Card:
    """Respuesta a /play o /radio cuando lo pedido aún no suena."""
    card = tarjetas.Card(tarjetas.QUEUE)
    details = _length(song)
    wait = _seconds_until(player, position)
    if wait is not None:
        details += f" · sonará <t:{int(time.time() + wait)}:R>"  # Discord lo muestra como «dentro de 5 minutos»
    card.section(f"### 📝 En cola · posición {position}\n{_title_line(song)}\n{details}", song.thumbnail, song.title[:200])
    requested = _requested_by(song)
    if requested:
        card.note(requested)
    return card


def playlist_card(added: List[Song], left_out: int, requester: discord.abc.User,
                  title: str = "📂 Playlist añadida", notes: Sequence[str] = ()) -> tarjetas.Card:
    """Respuesta cuando se ponen varias canciones a la vez (una playlist de YouTube o una del servidor)."""
    card = tarjetas.Card(tarjetas.QUEUE)
    text = f"### {title}\n**{len(added)}** {'canción' if len(added) == 1 else 'canciones'}"
    total = sum(song.duration or 0 for song in added if not song.is_radio)
    if total:
        text += f" · `{fmt_time(total)}`"
    card.section(text, added[0].thumbnail if added else None)
    lines = [*notes, f"Pedida por {requester.mention}"]
    if left_out:
        lines.insert(0, f"La cola está llena: {left_out} no {'cupo' if left_out == 1 else 'cupieron'}.")
    card.note("\n".join(lines))
    return card


def build_queue_card(player: MusicPlayer, owner_id: int, page: Union[int, str]) -> tarjetas.Card:
    """/queue: lo que suena y la cola, 10 canciones por página."""
    songs = list(player.queue)
    pages = max(1, math.ceil(len(songs) / QUEUE_PAGE_SIZE))
    page = tarjetas.clamp_page(page, pages)
    card = tarjetas.Card(tarjetas.QUEUE)
    card.text("## 🎵 Cola de reproducción")
    current = player.current
    if current is not None:
        if current.is_radio or not current.duration:
            progress = "🔴 En directo"
        else:
            progress = f"`{fmt_time(min(player.get_elapsed(), current.duration))} / {fmt_time(current.duration)}`"
        card.text(f"{'⏸️' if _paused(player) else '▶️'} **Sonando:** {song_label(current)} · {progress}")
    start = (page - 1) * QUEUE_PAGE_SIZE
    lines = [f"`{n}.` {song_label(song, link=False)} · {_length(song)}"
             for n, song in enumerate(songs[start:start + QUEUE_PAGE_SIZE], start + 1)]
    if lines:
        card.separator()
        card.text("\n".join(lines))
    card.separator()
    parts = [f"{len(songs)} en cola"]
    if pages > 1:
        parts.insert(0, f"Página {page} de {pages}")
    total = sum(song.duration or 0 for song in songs if not song.is_radio)
    if total:
        parts.append(f"⏱️ {fmt_time(total)}")
    parts.append(f"Bucle: {LOOP_LABELS[player.loop_mode]}")
    card.note(" · ".join(parts))
    card.buttons(*tarjetas.page_controls("cola", owner_id, page, pages))
    return card


async def render_queue_page(interaction: discord.Interaction, owner_id: int, page: Union[int, str],
                            arg: Optional[int] = None):
    player = music_manager.peek(interaction.guild_id)
    if player is None or (not player.queue and player.current is None):
        return "📭 La cola está vacía."
    return build_queue_card(player, owner_id, page)


tarjetas.register_pages("cola", render_queue_page)


# ==========================================
# TARJETA "SONANDO AHORA" (ganchos del motor)
# ==========================================

def adopt_card(player: MusicPlayer, song: Song, message: discord.abc.Snowflake,
               channel: Optional[discord.abc.Messageable]):
    """
    La respuesta a /play o /radio hace de tarjeta «Sonando ahora» de su canción, para que no salgan
    dos mensajes. Se guarda a través del canal, porque el enlace de la interacción caduca a los 15 minutos.
    """
    target = message
    permissions_for = getattr(channel, "permissions_for", None)
    if permissions_for is not None and permissions_for(channel.guild.me).view_channel:
        target = channel.get_partial_message(message.id)
    player.now_playing_message = target
    player.announced_song = song
    _last_action.pop(player.guild.id, None)


async def announce_now_playing(player: MusicPlayer, song: Song):
    """Publica la tarjeta con botones cuando empieza una canción."""
    if player.text_channel is None:
        return
    if song is player.announced_song and player.now_playing_message is not None:
        return  # Ya tiene tarjeta: la misma canción en bucle, o la respuesta a /play
    await retire_now_playing(player)
    try:
        player.now_playing_message = await player.text_channel.send(
            view=build_card(player, song), allowed_mentions=NO_MENTIONS
        )
        player.announced_song = song
    except discord.HTTPException as e:
        logger.warning(f"No pude publicar la tarjeta de 'Sonando ahora': {e}")


async def announce_error(player: MusicPlayer, song: Optional[Song], reason: str):
    """Avisa en el canal cuando una canción no se puede reproducir (antes se saltaba en silencio)."""
    if player.text_channel is None:
        return
    text = f"⚠️ No pude reproducir **{song_label(song, link=False)}**: {reason}." if song else f"⚠️ {reason}"
    try:
        await player.text_channel.send(text, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException as e:
        logger.warning(f"No pude avisar del error en el canal: {e}")


async def retire_now_playing(player: MusicPlayer):
    """
    Al cambiar de canción, acabarse la cola o desconectarse el bot, la tarjeta de la que sonaba
    se queda en el canal como registro: «Sonó», sin botones y en gris.
    """
    message, song = player.now_playing_message, player.announced_song
    player.now_playing_message = None
    player.announced_song = None
    _last_action.pop(player.guild.id, None)
    if message is not None and song is not None:
        try:
            await message.edit(view=build_log_card(song), allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            pass  # La habían borrado


async def refresh_card(guild: discord.Guild):
    """Actualiza la tarjeta tras un cambio de estado (pausa, bucle, volumen...)."""
    player = music_manager.peek(guild.id)
    if player is None or player.now_playing_message is None or player.current is None:
        return
    try:
        await player.now_playing_message.edit(view=build_card(player, player.current), allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        pass


# ==========================================
# PONER EN LA COLA (/play, /radio y playlists)
# ==========================================

async def enqueue_songs(interaction: discord.Interaction, songs: List[Song], *, title: Optional[str] = None,
                        notes: Sequence[str] = ()):
    """
    Pone canciones en la cola de quien las pide (conectándose a su canal de voz) y responde.
    La interacción tiene que estar ya diferida. Si es una sola canción y empieza a sonar ya, la
    respuesta es su tarjeta «Sonando ahora» (un mensaje, no dos). Con `title` (una playlist del
    servidor), la respuesta es siempre la tarjeta de playlist.
    """
    vc = await connect_voice(interaction)
    if vc is None:
        return
    player = music_manager.get_player(interaction.guild)
    player.text_channel = interaction.channel

    added = []
    for song in songs:
        song.requester = interaction.user
        if not player.add_song(song):
            break
        added.append(song)
    if not added:
        return await interaction.followup.send(f"📭 La cola está llena ({MAX_QUEUE_SIZE} canciones).")

    starts_now = player.current is None and not vc.is_playing() and not vc.is_paused()
    first_up = (title is None and starts_now and len(songs) == 1 and bool(player.queue)
                and player.queue[0] is added[0])
    if first_up:
        card = build_card(player, added[0])
    elif title is not None or len(songs) > 1:
        card = playlist_card(added, len(songs) - len(added), interaction.user, **({"title": title} if title else {}),
                             notes=notes)
    else:
        card = queued_card(player, added[0], len(player.queue))
    message = await interaction.followup.send(view=card, allowed_mentions=NO_MENTIONS, wait=True)
    if first_up:
        adopt_card(player, added[0], message, interaction.channel)

    # Arrancar el motor si estaba parado (después de responder, para que la respuesta salga antes que nada)
    if starts_now:
        await play_next(vc, player)
    else:
        await refresh_card(interaction.guild)  # La tarjeta dice cuántas hay en cola


class NowButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"reimbou:np:(?P<action>pause|skip|stop|loop|save)"):
    """
    Botones de la tarjeta "Sonando ahora". Son persistentes: si el bot se reinicia, los de
    tarjetas antiguas responden con un aviso en vez de fallar.
    """

    def __init__(self, action: str, *, emoji=None, label: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(emoji=emoji, label=label, style=style, custom_id=f"reimbou:np:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"], emoji=item.emoji, label=item.label, style=item.style)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = music_manager.peek(interaction.guild_id)
        card = player.now_playing_message if player else None
        if card is None or interaction.message is None or interaction.message.id != card.id:
            await interaction.response.send_message("⌛ Estos botones ya no están activos.", ephemeral=True)
            return False
        if self.action == "save":
            return True  # Guardar la canción en una playlist no cambia la música: vale desde fuera de la llamada
        error = control_error(interaction.user)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, getattr(self, f"_{self.action}"))

    async def _pause(self, interaction: discord.Interaction):
        guild = interaction.guild
        if pause(guild):
            note_action(guild, f"⏸️ Pausada por {interaction.user.mention}")
        elif resume(guild):
            note_action(guild, f"▶️ Reanudada por {interaction.user.mention}")
        else:
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        player = music_manager.peek(interaction.guild_id)
        await tarjetas.update(interaction, build_card(player, player.current))

    async def _skip(self, interaction: discord.Interaction):
        skipped = request_skip(interaction.guild)
        if skipped is None:
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        await interaction.response.send_message(skip_text(interaction.user, skipped), allowed_mentions=NO_MENTIONS)

    async def _stop(self, interaction: discord.Interaction):
        await interaction.response.send_message(stop_text(interaction.user), allowed_mentions=NO_MENTIONS)
        await disconnect_player(interaction.guild)

    async def _loop(self, interaction: discord.Interaction):
        player = music_manager.peek(interaction.guild_id)
        player.loop_mode = (player.loop_mode + 1) % len(LOOP_LABELS)
        note_action(interaction.guild, loop_action(interaction.user, player.loop_mode))
        if player.current is None:  # La canción terminó justo al pulsar
            return await interaction.response.send_message(
                f"Bucle: **{LOOP_LABELS[player.loop_mode]}**", ephemeral=True
            )
        await tarjetas.update(interaction, build_card(player, player.current))

    async def _save(self, interaction: discord.Interaction):
        player = music_manager.peek(interaction.guild_id)
        song = player.current if player else None
        if song is None:
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        if song.is_radio:
            return await interaction.response.send_message("📻 Las radios no se pueden guardar en una playlist.",
                                                           ephemeral=True)
        if on_save_song is None:
            return await interaction.response.send_message("❌ Las playlists no están disponibles.", ephemeral=True)
        await on_save_song(interaction, song)
