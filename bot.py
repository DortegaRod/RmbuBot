import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, MUSIC_CHANNEL_ID, INTENTS, AUDIT_WAIT_SECONDS
import db
import cache
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel
from music import music_manager, search_youtube, play_next

# Logging visible en consola
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# INTENTS
intents = discord.Intents.default()
intents.message_content = True
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
    logger.info(f"🎵 Canal de música configurado: {MUSIC_CHANNEL_ID}")
    db.init_db()


# --- NUEVO: DETECTOR DE DESCONEXIÓN FORZADA ---
@bot.event
async def on_voice_state_update(member, before, after):
    """
    Detecta si el bot ha sido desconectado manualmente del canal de voz
    y limpia su estado interno para evitar errores futuros.
    """
    # Solo nos importa si el que cambió de estado es el bot
    if member.id != bot.user.id:
        return

    # Si estaba en un canal (before) y ahora no está en ninguno (after es None)
    if before.channel is not None and after.channel is None:
        logger.warning(f"⚠️ El bot fue desconectado de {before.channel.name} en {member.guild.name}")

        # Limpiamos el reproductor de música de ese servidor
        music_manager.remove_player(member.guild.id)
        logger.info("🧹 Memoria del reproductor limpiada tras desconexión.")


# --- EVENTOS DE LOGS ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild: return

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

    cached = cache.get_cached(payload.message_id)
    content = cached[1] if cached else None
    author_id = cached[0] if cached else None

    if not content:
        rec = db.get_message(payload.message_id)
        if rec:
            content = rec['content']
            author_id = rec['author_id']

    if not content: return

    await asyncio.sleep(AUDIT_WAIT_SECONDS)

    try:
        guild = bot.get_guild(payload.guild_id)
        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel: return

        entry = await find_audit_entry_for_channel(guild, payload.channel_id)
        executor = entry.user if entry else None

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
    except Exception as e:
        logger.error(f"Error enviando log: {e}")


# --- COMANDOS MÚSICA ---

def check_music_channel(interaction: discord.Interaction) -> bool:
    if not MUSIC_CHANNEL_ID:
        return True
    return interaction.channel_id == MUSIC_CHANNEL_ID


@bot.tree.command(name="play", description="Reproduce música")
async def play(interaction: discord.Interaction, busqueda: str):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Comandos de música solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True
        )

    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    await interaction.response.defer()

    song = await search_youtube(busqueda)
    if not song:
        return await interaction.followup.send("❌ No encontré esa canción.")

    guild = interaction.guild
    voice_channel = interaction.user.voice.channel
    player = music_manager.get_player(guild)

    vc = guild.voice_client
    try:
        if not vc:
            vc = await voice_channel.connect(self_deaf=True)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    except Exception as e:
        logger.error(f"Error conectando: {e}")
        return await interaction.followup.send("❌ Error de conexión.")

    song.requester = interaction.user
    player.add_song(song)

    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        await interaction.followup.send(f"▶️ Reproduciendo: **{song.title}**")
    else:
        await interaction.followup.send(f"📝 Añadido a la cola: **{song.title}**")


@bot.tree.command(name="stop", description="Desconectar")
async def stop(interaction: discord.Interaction):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Comandos de música solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True
        )

    if interaction.guild.voice_client:
        # La limpieza se hace automáticamente por el evento on_voice_state_update,
        # pero forzarla aquí también no hace daño.
        music_manager.remove_player(interaction.guild.id)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Adiós")
    else:
        await interaction.response.send_message("❌ No estoy conectado", ephemeral=True)


@bot.tree.command(name="skip", description="Saltar canción")
async def skip(interaction: discord.Interaction):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(
            f"❌ Comandos de música solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True
        )

    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Saltada")
    else:
        await interaction.response.send_message("❌ No hay nada sonando", ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN)