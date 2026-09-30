"""
Controles de música compartidos por los comandos de barra y los botones:
permisos ("controlar solo desde la llamada"), acciones, embeds y la tarjeta "Sonando ahora".
"""

import logging
from typing import Optional

import discord
from discord.utils import escape_markdown

from music import music_manager, disconnect_player, MusicPlayer, Song, LOOP_LABELS

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()


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


# ==========================================
# EMBEDS
# ==========================================

def _base_embed(player: MusicPlayer, song: Song) -> discord.Embed:
    voice_client = player.guild.voice_client
    if voice_client is not None and voice_client.is_paused():
        title = "⏸️ En pausa"
    else:
        title = "📻 Sonando ahora" if song.is_radio else "🎶 Sonando ahora"
    color = discord.Color.gold() if song.is_radio else discord.Color.green()
    embed = discord.Embed(title=title, description=f"**{song_label(song)}**", color=color)
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    return embed


def _footer(player: MusicPlayer, song: Song) -> str:
    parts = [f"Bucle: {LOOP_LABELS[player.loop_mode]}", f"En cola: {len(player.queue)}"]
    if song.requester:
        parts.insert(0, f"Pedido por {song.requester.display_name}")
    return " • ".join(parts)


def build_card(player: MusicPlayer, song: Song) -> discord.Embed:
    """Tarjeta "Sonando ahora" que se publica al empezar cada canción (va con los botones)."""
    embed = _base_embed(player, song)
    if song.is_radio or not song.duration:
        embed.add_field(name="​", value="🔴 **EN DIRECTO**", inline=False)
    else:
        embed.add_field(name="Duración", value=f"`{fmt_time(song.duration)}`")
    embed.set_footer(text=_footer(player, song))
    return embed


def build_current_embed(player: MusicPlayer, song: Song, live_title: Optional[str] = None) -> discord.Embed:
    """Embed de /current: barra de progreso en canciones; en radios, la canción que emiten."""
    embed = _base_embed(player, song)
    if song.is_radio or not song.duration:
        value = "🔴 **EN DIRECTO**"
        if live_title:
            value += f"\n🎵 En antena: **{escape_markdown(live_title)}**"
        embed.add_field(name="​", value=value, inline=False)
    else:
        voice_client = player.guild.voice_client
        paused = voice_client is not None and voice_client.is_paused()
        elapsed = min(player.get_elapsed(), song.duration)
        icon = "⏸️" if paused else "▶️"
        embed.add_field(
            name="​",
            value=f"{icon} `{fmt_time(elapsed)}` {progress_bar(elapsed, song.duration)} `{fmt_time(song.duration)}`",
            inline=False
        )
    embed.set_footer(text=f"{_footer(player, song)} • Volumen: {int(player.volume * 100)}%")
    return embed


# ==========================================
# TARJETA "SONANDO AHORA" (ganchos del motor)
# ==========================================

async def announce_now_playing(player: MusicPlayer, song: Song):
    """Publica la tarjeta con botones cuando empieza una canción."""
    if player.text_channel is None:
        return
    if song is player.announced_song and player.now_playing_message is not None:
        return  # La misma canción repitiéndose en bucle: su tarjeta ya está publicada
    await delete_now_playing(player)
    try:
        player.now_playing_message = await player.text_channel.send(
            embed=build_card(player, song), view=NowPlayingView()
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


async def delete_now_playing(player: MusicPlayer):
    """Borra la tarjeta anterior (al cambiar de canción, al acabar la cola o al desconectar)."""
    message = player.now_playing_message
    player.now_playing_message = None
    player.announced_song = None
    if message is not None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # Ya la había borrado alguien


async def refresh_card(guild: discord.Guild):
    """Actualiza la tarjeta tras un cambio de estado (pausa, bucle...)."""
    player = music_manager.peek(guild.id)
    if player is None or player.now_playing_message is None or player.current is None:
        return
    try:
        await player.now_playing_message.edit(embed=build_card(player, player.current))
    except discord.HTTPException:
        pass


class NowPlayingView(discord.ui.View):
    """
    Botones de la tarjeta "Sonando ahora". Es persistente (custom_id fijos): si el bot se
    reinicia, los botones de tarjetas antiguas responden con un aviso en vez de fallar.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = music_manager.peek(interaction.guild_id)
        card = player.now_playing_message if player else None
        if card is None or interaction.message is None or interaction.message.id != card.id:
            await interaction.response.send_message("⌛ Estos botones ya no están activos.", ephemeral=True)
            return False
        error = control_error(interaction.user)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error("Error en un botón de música", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Algo ha fallado con ese botón.", ephemeral=True)

    @discord.ui.button(emoji="⏯️", label="Pausa", style=discord.ButtonStyle.secondary, custom_id="reimbou:np:pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (pause(interaction.guild) or resume(interaction.guild)):
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        player = music_manager.peek(interaction.guild_id)
        await interaction.response.edit_message(embed=build_card(player, player.current))

    @discord.ui.button(emoji="⏭️", label="Saltar", style=discord.ButtonStyle.primary, custom_id="reimbou:np:skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        skipped = request_skip(interaction.guild)
        if skipped is None:
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        await interaction.response.send_message(
            f"⏭️ {interaction.user.mention} saltó **{song_label(skipped, link=False)}**.", allowed_mentions=NO_MENTIONS
        )

    @discord.ui.button(emoji="⏹️", label="Parar", style=discord.ButtonStyle.danger, custom_id="reimbou:np:stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"⏹️ {interaction.user.mention} paró la música.", allowed_mentions=NO_MENTIONS
        )
        await disconnect_player(interaction.guild)

    @discord.ui.button(emoji="🔁", label="Bucle", style=discord.ButtonStyle.secondary, custom_id="reimbou:np:loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = music_manager.peek(interaction.guild_id)
        player.loop_mode = (player.loop_mode + 1) % len(LOOP_LABELS)
        if player.current is None:  # La canción terminó justo al pulsar
            return await interaction.response.send_message(
                f"Bucle: **{LOOP_LABELS[player.loop_mode]}**", ephemeral=True
            )
        await interaction.response.edit_message(embed=build_card(player, player.current))
