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

        # Si hay attachments, agregar info
        if message.attachments:
            attachments_info = "\n".join([f"[Archivo: {a.filename}]" for a in message.attachments])
            content = f"{content}\n{attachments_info}".strip()

        # Log para debug
        logger.debug(f"Guardando mensaje {message.id} de {message.author.name} en #{message.channel.name}")

        # Guardar en base de datos
        success = db.save_message(message.id, message.author.id, content, message.channel.id)
        if success:
            logger.debug(f"✅ Mensaje {message.id} guardado en DB")
        else:
            logger.warning(f"⚠️ No se pudo guardar mensaje {message.id} en DB")

        # Guardar en cache
        cache.cache_message(message.id, message.author.id, content)
        logger.debug(f"✅ Mensaje {message.id} guardado en cache")

    except Exception as e:
        logger.error(f"[ERROR] Error al guardar mensaje {message.id}: {e}", exc_info=True)

    # Procesar comandos (por si acaso usas prefix commands)
    await bot.process_commands(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Detecta cuando se elimina un mensaje y registra quién lo eliminó."""
    logger.info(f"🗑️ Mensaje {payload.message_id} eliminado en canal {payload.channel_id}")

    # Obtener el servidor
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        logger.warning(f"No se pudo obtener guild {payload.guild_id}")
        return

    # Obtener el canal
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        logger.warning(f"No se pudo obtener canal {payload.channel_id}")
        return

    # Intentar obtener el mensaje del cache primero
    cached_data = cache.get_cached(payload.message_id)

    if cached_data:
        author_id, content = cached_data
        # Eliminar del cache
        cache.remove_cached(payload.message_id)
        logger.debug(f"Mensaje encontrado en cache")
    else:
        # Si no está en cache, buscar en DB
        rec = db.get_message(payload.message_id)
        if not rec:
            logger.debug(f"Mensaje {payload.message_id} no encontrado en DB ni cache")
            return
        author_id = rec['author_id']
        content = rec['content']
        logger.debug(f"Mensaje encontrado en DB")

    # Esperar un momento para que se registre en audit log
    logger.debug(f"Esperando {AUDIT_WAIT_SECONDS}s para audit log...")
    await asyncio.sleep(AUDIT_WAIT_SECONDS)

    # Buscar quién eliminó el mensaje
    logger.debug("Buscando en audit log...")
    entry = await find_audit_entry_for_channel(guild, channel.id)

    # Obtener canal de administración
    if not ADMIN_LOG_CHANNEL_ID or ADMIN_LOG_CHANNEL_ID == 0:
        logger.error("⚠️ ADMIN_LOG_CHANNEL_ID no está configurado!")
        logger.error("   Configura ADMIN_LOG_CHANNEL_ID en tu .env o config_local.py")
        return

    admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
    if not admin_channel:
        logger.error(f"⚠️ Canal de administración no encontrado: {ADMIN_LOG_CHANNEL_ID}")
        logger.error(f"   Verifica que el ID sea correcto y que el bot pueda ver el canal")
        # Intentar listar canales disponibles
        available_channels = [f"{c.name} ({c.id})" for c in guild.text_channels if
                              c.permissions_for(guild.me).view_channel]
        logger.info(f"   Canales disponibles: {', '.join(available_channels[:5])}")
        return

    author_display = f"<@{author_id}>"

    # Si encontramos la entrada de auditoría
    if entry and entry.user:
        executor = entry.user
        logger.info(f"Audit log: {executor.name} eliminó mensaje de autor {author_id}")

        # No notificar si el autor eliminó su propio mensaje
        if author_id and executor.id == author_id:
            logger.debug(f"Usuario {author_id} eliminó su propio mensaje, no notificar")
            return

        try:
            logger.info(f"Enviando notificación al canal #{admin_channel.name}")
            await send_admin_embed(
                admin_channel,
                author_display=author_display,
                executor_display=executor.mention,
                channel_display=channel.mention,
                content=content or "(sin contenido)",
                message_id=payload.message_id
            )
            logger.info("✅ Notificación enviada correctamente")
        except discord.Forbidden:
            logger.error(f"❌ Sin permisos para enviar en #{admin_channel.name}")
        except Exception as e:
            logger.error(f"❌ Error al enviar notificación: {e}", exc_info=True)
    else:
        logger.debug("No se encontró entrada en audit log (posible auto-delete o bot)")


# ==================== COMANDOS DE MÚSICA ====================

@bot.tree.command(name="play", description="Reproduce música de YouTube")
@app_commands.describe(busqueda="Nombre de la canción o enlace de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    """Reproduce música de YouTube."""
    logger.info(f"/play ejecutado por {interaction.user} con búsqueda: {busqueda}")

    # Verificar que el usuario esté en un canal de voz
    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ ¡Debes estar en un canal de voz para usar este comando!",
            ephemeral=True
        )
        return

    # Diferir la respuesta porque la búsqueda puede tardar
    await interaction.response.defer()
    logger.debug("Respuesta diferida")

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild

    # Obtener el reproductor del servidor
    player = music_manager.get_player(guild)
    logger.debug(f"Reproductor obtenido para guild {guild.name}")

    try:
        # Buscar la canción
        logger.info(f"Iniciando búsqueda de: {busqueda}")
        song = await search_youtube(busqueda)

        if not song:
            logger.warning(f"No se encontró canción para: {busqueda}")
            await interaction.followup.send(
                f"❌ No se pudo encontrar la canción: **{busqueda}**\n"
                "Intenta con:\n"
                "• Otro término de búsqueda\n"
                "• Un enlace directo de YouTube\n"
                "• Verificar que el video no esté bloqueado"
            )
            return

        logger.info(f"Canción encontrada: {song.title}")
        song.requester = interaction.user

        # Conectar al canal de voz si no está conectado
        voice_client = guild.voice_client

        try:
            if voice_client is None:
                logger.info(f"Conectando a {voice_channel.name}")
                voice_client = await voice_channel.connect()
                logger.info(f"Conectado a {voice_channel.name} en {guild.name}")
            elif voice_client.channel != voice_channel:
                logger.info(f"Moviendo a {voice_channel.name}")
                await voice_client.move_to(voice_channel)
                logger.info(f"Movido a {voice_channel.name} en {guild.name}")
        except discord.errors.ClientException as e:
            logger.error(f"ClientException al conectar: {e}")
            await interaction.followup.send(
                "❌ Ya estoy conectado a otro canal de voz. Usa `/stop` primero."
            )
            return
        except Exception as e:
            logger.error(f"Error al conectar a voz: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ No pude conectarme al canal de voz. Verifica que tenga permisos."
            )
            return

        # Si no hay nada reproduciéndose, reproducir inmediatamente
        if not voice_client.is_playing() and not player.current:
            logger.info(f"Reproduciendo inmediatamente: {song.title}")
            player.current = song
            await play_next(voice_client, player)
            await interaction.followup.send(f"🎶 Reproduciendo: **{song.title}**")
        else:
            # Añadir a la cola
            logger.info(f"Añadiendo a la cola: {song.title}")
            if player.add_song(song):
                position = len(player.queue)
                await interaction.followup.send(
                    f"✅ **{song.title}** añadida a la cola (posición {position})"
                )
            else:
                logger.warning("Cola llena")
                await interaction.followup.send("❌ La cola está llena. Usa `/clear` para limpiarla.")

    except discord.errors.ClientException as e:
        logger.error(f"Error de Discord ClientException: {e}", exc_info=True)
        await interaction.followup.send(
            "❌ Ya estoy conectado a otro canal de voz. Usa `/stop` primero."
        )
    except Exception as e:
        logger.error(f"Error inesperado en /play: {e}", exc_info=True)
        await interaction.followup.send(
            f"❌ Ocurrió un error inesperado. Por favor, intenta de nuevo.\n"
            f"Error: {str(e)[:200]}"
        )


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

@bot.tree.command(name="testlogs", description="[DEBUG] Prueba el sistema de logs")
async def testlogs(interaction: discord.Interaction):
    """Comando de debug para probar el sistema de logs."""
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    # Verificar intents
    embed = discord.Embed(
        title="🔍 Diagnóstico del Sistema de Logs",
        color=discord.Color.blue()
    )

    # 1. Verificar Intents
    intents_ok = []
    intents_fail = []

    if bot.intents.guilds:
        intents_ok.append("guilds")
    else:
        intents_fail.append("guilds")

    if bot.intents.messages:
        intents_ok.append("messages")
    else:
        intents_fail.append("messages")

    if bot.intents.message_content:
        intents_ok.append("message_content")
    else:
        intents_fail.append("message_content")

    if bot.intents.members:
        intents_ok.append("members")
    else:
        intents_fail.append("members")

    intents_text = ""
    if intents_ok:
        intents_text += f"✅ Activos: {', '.join(intents_ok)}\n"
    if intents_fail:
        intents_text += f"❌ Inactivos: {', '.join(intents_fail)}\n"

    embed.add_field(name="Intents", value=intents_text or "✅ Todos activos", inline=False)

    # 2. Verificar canal de admin
    admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
    if admin_channel:
        perms = admin_channel.permissions_for(guild.me)
        can_send = perms.send_messages
        can_embed = perms.embed_links
        can_audit = guild.me.guild_permissions.view_audit_log

        channel_text = f"✅ Canal: {admin_channel.mention}\n"
        channel_text += f"{'✅' if can_send else '❌'} Enviar mensajes\n"
        channel_text += f"{'✅' if can_embed else '❌'} Enviar embeds\n"
        channel_text += f"{'✅' if can_audit else '❌'} Ver audit log"
    else:
        channel_text = f"❌ Canal no encontrado (ID: {ADMIN_LOG_CHANNEL_ID})"

    embed.add_field(name="Canal de Admin", value=channel_text, inline=False)

    # 3. Verificar base de datos
    try:
        # Intentar guardar un mensaje de prueba
        test_id = 9999999999999
        db.save_message(test_id, interaction.user.id, "TEST", interaction.channel.id)
        rec = db.get_message(test_id)
        if rec:
            db_text = "✅ Base de datos funcional"
            # Limpiar
            import sqlite3
            conn = sqlite3.connect(str(db.DB_PATH))
            conn.execute("DELETE FROM mensajes WHERE message_id = ?", (test_id,))
            conn.commit()
            conn.close()
        else:
            db_text = "⚠️ No se pudo leer de la BD"
    except Exception as e:
        db_text = f"❌ Error: {str(e)[:100]}"

    embed.add_field(name="Base de Datos", value=db_text, inline=False)

    # 4. Cache
    cache_stats = cache.get_cache_stats()
    cache_text = f"Tamaño: {cache_stats['size']}/{cache_stats['max_size']}\n"
    cache_text += f"Uso: {cache_stats['usage_percent']:.1f}%"
    embed.add_field(name="Cache", value=cache_text, inline=False)

    # 5. Instrucciones
    if intents_fail:
        embed.add_field(
            name="⚠️ Acción Requerida",
            value="Ve a Discord Developer Portal → Bot → Activa los intents faltantes → Reinicia el bot",
            inline=False
        )

    if not admin_channel:
        embed.add_field(
            name="⚠️ Configuración Requerida",
            value=f"Configura ADMIN_LOG_CHANNEL_ID en tu .env o config_local.py\n"
                  f"ID actual: {ADMIN_LOG_CHANNEL_ID}",
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


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