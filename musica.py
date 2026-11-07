"""
Comandos de música (/play, /radio, /queue, /skip...) y la salida automática del canal de voz.
El motor está en music.py; las tarjetas, botones y permisos compartidos, en controls.py.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import controls
import spotify
from config import MAX_QUEUE_SIZE
from controls import deny_control, deny_join
from music import (
    music_manager, search_youtube, disconnect_player, empty_channel_disconnect,
    log_dependency_status, SearchError, Song, LOOP_OFF, LOOP_CURRENT
)
from radios import RADIOS, RADIOS_BY_KEY, RADIO_ICON, fetch_now_playing
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)


class MusicaCog(commands.Cog):
    """Comandos de música y la salida automática cuando el canal de voz se queda vacío."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        log_dependency_status()
        # Ganchos del motor de música: tarjeta "Sonando ahora" y avisos en el canal de texto
        music_manager.on_track_start = controls.announce_now_playing
        music_manager.on_track_error = controls.announce_error
        music_manager.on_player_end = controls.retire_now_playing
        # Botones de la tarjeta "Sonando ahora": siguen respondiendo tras un reinicio
        self.bot.add_dynamic_items(controls.NowButton)

    # ---------- Canal de voz ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        """Monitoriza cambios de estado en canales de voz (conexiones, desconexiones)."""
        guild = member.guild

        # Limpieza si el bot es expulsado/desconectado manualmente (incluida su tarjeta de botones)
        if member.id == self.bot.user.id and after.channel is None:
            player = music_manager.remove_player(guild.id)
            if player:
                await music_manager.notify("on_player_end", player)
            return

        vc = guild.voice_client
        player = music_manager.peek(guild.id)
        if vc is None or vc.channel is None or player is None:
            return

        # Auto-salida: si el canal de voz del bot se queda sin humanos, programar desconexión.
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            if player.empty_task is None:
                player.empty_task = asyncio.create_task(empty_channel_disconnect(vc, player))
        elif player.empty_task is not None:
            # Volvió alguien: cancelar la salida programada
            player.empty_task.cancel()
            player.empty_task = None

    # ---------- Poner música ----------

    @app_commands.command(name="play", description="Reproduce música de YouTube o Spotify (búsqueda, enlace o playlist)")
    @app_commands.guild_only()
    @app_commands.describe(busqueda="Nombre de la canción, o enlace de YouTube o Spotify (canción, álbum o playlist)")
    async def play(self, interaction: discord.Interaction, busqueda: str):
        """
        Busca en YouTube y añade una canción o playlist a la cola del servidor. Los enlaces de Spotify
        se leen de Spotify y cada canción se busca en YouTube al ir a sonar (spotify.py).
        """
        if await deny_join(interaction):
            return

        await interaction.response.defer()
        search = spotify.fetch_songs if spotify.is_spotify(busqueda) else search_youtube
        existing = music_manager.peek(interaction.guild_id)
        try:
            songs = await search(busqueda, limit=existing.free_slots() if existing else MAX_QUEUE_SIZE)
        except SearchError as e:
            return await interaction.followup.send(f"❌ No pude abrir eso: {e}.")
        if not songs:
            return await interaction.followup.send("❌ No encontré resultados.")

        await controls.enqueue_songs(interaction, songs)

    @app_commands.command(name="radio", description="Sintoniza una radio española en directo")
    @app_commands.guild_only()
    @app_commands.describe(emisora="Emisora que quieres escuchar")
    @app_commands.choices(emisora=[app_commands.Choice(name=f"{r.emoji} {r.name}", value=r.key) for r in RADIOS])
    async def radio(self, interaction: discord.Interaction, emisora: app_commands.Choice[str]):
        """Se conecta directamente a un flujo (stream) HTTP de una emisora de radio."""
        if await deny_join(interaction):
            return

        await interaction.response.defer()
        station = RADIOS_BY_KEY[emisora.value]
        # Song con `stream_url` y marcada como radio: music.py se salta yt-dlp y conecta directamente a la emisora.
        radio_song = Song(
            title=station.name,
            webpage_url=station.url,
            thumbnail=RADIO_ICON,
            stream_url=station.url,
            is_radio=True
        )
        await controls.enqueue_songs(interaction, [radio_song])

    # ---------- Consultas ----------

    @app_commands.command(name="current",
                          description="Muestra lo que suena ahora (barra de progreso, o la canción de la radio)")
    @app_commands.guild_only()
    async def current(self, interaction: discord.Interaction):
        """Muestra la pista en reproducción con su progreso; en radios, el título que emiten."""
        player = music_manager.peek(interaction.guild_id)
        if player is None or player.current is None:
            return await interaction.response.send_message("🔇 No hay nada sonando ahora mismo.", ephemeral=True)

        song = player.current
        if not song.is_radio:
            return await interaction.response.send_message(view=controls.build_current_card(player, song),
                                                           allowed_mentions=NO_MENTIONS)

        # Radio: leer el título que está emitiendo (puede tardar un par de segundos)
        await interaction.response.defer()
        live_title = await fetch_now_playing(song.stream_url, station_name=song.title)
        await interaction.followup.send(view=controls.build_current_card(player, song, live_title),
                                        allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="queue", description="Muestra lo que suena y la cola, por páginas")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        player = music_manager.peek(interaction.guild_id)
        if player is None or (not player.queue and not player.current):
            return await interaction.response.send_message("📭 La cola está vacía.", ephemeral=True)
        await interaction.response.send_message(view=controls.build_queue_card(player, interaction.user.id, 1),
                                                allowed_mentions=NO_MENTIONS)

    # ---------- Controles ----------

    @app_commands.command(name="skip", description="Termina la pista actual y pasa a la siguiente")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if await deny_control(interaction):
            return
        skipped = controls.request_skip(interaction.guild)
        if skipped is None:
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        await interaction.response.send_message(controls.skip_text(interaction.user, skipped),
                                                allowed_mentions=NO_MENTIONS)

    # /pause, /resume, /loop y /volume contestan solo a quien los usa: la tarjeta «Sonando ahora»
    # ya enseña al canal el cambio y quién lo ha hecho.

    @app_commands.command(name="pause", description="Pausa la reproducción actual")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if await deny_control(interaction):
            return
        if not controls.pause(interaction.guild):
            return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        controls.note_action(interaction.guild, f"⏸️ Pausada por {interaction.user.mention}")
        await interaction.response.send_message("⏸️ Pausada. Sigue con /resume o con el botón de la tarjeta.",
                                                ephemeral=True)
        await controls.refresh_card(interaction.guild)

    @app_commands.command(name="resume", description="Reanuda la reproducción pausada")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if await deny_control(interaction):
            return
        if not controls.resume(interaction.guild):
            return await interaction.response.send_message("❌ No hay nada pausado.", ephemeral=True)
        controls.note_action(interaction.guild, f"▶️ Reanudada por {interaction.user.mention}")
        await interaction.response.send_message("▶️ Reanudada.", ephemeral=True)
        await controls.refresh_card(interaction.guild)

    @app_commands.command(name="loop", description="Configura el modo de repetición (Bucle)")
    @app_commands.guild_only()
    @app_commands.choices(modo=[
        app_commands.Choice(name="⛔ Desactivado", value=0),
        app_commands.Choice(name="🔂 Canción Actual", value=1),
        app_commands.Choice(name="🔁 Toda la Cola", value=2)
    ])
    async def loop(self, interaction: discord.Interaction, modo: app_commands.Choice[int]):
        if await deny_control(interaction):
            return

        player = music_manager.get_player(interaction.guild)
        player.loop_mode = modo.value
        controls.note_action(interaction.guild, controls.loop_action(interaction.user, modo.value))

        msgs = {LOOP_OFF: "➡️ Bucle **quitado**.", LOOP_CURRENT: "🔂 Se repetirá la **canción actual**."}
        await interaction.response.send_message(msgs.get(modo.value, "🔁 Se repetirá **toda la cola**."),
                                                ephemeral=True)
        await controls.refresh_card(interaction.guild)

    @app_commands.command(name="shuffle", description="Mezcla de forma aleatoria la cola de reproducción")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if await deny_control(interaction):
            return

        player = music_manager.peek(interaction.guild_id)
        if player is None or len(player.queue) < 2:
            return await interaction.response.send_message("❌ Necesitas al menos 2 canciones en la cola.",
                                                           ephemeral=True)

        player.shuffle_queue()
        await interaction.response.send_message(
            f"🔀 {interaction.user.mention} ha mezclado la cola (**{len(player.queue)}** canciones).",
            allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(name="remove", description="Quita una canción de la cola por su número (mira /queue)")
    @app_commands.guild_only()
    @app_commands.describe(posicion="Número de la canción en la cola")
    async def remove(self, interaction: discord.Interaction, posicion: int):
        if await deny_control(interaction):
            return
        player = music_manager.peek(interaction.guild_id)
        if player is None or not player.queue:
            return await interaction.response.send_message("📭 La cola está vacía.", ephemeral=True)
        if posicion < 1 or posicion > len(player.queue):
            return await interaction.response.send_message(
                f"❌ Número inválido. La cola tiene **{len(player.queue)}** canciones.", ephemeral=True
            )

        removed = player.remove_at(posicion - 1)
        if removed:
            await interaction.response.send_message(
                f"🗑️ {interaction.user.mention} ha quitado **{controls.song_label(removed, link=False)}** de la cola.",
                allowed_mentions=NO_MENTIONS
            )
            await controls.refresh_card(interaction.guild)
        else:
            await interaction.response.send_message("❌ No pude quitar esa canción.", ephemeral=True)

    @app_commands.command(name="volume", description="Ajusta el volumen de reproducción (0-100)")
    @app_commands.guild_only()
    @app_commands.describe(nivel="Volumen de 0 a 100")
    async def volume(self, interaction: discord.Interaction, nivel: app_commands.Range[int, 0, 100]):
        if await deny_control(interaction):
            return
        player = music_manager.get_player(interaction.guild)
        player.volume = nivel / 100

        # Aplicar en vivo si hay algo sonando
        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume

        controls.note_action(interaction.guild, f"🔊 Volumen puesto al {nivel}% por {interaction.user.mention}")
        await interaction.response.send_message(f"🔊 Volumen al **{nivel}%**.", ephemeral=True)
        await controls.refresh_card(interaction.guild)

    @app_commands.command(name="clear", description="Vacía la cola (sin cortar la canción que suena ahora)")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction):
        if await deny_control(interaction):
            return
        player = music_manager.peek(interaction.guild_id)
        count = len(player.queue) if player else 0
        if count == 0:
            return await interaction.response.send_message("📭 La cola ya está vacía.", ephemeral=True)

        player.clear_queue()
        await interaction.response.send_message(
            f"🧹 {interaction.user.mention} ha vaciado la cola (**{count}** "
            f"{'canción' if count == 1 else 'canciones'}). La que suena sigue.",
            allowed_mentions=NO_MENTIONS
        )
        await controls.refresh_card(interaction.guild)

    @app_commands.command(name="stop", description="Detiene la música, limpia la cola y expulsa al bot")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if await deny_control(interaction):
            return
        if interaction.guild.voice_client is None:
            return await interaction.response.send_message("❌ No estoy conectado.", ephemeral=True)
        await interaction.response.send_message(controls.stop_text(interaction.user), allowed_mentions=NO_MENTIONS)
        await disconnect_player(interaction.guild)
