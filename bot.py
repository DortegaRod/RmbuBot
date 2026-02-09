import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from typing import Optional

from config import TOKEN, ADMIN_LOG_CHANNEL_ID, INTENTS, AUDIT_WAIT_SECONDS
import db
import cache
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel
from music import music_manager, search_youtube, play_next, Song

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configurar intents
intents = discord.Intents.default()
intents.guilds = INTENTS.get("guilds", True)
intents.messages = INTENTS.get("messages", True)
intents.message_content = INTENTS.get("message_content", True)
intents.members = INTENTS.get("members", True)
intents.voice_states = INTENTS.get("voice_states", True)


class MusicBot(commands.Bot):
    """Bot principal con funcionalidades de música y logging."""

    def __init__(self):
        super().__init__(
            command_prefix="!",  # Prefix de respaldo, usaremos slash commands
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        """Se ejecuta antes de que el bot se conecte."""
        # Sincronizar comandos slash
        try:
            synced = await self.tree.sync()
            logger.info(f"[SYNC] {len(synced)} comandos sincronizados")
        except Exception as e:
            logger.error(f"[ERROR] Error al sincronizar comandos: {e}")


bot = MusicBot()


@bot.event
async def on_ready():
    """Se ejecuta cuando el bot está listo."""
    logger.info(f"[READY] Bot conectado como {bot.user} (ID: {bot.user.id})")
    logger.info(f"[READY] Conectado a {len(bot.guilds)} servidor(es)")

    # Inicializar base de datos
    try:
        db.init_db()
        logger.info("[DB] Base de datos inicializada")
    except Exception as e:
        logger.error(f"[ERROR] Error al inicializar la base de datos: {e}")

    # Establecer estado del bot
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/play | Música y Logs"
        )
    )


# ==================== EVENTOS DE MENSAJES ====================

@bot.event
async def on_message(message: discord.Message):
    """Guarda los mensajes en la base de datos y cache."""
    # Ignorar mensajes del bot
    if message.author == bot.user:
        return

    # Solo procesar mensajes de servidores
    if not message.guild:
        return

    try:
        # Obtener contenido del mensaje
        content = message.content or ""

        # Si no hay contenido de texto, intentar obtener de embeds
        if not content and message.embeds:
            embed = message.embeds[0]
            title = embed.title or ""
            description = embed.description or ""
            content = f"{title}\n{description}".strip()

        # Guardar en base de datos
        db.save_message(message.id, message.author.id, content, message.channel.id)

        # Guardar en cache
        cache.cache_message(message.id, message.author.id, content)

    except Exception as e:
        logger.error(f"[ERROR] Error al guardar mensaje {message.id}: {e}")

    # Procesar comandos (por si acaso usas prefix commands)
    await bot.process_commands(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Detecta cuando se elimina un mensaje y registra quién lo eliminó."""
    # Obtener el servidor
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    # Obtener el canal
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return

    # Intentar obtener el mensaje del cache primero
    cached_data = cache.get_cached(payload.message_id)

    if cached_data:
        author_id, content = cached_data
        # Eliminar del cache
        cache.remove_cached(payload.message_id)
    else:
        # Si no está en cache, buscar en DB
        rec = db.get_message(payload.message_id)
        if not rec:
            return
        author_id = rec['author_id']
        content = rec['content']

    # Esperar un momento para que se registre en audit log
    await asyncio.sleep(AUDIT_WAIT_SECONDS)

    # Buscar quién eliminó el mensaje
    entry = await find_audit_entry_for_channel(guild, channel.id)

    # Obtener canal de administración
    admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
    if not admin_channel:
        logger.warning(f"[WARN] Canal de administración no encontrado: {ADMIN_LOG_CHANNEL_ID}")
        return

    author_display = f"<@{author_id}>"

    # Si encontramos la entrada de auditoría
    if entry and entry.user:
        executor = entry.user

        # No notificar si el autor eliminó su propio mensaje
        if author_id and executor.id == author_id:
            logger.debug(f"Usuario {author_id} eliminó su propio mensaje, no notificar")
            return

        try:
            await send_admin_embed(
                admin_channel,
                author_display=author_display,
                executor_display=executor.mention,
                channel_display=channel.mention,
                content=content or "(sin contenido)",
                message_id=payload.message_id
            )
        except Exception as e:
            logger.error(f"[ERROR] Error al enviar notificación: {e}")


# ==================== COMANDOS DE MÚSICA ====================

@bot.tree.command(name="play", description="Reproduce música de YouTube")
@app_commands.describe(busqueda="Nombre de la canción o enlace de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    """Reproduce música de YouTube."""
    # Verificar que el usuario esté en un canal de voz
    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ ¡Debes estar en un canal de voz para usar este comando!",
            ephemeral=True
        )
        return

    # Diferir la respuesta porque la búsqueda puede tardar
    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild

    # Obtener el reproductor del servidor
    player = music_manager.get_player(guild)

    # Buscar la canción
    song = await search_youtube(busqueda)
    if not song:
        await interaction.followup.send("❌ No se pudo encontrar la canción.")
        return

    song.requester = interaction.user

    # Conectar al canal de voz si no está conectado
    voice_client = guild.voice_client
    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    # Si no hay nada reproduciéndose, reproducir inmediatamente
    if not voice_client.is_playing() and not player.current:
        player.current = song
        await play_next(voice_client, player)
        await interaction.followup.send(f"🎶 Reproduciendo: **{song.title}**")
    else:
        # Añadir a la cola
        if player.add_song(song):
            position = len(player.queue)
            await interaction.followup.send(
                f"✅ **{song.title}** añadida a la cola (posición {position})"
            )
        else:
            await interaction.followup.send("❌ La cola está llena.")


@bot.tree.command(name="skip", description="Salta a la siguiente canción")
async def skip(interaction: discord.Interaction):
    """Salta a la siguiente canción en la cola."""
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ No estoy conectado a un canal de voz.", ephemeral=True)
        return

    player = music_manager.get_player(interaction.guild)

    if not voice_client.is_playing():
        await interaction.response.send_message("❌ No hay nada reproduciéndose.", ephemeral=True)
        return

    skipped = player.current
    voice_client.stop()  # Esto activará el callback de play_next

    await interaction.response.send_message(f"⏭️ Canción saltada: **{skipped}**")


@bot.tree.command(name="stop", description="Detiene la música y desconecta el bot")
async def stop(interaction: discord.Interaction):
    """Detiene la música y desconecta el bot."""
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ No estoy conectado a un canal de voz.", ephemeral=True)
        return

    player = music_manager.get_player(interaction.guild)
    player.clear_queue()
    player.current = None

    await voice_client.disconnect()
    music_manager.remove_player(interaction.guild.id)

    await interaction.response.send_message("👋 Música detenida y desconectado.")


@bot.tree.command(name="pause", description="Pausa la música")
async def pause(interaction: discord.Interaction):
    """Pausa la reproducción de música."""
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_playing():
        await interaction.response.send_message("❌ No hay nada reproduciéndose.", ephemeral=True)
        return

    voice_client.pause()
    await interaction.response.send_message("⏸️ Música pausada.")


@bot.tree.command(name="resume", description="Reanuda la música")
async def resume(interaction: discord.Interaction):
    """Reanuda la reproducción de música."""
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_paused():
        await interaction.response.send_message("❌ No hay nada pausado.", ephemeral=True)
        return

    voice_client.resume()
    await interaction.response.send_message("▶️ Música reanudada.")


@bot.tree.command(name="queue", description="Muestra la cola de reproducción")
async def queue(interaction: discord.Interaction):
    """Muestra la cola de reproducción actual."""
    player = music_manager.get_player(interaction.guild)

    if not player.current and not player.queue:
        await interaction.response.send_message("❌ No hay canciones en la cola.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎵 Cola de Reproducción",
        color=discord.Color.blue()
    )

    # Canción actual
    if player.current:
        current_info = f"**{player.current.title}**"
        if player.current.requester:
            current_info += f"\nSolicitada por: {player.current.requester.mention}"
        if player.loop:
            current_info += "\n🔁 **[En bucle]**"
        embed.add_field(name="▶️ Reproduciendo ahora", value=current_info, inline=False)

    # Próximas canciones
    if player.queue:
        queue_text = ""
        for i, song in enumerate(list(player.queue)[:10], 1):
            queue_text += f"{i}. **{song.title}**\n"

        if len(player.queue) > 10:
            queue_text += f"\n...y {len(player.queue) - 10} más"

        embed.add_field(name="📋 Próximas canciones", value=queue_text, inline=False)

    embed.set_footer(text=f"Total: {len(player.queue)} canción(es) en cola")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="loop", description="Activa o desactiva el modo bucle")
async def loop(interaction: discord.Interaction):
    """Activa o desactiva el modo bucle para la canción actual."""
    player = music_manager.get_player(interaction.guild)

    if not player.current:
        await interaction.response.send_message("❌ No hay nada reproduciéndose.", ephemeral=True)
        return

    is_looping = player.toggle_loop()

    if is_looping:
        await interaction.response.send_message(f"🔁 Bucle activado para: **{player.current.title}**")
    else:
        await interaction.response.send_message("🔁 Bucle desactivado.")


@bot.tree.command(name="clear", description="Limpia la cola de reproducción")
async def clear(interaction: discord.Interaction):
    """Limpia toda la cola de reproducción."""
    player = music_manager.get_player(interaction.guild)

    count = player.clear_queue()

    if count == 0:
        await interaction.response.send_message("❌ La cola ya está vacía.", ephemeral=True)
    else:
        await interaction.response.send_message(f"🗑️ Se eliminaron **{count}** canción(es) de la cola.")


# ==================== COMANDOS DE UTILIDAD ====================

@bot.tree.command(name="ping", description="Verifica la latencia del bot")
async def ping(interaction: discord.Interaction):
    """Muestra la latencia del bot."""
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latencia: **{latency}ms**")


@bot.tree.command(name="info", description="Información sobre el bot")
async def info(interaction: discord.Interaction):
    """Muestra información sobre el bot."""
    embed = discord.Embed(
        title="ℹ️ Información del Bot",
        description="Bot multifuncional con música y sistema de logs",
        color=discord.Color.blue()
    )

    embed.add_field(name="Servidores", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Latencia", value=f"{round(bot.latency * 1000)}ms", inline=True)

    # Estadísticas del cache
    cache_stats = cache.get_cache_stats()
    embed.add_field(
        name="Cache",
        value=f"{cache_stats['size']}/{cache_stats['max_size']} ({cache_stats['usage_percent']:.1f}%)",
        inline=True
    )

    embed.set_footer(text=f"Bot ID: {bot.user.id}")

    await interaction.response.send_message(embed=embed)


# ==================== MANEJO DE ERRORES ====================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Maneja errores de comandos slash."""
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏱️ Este comando está en cooldown. Intenta de nuevo en {error.retry_after:.2f}s.",
            ephemeral=True
        )
    elif isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ No tienes permisos para usar este comando.",
            ephemeral=True
        )
    else:
        logger.error(f"Error en comando: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Ocurrió un error al ejecutar el comando.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Ocurrió un error al ejecutar el comando.", ephemeral=True)
        except:
            pass


# ==================== INICIAR BOT ====================

if __name__ == '__main__':
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Error al iniciar el bot: {e}")