import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, MUSIC_CHANNEL_ID, INTENTS, AUDIT_WAIT_SECONDS, INACTIVITY_TIMEOUT
import db
import cache
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel
from music import music_manager, search_youtube, play_next, LOOP_OFF, LOOP_CURRENT, LOOP_QUEUE

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Comandos sincronizados con Discord")


bot = MusicBot()


@bot.event
async def on_ready():
    logger.info(f"✅ Bot conectado como {bot.user}")
    db.init_db()


@bot.event
async def on_voice_state_update(member, before, after):
    """Maneja cambios en estados de voz."""
    guild_id = member.guild.id
    vc = member.guild.voice_client

    # Si el bot fue desconectado, limpiar el player
    if member.id == bot.user.id and after.channel is None:
        music_manager.remove_player(guild_id)
        logger.info(f"Bot desconectado de voz en {member.guild.name}")
        return


@bot.event
async def on_message(message: discord.Message):
    """Guarda todos los mensajes para el sistema de logs."""
    if message.author.bot or not message.guild:
        return

    try:
        content = message.content or ("[Embed]" if message.embeds else "[Sin contenido]")
        db.save_message(message.id, message.author.id, content, message.channel.id)
        cache.cache_message(message.id, message.author.id, content)
    except Exception as e:
        logger.error(f"Error guardando mensaje {message.id}: {e}")


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Registra mensajes eliminados en el canal de administración."""
    if not payload.guild_id:
        return

    # Intentar recuperar del cache primero
    cached = cache.get_cached(payload.message_id)
    content = cached[1] if cached else None
    author_id = cached[0] if cached else None

    # Si no está en cache, buscar en DB
    if not content:
        rec = db.get_message(payload.message_id)
        if rec:
            content, author_id = rec['content'], rec['author_id']

    # Si no tenemos contenido, no hay nada que reportar
    if not content:
        return

    # Esperar un poco para que la auditoría se registre
    await asyncio.sleep(AUDIT_WAIT_SECONDS)

    try:
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            return

        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel:
            logger.warning(f"Canal de admin {ADMIN_LOG_CHANNEL_ID} no encontrado")
            return

        # Buscar quién eliminó el mensaje
        entry = await find_audit_entry_for_channel(guild, payload.channel_id)
        executor = entry.user if entry else None

        # Si el autor eliminó su propio mensaje, no registrar
        if author_id and executor and executor.id == author_id:
            logger.debug(f"Usuario {author_id} eliminó su propio mensaje, no se registra")
            return

        await send_admin_embed(
            admin_channel,
            author_display=f"<@{author_id}>" if author_id else "Desconocido",
            executor_display=executor.mention if executor else "Desconocido",
            channel_display=guild.get_channel(payload.channel_id).mention if guild.get_channel(
                payload.channel_id) else f"Canal ID: {payload.channel_id}",
            content=content,
            message_id=payload.message_id
        )
    except Exception as e:
        logger.error(f"Error enviando log de eliminación: {e}")


def check_music_channel(interaction: discord.Interaction) -> bool:
    """Verifica si el comando se ejecuta en el canal de música correcto."""
    # Si MUSIC_CHANNEL_ID es 0, permitir en cualquier canal
    return not MUSIC_CHANNEL_ID or interaction.channel_id == MUSIC_CHANNEL_ID


@bot.tree.command(name="play", description="Reproduce música o playlists")
async def play(interaction: discord.Interaction, busqueda: str):
    """Comando principal para reproducir música."""
    # Verificar canal
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Solo en <#{MUSIC_CHANNEL_ID}>",
            ephemeral=True
        )

    # Verificar que el usuario esté en un canal de voz
    if not interaction.user.voice:
        return await interaction.response.send_message(
            "❌ Entra a un canal de voz primero.",
            ephemeral=True
        )

    # Defer para tener más tiempo
    await interaction.response.defer()

    # Buscar en YouTube
    songs = await search_youtube(busqueda)
    if not songs:
        return await interaction.followup.send("❌ No encontré resultados.")

    guild = interaction.guild
    voice_channel = interaction.user.voice.channel
    player = music_manager.get_player(guild)
    vc = guild.voice_client

    # Conectar al canal de voz
    try:
        if not vc:
            vc = await voice_channel.connect(self_deaf=True)
            logger.info(f"Conectado a {voice_channel.name} en {guild.name}")
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
            logger.info(f"Movido a {voice_channel.name} en {guild.name}")
    except Exception as e:
        logger.error(f"Error de conexión: {e}")
        return await interaction.followup.send(f"❌ Error de conexión: {e}")

    # Añadir canciones a la cola
    added_count = 0
    rejected_count = 0
    for s in songs:
        s.requester = interaction.user
        if player.add_song(s):
            added_count += 1
        else:
            rejected_count += 1

    # Si no estamos reproduciendo, empezar
    is_playing_now = False
    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        is_playing_now = True

    # Crear embed de respuesta
    if len(songs) > 1:
        # Es una playlist
        desc = f"Se han añadido **{added_count}** canciones."
        if rejected_count > 0:
            desc += f"\n⚠️ {rejected_count} canciones rechazadas (cola llena)."

        embed = discord.Embed(
            title="📂 Playlist Añadida",
            description=desc,
            color=discord.Color.purple()
        )

        # Mostrar las primeras 3
        if added_count > 0:
            preview = []
            for i, song in enumerate(songs[:3], 1):
                preview.append(f"`{i}.` {song.title}")
            embed.add_field(
                name="Vista previa",
                value="\n".join(preview),
                inline=False
            )
    else:
        # Es una sola canción
        s = songs[0]
        embed = discord.Embed(
            title="🎶 Reproduciendo" if is_playing_now else "📝 En cola",
            description=f"**[{s.title}]({s.webpage_url})**",
            color=discord.Color.green() if is_playing_now else discord.Color.blue()
        )
        if s.thumbnail:
            embed.set_thumbnail(url=s.thumbnail)

    embed.set_footer(
        text=f"Pedido por {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="loop", description="Configura el modo de repetición")
@app_commands.choices(modo=[
    app_commands.Choice(name="⛔ Desactivado", value=0),
    app_commands.Choice(name="🔂 Canción Actual", value=1),
    app_commands.Choice(name="🔁 Toda la Cola", value=2)
])
async def loop(interaction: discord.Interaction, modo: app_commands.Choice[int]):
    """Configura el modo de bucle."""
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Solo en <#{MUSIC_CHANNEL_ID}>",
            ephemeral=True
        )

    player = music_manager.get_player(interaction.guild)
    player.loop_mode = modo.value

    msgs = {
        0: "⛔ Modo bucle **desactivado**.",
        1: "🔂 Bucle: **Canción Actual**.",
        2: "🔁 Bucle: **Toda la Cola**."
    }
    await interaction.response.send_message(msgs[modo.value])


@bot.tree.command(name="shuffle", description="Mezcla aleatoriamente la cola")
async def shuffle(interaction: discord.Interaction):
    """Mezcla las canciones en la cola."""
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Solo en <#{MUSIC_CHANNEL_ID}>",
            ephemeral=True
        )

    player = music_manager.get_player(interaction.guild)
    if len(player.queue) < 2:
        return await interaction.response.send_message(
            "❌ Necesitas al menos 2 canciones en la cola.",
            ephemeral=True
        )

    player.shuffle_queue()
    await interaction.response.send_message("🔀 **Cola mezclada** aleatoriamente.")


@bot.tree.command(name="skip", description="Salta la canción")
async def skip(interaction: discord.Interaction):
    """Salta la canción actual."""
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Solo en <#{MUSIC_CHANNEL_ID}>",
            ephemeral=True
        )

    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Canción saltada")
    else:
        await interaction.response.send_message(
            "❌ No hay nada sonando.",
            ephemeral=True
        )


@bot.tree.command(name="stop", description="Detiene la música y desconecta el bot")
async def stop(interaction: discord.Interaction):
    """Desconecta el bot."""
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Solo en <#{MUSIC_CHANNEL_ID}>",
            ephemeral=True
        )

    if interaction.guild.voice_client:
        music_manager.remove_player(interaction.guild.id)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Desconectado")
    else:
        await interaction.response.send_message(
            "❌ No estoy conectado a ningún canal.",
            ephemeral=True
        )


@bot.tree.command(name="queue", description="Muestra las próximas canciones")
async def queue(interaction: discord.Interaction):
    """Muestra la cola de reproducción."""
    player = music_manager.get_player(interaction.guild)

    if not player.current and len(player.queue) == 0:
        return await interaction.response.send_message("🔭 La cola está vacía.")

    desc = ""

    # Canción actual
    if player.current:
        desc += f"**💿 Sonando ahora:**\n[{player.current.title}]({player.current.webpage_url})\n"
        if player.current.requester:
            desc += f"*Pedida por {player.current.requester.display_name}*\n\n"

    # Próximas canciones
    if len(player.queue) > 0:
        desc += "**⏱️ Próximas:**\n"
        upcoming = list(player.queue)[:10]
        for i, song in enumerate(upcoming, 1):
            # Truncar títulos muy largos
            title = song.title if len(song.title) <= 50 else song.title[:47] + "..."
            desc += f"`{i}.` {title}\n"

        if len(player.queue) > 10:
            desc += f"\n*...y {len(player.queue) - 10} más en espera.*"

    # Modo de bucle
    modes = {0: "⛔ Off", 1: "🔂 Canción", 2: "🔁 Cola"}
    loop_status = modes.get(player.loop_mode, "Off")

    embed = discord.Embed(
        title="🎵 Cola de Reproducción",
        description=desc,
        color=discord.Color.blue()
    )

    # Thumbnail de la canción actual
    if player.current and player.current.thumbnail:
        embed.set_thumbnail(url=player.current.thumbnail)

    total_songs = len(player.queue) + (1 if player.current else 0)
    embed.set_footer(text=f"Modo Bucle: {loop_status} | Total: {total_songs} canciones")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Muestra la canción actual")
async def nowplaying(interaction: discord.Interaction):
    """Muestra información de la canción actual."""
    player = music_manager.get_player(interaction.guild)

    if not player.current:
        return await interaction.response.send_message(
            "❌ No hay nada sonando ahora.",
            ephemeral=True
        )

    song = player.current
    embed = discord.Embed(
        title="🎵 Reproduciendo ahora",
        description=f"**[{song.title}]({song.webpage_url})**",
        color=discord.Color.green()
    )

    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    if song.requester:
        embed.add_field(
            name="Pedida por",
            value=song.requester.mention,
            inline=True
        )

    # Modo de bucle
    modes = {0: "⛔ Off", 1: "🔂 Canción Actual", 2: "🔁 Toda la Cola"}
    embed.add_field(
        name="Modo bucle",
        value=modes.get(player.loop_mode, "Off"),
        inline=True
    )

    # Canciones en cola
    embed.add_field(
        name="En cola",
        value=f"{len(player.queue)} canciones",
        inline=True
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="clear", description="Limpia toda la cola de reproducción")
async def clear(interaction: discord.Interaction):
    """Limpia la cola de reproducción."""
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Solo en <#{MUSIC_CHANNEL_ID}>",
            ephemeral=True
        )

    player = music_manager.get_player(interaction.guild)

    if len(player.queue) == 0:
        return await interaction.response.send_message(
            "❌ La cola ya está vacía.",
            ephemeral=True
        )

    count = len(player.queue)
    player.clear_queue()

    await interaction.response.send_message(
        f"🗑️ Se eliminaron **{count}** canciones de la cola."
    )


if __name__ == '__main__':
    bot.run(TOKEN)