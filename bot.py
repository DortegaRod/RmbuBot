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
from music import music_manager, search_youtube, play_next

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Diccionario para gestionar los timers de desconexión por canal vacío
# guild_id -> task
empty_channel_timers = {}

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
    db.init_db()


# --- DETECTOR DE ESTADO DE VOZ (MEJORADO) ---
@bot.event
async def on_voice_state_update(member, before, after):
    guild_id = member.guild.id
    vc = member.guild.voice_client

    # 1. Si el bot es desconectado manualmente
    if member.id == bot.user.id and after.channel is None:
        logger.warning(f"⚠️ Bot desconectado manualmente en {member.guild.name}")
        if guild_id in empty_channel_timers:
            empty_channel_timers[guild_id].cancel()
            del empty_channel_timers[guild_id]
        music_manager.remove_player(guild_id)
        return

    # 2. Lógica de "Canal Vacío"
    if vc and vc.channel:
        # Contamos cuántos humanos hay (excluyendo bots)
        human_members = [m for m in vc.channel.members if not m.bot]

        if len(human_members) == 0:
            # Si no hay humanos y no hay un timer ya corriendo, lo iniciamos
            if guild_id not in empty_channel_timers:
                logger.info(f"🔇 Canal vacío en {vc.channel.name}. Iniciando cuenta atrás de {INACTIVITY_TIMEOUT}s")
                empty_channel_timers[guild_id] = asyncio.create_task(
                    disconnect_if_empty(member.guild, vc)
                )
        else:
            # Si alguien entra (hay humanos) y había un timer, lo cancelamos
            if guild_id in empty_channel_timers:
                logger.info(f"👤 Alguien ha vuelto al canal en {vc.channel.name}. Timer cancelado.")
                empty_channel_timers[guild_id].cancel()
                del empty_channel_timers[guild_id]


async def disconnect_if_empty(guild, vc):
    """Tarea que espera el tiempo de inactividad y desconecta el bot."""
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        if vc and vc.is_connected():
            logger.info(f"⏰ Tiempo agotado. Desconectando de {vc.channel.name} por inactividad.")
            # Limpiamos música y desconectamos
            music_manager.remove_player(guild.id)
            await vc.disconnect()
    except asyncio.CancelledError:
        # El timer fue cancelado porque alguien entró
        pass
    finally:
        # Limpiar el diccionario al terminar
        if guild.id in empty_channel_timers:
            del empty_channel_timers[guild.id]


# --- EVENTOS DE LOGS ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild: return
    try:
        content = message.content or ("[Embed]" if message.embeds else "[Sin contenido]")
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
            content, author_id = rec['content'], rec['author_id']
    if not content: return

    await asyncio.sleep(AUDIT_WAIT_SECONDS)
    try:
        guild = bot.get_guild(payload.guild_id)
        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel: return
        entry = await find_audit_entry_for_channel(guild, payload.channel_id)
        executor = entry.user if entry else None
        if author_id and executor and executor.id == author_id: return

        await send_admin_embed(
            admin_channel,
            author_display=f"<@{author_id}>" if author_id else "Desconocido",
            executor_display=executor.mention if executor else "Desconocido",
            channel_display=guild.get_channel(payload.channel_id).mention,
            content=content,
            message_id=payload.message_id
        )
    except Exception as e:
        logger.error(f"Error enviando log: {e}")


# --- COMANDOS MÚSICA ---
def check_music_channel(interaction: discord.Interaction) -> bool:
    if not MUSIC_CHANNEL_ID: return True
    return interaction.channel_id == MUSIC_CHANNEL_ID


@bot.tree.command(name="play", description="Reproduce música")
async def play(interaction: discord.Interaction, busqueda: str):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
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
    except:
        return await interaction.followup.send("❌ Error de conexión.")

    song.requester = interaction.user
    player.add_song(song)

    is_playing_now = False
    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        is_playing_now = True

    embed = discord.Embed(
        title="🎶 Reproduciendo ahora" if is_playing_now else "📝 Añadido a la cola",
        description=f"**[{song.title}]({song.webpage_url})**",
        color=discord.Color.green() if is_playing_now else discord.Color.blue()
    )
    if song.thumbnail: embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text=f"Pedido por {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="stop", description="Desconectar")
async def stop(interaction: discord.Interaction):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    if interaction.guild.voice_client:
        music_manager.remove_player(interaction.guild.id)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Adiós")
    else:
        await interaction.response.send_message("❌ No estoy conectado", ephemeral=True)


@bot.tree.command(name="skip", description="Saltar canción")
async def skip(interaction: discord.Interaction):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Saltada")
    else:
        await interaction.response.send_message("❌ No hay nada sonando", ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN)