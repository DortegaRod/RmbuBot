import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, INTENTS, AUDIT_WAIT_SECONDS
import db
import cache
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel
from music import music_manager, search_youtube, play_next

# Logging visible en consola
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# INTENTS: Esto debe coincidir con lo que activaste en el portal
intents = discord.Intents.default()
intents.message_content = True  # ¡CRUCIAL PARA LOGS!
intents.members = True
intents.voice_states = True


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        await self.tree.sync()


bot = MusicBot()


@bot.event
async def on_ready():
    logger.info(f"✅ Bot conectado como {bot.user}")
    db.init_db()


# --- EVENTOS DE LOGS ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild: return

    # Debug para ver si llegan mensajes
    # logger.info(f"Mensaje recibido de {message.author}: {message.content}")

    try:
        content = message.content
        if not content and message.embeds: content = "[Embed]"
        if message.attachments: content += f" [Adjunto: {message.attachments[0].filename}]"

        db.save_message(message.id, message.author.id, content, message.channel.id)
        cache.cache_message(message.id, message.author.id, content)
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if not payload.guild_id: return
    logger.info("🗑️ Mensaje eliminado detectado")

    # Recuperar contenido
    cached = cache.get_cached(payload.message_id)
    content = cached[1] if cached else None
    author_id = cached[0] if cached else None

    if not content:
        rec = db.get_message(payload.message_id)
        if rec:
            content = rec['content']
            author_id = rec['author_id']

    if not content: return  # No sabemos qué decía

    # Esperar audit log
    await asyncio.sleep(AUDIT_WAIT_SECONDS)

    try:
        guild = bot.get_guild(payload.guild_id)
        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel:
            logger.warning("Canal de logs no encontrado")
            return

        # Buscar quién lo borró
        entry = await find_audit_entry_for_channel(guild, payload.channel_id)
        executor = entry.user if entry else None

        # Ignorar auto-borrado
        if author_id and executor and executor.id == author_id: return

        author_display = f"<@{author_id}>" if author_id else "Desconocido"
        executor_display = executor.mention if executor else "Desconocido (o autor)"
        channel = guild.get_channel(payload.channel_id)

        await send_admin_embed(
            admin_channel,
            author_display=author_display,
            executor_display=executor_display,
            channel_display=channel.mention,
            content=content,
            message_id=payload.message_id
        )
        logger.info("✅ Log enviado al canal admin")
    except Exception as e:
        logger.error(f"Error enviando log: {e}")


# --- COMANDOS MÚSICA ---
@bot.tree.command(name="play", description="Reproduce música")
async def play(interaction: discord.Interaction, busqueda: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    # 1. Avisar que estamos procesando
    await interaction.response.defer()
    logger.info(f"Comando play recibido: {busqueda}")

    try:
        # 2. Buscar ANTES de conectar (para no entrar y salir si falla)
        song = await search_youtube(busqueda)
        if not song:
            return await interaction.followup.send("❌ No encontré esa canción o YouTube bloqueó la búsqueda.")

        # 3. Conectar
        guild = interaction.guild
        voice_channel = interaction.user.voice.channel
        player = music_manager.get_player(guild)

        vc = guild.voice_client
        if not vc:
            # self_deaf=True es vital para evitar bugs de conexión
            vc = await voice_channel.connect(self_deaf=True)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)

        # 4. Reproducir
        song.requester = interaction.user
        if not vc.is_playing() and not player.current:
            player.current = song
            await play_next(vc, player)
            await interaction.followup.send(f"▶️ Reproduciendo: **{song.title}**")
        else:
            player.add_song(song)
            await interaction.followup.send(f"📝 Añadido a la cola: **{song.title}**")

    except Exception as e:
        logger.error(f"Error en comando play: {e}")
        await interaction.followup.send("❌ Hubo un error interno.")


@bot.tree.command(name="stop", description="Desconectar")
async def stop(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        music_manager.remove_player(interaction.guild.id)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Adiós")
    else:
        await interaction.response.send_message("❌ No estoy conectado", ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN)