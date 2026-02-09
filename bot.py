import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import yt_dlp
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, INTENTS
import db
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel

# --- CONFIGURACIÓN DE MÚSICA ---
YDL_OPTIONS = {'format': 'bestaudio', 'noplaylist': 'True'}
# Usamos 'executable' si subiste el archivo ffmpeg manual, si no, quítalo.
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# Configurar intents
intents = discord.Intents.default()
intents.guilds = INTENTS.get("guilds", True)
intents.messages = INTENTS.get("messages", True)
intents.message_content = INTENTS.get("message_content", True)
intents.members = INTENTS.get("members", True)


# Definimos el bot (quitamos command_prefix porque usaremos /)
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        # Esto sincroniza los comandos / con Discord al iniciar
        await self.tree.sync()
        print("[SYNC] Comandos de barra sincronizados")


bot = MyBot()


@bot.event
async def on_ready():
    print(f"[READY] Conectado como {bot.user} (ID {bot.user.id})")
    db.init_db()
    print("[INFO] Base de datos inicializada")


# --- COMANDOS DE BARRA (SLASH COMMANDS) ---

@bot.tree.command(name="play", description="Reproduce música de YouTube")
@app_commands.describe(busqueda="Nombre de la canción o enlace de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    # 1. Validar voz
    if not interaction.user.voice:
        await interaction.response.send_message("❌ ¡Entra a un canal de voz primero!", ephemeral=True)
        return

    # IMPORTANTE: Deferimos la respuesta porque buscar en YT tarda más de 3 segundos
    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild

    # 2. Conectar
    if guild.voice_client is None:
        await voice_channel.connect()
    elif guild.voice_client.channel != voice_channel:
        await guild.voice_client.move_to(voice_channel)

    vc = guild.voice_client
    if vc.is_playing():
        vc.stop()

    # 3. Buscar y Reproducir
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"ytsearch:{busqueda}", download=False)
            if 'entries' in info:
                info = info['entries'][0]

            url2 = info['url']
            title = info.get('title', 'Audio desconocido')

            # Ajusta executable="./ffmpeg" si subiste el archivo manual a SparkedHost
            source = discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS)
            vc.play(source)

            # Usamos followup porque ya usamos defer() arriba
            await interaction.followup.send(f"🎶 Reproduciendo: **{title}**")

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        print(e)


@bot.tree.command(name="stop", description="Detiene la música y desconecta")
async def stop(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Música detenida.")
    else:
        await interaction.response.send_message("❌ No estoy conectado.", ephemeral=True)


# --- SISTEMA DE LOGS Y AUDIT (TU CÓDIGO ANTERIOR) ---

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user: return
    if message.guild:
        try:
            content = message.content or ""
            if not content and message.embeds:
                embed = message.embeds[0]
                content = (embed.title or "") + "\n" + (embed.description or "")
            db.save_message(message.id, message.author.id, content, message.channel.id)
        except Exception as e:
            print(f"[ERROR] DB Save: {e}")


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    channel = guild.get_channel(payload.channel_id)
    rec = db.get_message(payload.message_id)

    if not rec or not channel: return

    await asyncio.sleep(1.2)
    entry = await find_audit_entry_for_channel(guild, channel.id)
    admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)

    if not admin_channel: return

    author_id = rec['author_id']
    content = rec['content']
    author_display = f"<@{author_id}>"

    if entry and entry.user:
        executor = entry.user
        if author_id and executor.id == author_id: return
        try:
            await send_admin_embed(admin_channel,
                                   author_display=author_display,
                                   executor_display=executor.mention,
                                   channel_display=channel.mention,
                                   content=content,
                                   message_id=payload.message_id)
        except Exception:
            pass


if __name__ == '__main__':
    bot.run(TOKEN)