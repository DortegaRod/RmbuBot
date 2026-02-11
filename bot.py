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


@bot.event
async def on_voice_state_update(member, before, after):
    guild_id = member.guild.id
    vc = member.guild.voice_client

    if member.id == bot.user.id and after.channel is None:
        if guild_id in empty_channel_timers:
            empty_channel_timers[guild_id].cancel()
            del empty_channel_timers[guild_id]
        music_manager.remove_player(guild_id)
        return

    if vc and vc.channel:
        human_members = [m for m in vc.channel.members if not m.bot]

        if len(human_members) == 0:
            if guild_id not in empty_channel_timers:
                empty_channel_timers[guild_id] = asyncio.create_task(disconnect_if_empty(member.guild, vc))
        else:
            if guild_id in empty_channel_timers:
                empty_channel_timers[guild_id].cancel()
                del empty_channel_timers[guild_id]


async def disconnect_if_empty(guild, vc):
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        if vc and vc.is_connected():
            music_manager.remove_player(guild.id)
            await vc.disconnect()
    except asyncio.CancelledError:
        pass
    finally:
        if guild.id in empty_channel_timers:
            del empty_channel_timers[guild.id]


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
        if rec: content, author_id = rec['content'], rec['author_id']
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


def check_music_channel(interaction: discord.Interaction) -> bool:
    return not MUSIC_CHANNEL_ID or interaction.channel_id == MUSIC_CHANNEL_ID


@bot.tree.command(name="play", description="Reproduce música o playlists de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    await interaction.response.defer()

    songs = await search_youtube(busqueda)
    if not songs:
        return await interaction.followup.send("❌ No encontré resultados.")

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
        return await interaction.followup.send(f"❌ Error conexión: {e}")

    for s in songs:
        s.requester = interaction.user
        player.add_song(s)

    is_playing_now = False
    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        is_playing_now = True

    if len(songs) > 1:
        embed = discord.Embed(title="📂 Playlist Añadida",
                              description=f"Se han añadido **{len(songs)}** canciones a la cola.",
                              color=discord.Color.purple())
    else:
        s = songs[0]
        embed = discord.Embed(
            title="🎶 Reproduciendo" if is_playing_now else "📝 En cola",
            description=f"**[{s.title}]({s.webpage_url})**",
            color=discord.Color.green() if is_playing_now else discord.Color.blue()
        )
        if s.thumbnail: embed.set_thumbnail(url=s.thumbnail)

    embed.set_footer(text=f"Pedido por {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="loop", description="Configura el modo de repetición")
@app_commands.choices(modo=[
    app_commands.Choice(name