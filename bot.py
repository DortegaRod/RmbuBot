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

# 1. CONFIGURAR LOGGING E INSTANCIA (Debe ir arriba para evitar NameError)
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


bot = MusicBot()


# 2. EVENTOS
@bot.event
async def on_ready():
    logger.info(f"✅ Bot conectado como {bot.user}")
    db.init_db()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id and after.channel is None:
        music_manager.remove_player(member.guild.id)


# --- COMANDOS MÚSICA ---
def check_music_channel(interaction: discord.Interaction) -> bool:
    return not MUSIC_CHANNEL_ID or interaction.channel_id == MUSIC_CHANNEL_ID


@bot.tree.command(name="play", description="Reproduce música o playlists de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    await interaction.response.defer()

    # Buscar canciones (devuelve una lista)
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
        return await interaction.followup.send(f"❌ Error de conexión: {e}")

    # Añadir todas las canciones encontradas a la cola
    for s in songs:
        s.requester = interaction.user
        player.add_song(s)

    is_playing_now = False
    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        is_playing_now = True

    # Embed informativo
    if len(songs) > 1:
        embed = discord.Embed(title="📂 Playlist Añadida", description=f"Se han añadido **{len(songs)}** canciones.",
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


@bot.tree.command(name="skip", description="Salta la canción")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Saltada")
    else:
        await interaction.response.send_message("❌ Nada sonando.", ephemeral=True)


# (Incluye aquí el resto de tus eventos de logs y comandos como stop, etc.)

if __name__ == '__main__':
    bot.run(TOKEN)