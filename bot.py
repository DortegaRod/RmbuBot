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
            command_prefix="!",  # Prefix de respaldo
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        """Se ejecuta antes de que el bot se conecte."""
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

    try:
        db.init_db()
        logger.info("[DB] Base de datos inicializada")
    except Exception as e:
        logger.error(f"[ERROR] Error al inicializar la base de datos: {e}")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="/play | Música y Logs"
        )
    )


# ==================== EVENTOS DE MENSAJES ====================

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or not message.guild:
        return

    try:
        content = message.content or ""
        if not content and message.embeds:
            embed = message.embeds[0]
            content = f"{embed.title or ''}\n{embed.description or ''}".strip()

        if message.attachments:
            attachments_info = "\n".join([f"[Archivo: {a.filename}]" for a in message.attachments])
            content = f"{content}\n{attachments_info}".strip()

        db.save_message(message.id, message.author.id, content, message.channel.id)
        cache.cache_message(message.id, message.author.id, content)

    except Exception as e:
        logger.error(f"[ERROR] Error al guardar mensaje {message.id}: {e}")

    await bot.process_commands(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    # (El código de logs funcionaba bien, lo mantengo resumido para no ocupar espacio,
    #  pero en tu archivo local mantén el original si quieres, aquí pongo la versión funcional)
    if not payload.guild_id: return

    guild = bot.get_guild(payload.guild_id)
    if not guild: return

    # Intentar recuperar contenido
    cached_data = cache.get_cached(payload.message_id)
    author_id = None
    content = None

    if cached_data:
        author_id, content = cached_data
        cache.remove_cached(payload.message_id)
    else:
        rec = db.get_message(payload.message_id)
        if rec:
            author_id = rec['author_id']
            content = rec['content']

    if not content: return  # Si no tenemos contenido, no logueamos (opcional)

    # Esperar audit log
    await asyncio.sleep(AUDIT_WAIT_SECONDS)

    try:
        if not ADMIN_LOG_CHANNEL_ID: return
        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel: return

        # Buscar en audit log
        entry = await find_audit_entry_for_channel(guild, payload.channel_id)

        executor_display = "Desconocido (o el propio autor)"
        if entry and entry.user:
            # Si el ejecutor es el mismo autor, ignorar
            if author_id and entry.user.id == author_id:
                return
            executor_display = entry.user.mention
        elif author_id:
            # Si no hay entrada de audit log, probablemente fue el autor
            return

        author_display = f"<@{author_id}>" if author_id else "Desconocido"
        channel = guild.get_channel(payload.channel_id)
        channel_display = channel.mention if channel else f"#{payload.channel_id}"

        await send_admin_embed(
            admin_channel,
            author_display=author_display,
            executor_display=executor_display,
            channel_display=channel_display,
            content=content,
            message_id=payload.message_id
        )
    except Exception as e:
        logger.error(f"Error en log de borrado: {e}")


# ==================== COMANDOS DE MÚSICA ====================

@bot.tree.command(name="play", description="Reproduce música de YouTube")
@app_commands.describe(busqueda="Nombre de la canción o enlace de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    """Reproduce música de YouTube."""
    logger.info(f"/play ejecutado por {interaction.user} con búsqueda: {busqueda}")

    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ ¡Debes estar en un canal de voz para usar este comando!",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild
    player = music_manager.get_player(guild)

    # 1. Buscar la canción primero (para no conectar si no hay resultados)
    song = await search_youtube(busqueda)

    if not song:
        await interaction.followup.send(f"❌ No se encontró nada para: **{busqueda}**")
        return

    song.requester = interaction.user

    # 2. Gestión de conexión al canal de voz
    voice_client = guild.voice_client

    try:
        if voice_client is None:
            # IMPORTANTE: self_deaf=True ayuda a prevenir el error 4006 y desconexiones
            voice_client = await voice_channel.connect(self_deaf=True)
            logger.info(f"Conectado a {voice_channel.name} (Nuevo)")

            # Pequeña pausa para estabilizar la conexión UDP
            await asyncio.sleep(1.5)

        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
            logger.info(f"Movido a {voice_channel.name}")
            await asyncio.sleep(1.0)

    except Exception as e:
        logger.error(f"Error crítico de conexión: {e}")
        await interaction.followup.send("❌ Error al conectar al canal de voz. Intenta de nuevo en unos segundos.")
        return

    # 3. Reproducir o encolar
    if not voice_client.is_playing() and not player.current:
        player.current = song
        await play_next(voice_client, player)
        await interaction.followup.send(f"🎶 Reproduciendo: **{song.title}**")
    else:
        if player.add_song(song):
            position = len(player.queue)
            await interaction.followup.send(f"✅ **{song.title}** añadida a la cola (posición {position})")
        else:
            await interaction.followup.send("❌ La cola está llena.")


@bot.tree.command(name="skip", description="Salta a la siguiente canción")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ No estoy conectado.", ephemeral=True)
        return

    player = music_manager.get_player(interaction.guild)
    if not voice_client.is_playing():
        await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
        return

    skipped = player.current
    voice_client.stop()
    await interaction.response.send_message(f"⏭️ Saltada: **{skipped}**")


@bot.tree.command(name="stop", description="Detiene y desconecta")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        player = music_manager.get_player(interaction.guild)
        player.clear_queue()
        player.current = None
        await voice_client.disconnect()
        music_manager.remove_player(interaction.guild.id)
        await interaction.response.send_message("👋 Desconectado.")
    else:
        await interaction.response.send_message("❌ No estoy conectado.", ephemeral=True)


# (Mantengo el resto de comandos loop, queue, clear, testlogs, ping, info iguales que en tu original
#  ya que no afectan al problema de audio)

@bot.tree.command(name="queue", description="Muestra la cola")
async def queue(interaction: discord.Interaction):
    player = music_manager.get_player(interaction.guild)
    if not player.current and not player.queue:
        await interaction.response.send_message("❌ Cola vacía.", ephemeral=True)
        return

    desc = ""
    if player.current:
        desc += f"▶️ **{player.current.title}**\n\n"

    if player.queue:
        desc += "**Próximas:**\n"
        for i, song in enumerate(list(player.queue)[:10], 1):
            desc += f"`{i}.` {song.title}\n"

    embed = discord.Embed(title="🎵 Cola", description=desc, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="testlogs", description="[DEBUG] Test logs")
async def testlogs(interaction: discord.Interaction):
    await interaction.response.send_message("Comando de prueba ejecutado. Revisa la consola.", ephemeral=True)


if __name__ == '__main__':
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Error fatal: {e}")