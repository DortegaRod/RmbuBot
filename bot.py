import discord
from discord.ext import commands
import asyncio
import yt_dlp
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, INTENTS
import db
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel

# --- CONFIGURACIÓN DE MÚSICA (MVP) ---
# Opciones para yt-dlp (formato audio y sin playlist)
YDL_OPTIONS = {'format': 'bestaudio', 'noplaylist': 'True'}
# Opciones para FFmpeg (reconexión para evitar cortes)
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
# -------------------------------------

# Configurar intents
intents = discord.Intents.default()
intents.guilds = INTENTS.get("guilds", True)
intents.messages = INTENTS.get("messages", True)
intents.message_content = INTENTS.get("message_content", True)
intents.members = INTENTS.get("members", True)

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"[READY] Conectado como {bot.user} (ID {bot.user.id})")
    db.init_db()
    print("[INFO] Base de datos inicializada")


@bot.event
async def on_message(message: discord.Message):
    # Ignorar mensajes del propio bot para evitar bucles
    if message.author == bot.user:
        return

    if message.guild:
        try:
            content = message.content or ""
            if not content and message.embeds:
                embed = message.embeds[0]
                content = (embed.title or "") + "\n" + (embed.description or "")
            author_id = message.author.id
            channel_id = message.channel.id
            # Guardar siempre en BD
            db.save_message(message.id, author_id, content, channel_id)
            print(f"[SAVE] Mensaje almacenado ID {message.id} Autor {author_id}")
        except Exception as e:
            print(f"[ERROR] No se pudo guardar mensaje {message.id}: {e}")

    # IMPORTANTE: Esto permite que los comandos (!play, !stop) funcionen
    await bot.process_commands(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    channel = guild.get_channel(payload.channel_id) if guild and payload.channel_id else None
    if not guild or not channel:
        return

    rec = db.get_message(payload.message_id)
    if not rec:
        return

    # Esperar para consultar audit logs
    await asyncio.sleep(1.2)
    entry = await find_audit_entry_for_channel(guild, channel.id)

    admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
    if not admin_channel:
        print(f"[ERROR] Canal admin no encontrado con ID {ADMIN_LOG_CHANNEL_ID}")
        return

    author_id = rec['author_id']
    content = rec['content']
    author_display = f"<@{author_id}>" if author_id else "Desconocido"

    if entry and getattr(entry, 'user', None):
        executor = entry.user
        executor_display = executor.mention if hasattr(executor, 'mention') else str(executor)

        if author_id and getattr(executor, 'id', None) == author_id:
            return  # Auto borrado

        channel_display = channel.mention
        try:
            await send_admin_embed(admin_channel,
                                   author_display=author_display,
                                   executor_display=executor_display,
                                   channel_display=channel_display,
                                   content=content,
                                   message_id=payload.message_id)
            print(f"[SEND] Notificación enviada para mensaje {payload.message_id}")
        except Exception as e:
            print(f"[ERROR] Error enviando notificación: {e}")


# --- COMANDOS DE MÚSICA ---

@bot.command()
async def play(ctx, *, search: str):
    """Reproduce música de YouTube. Uso: !play <url o búsqueda>"""
    # 1. Verificar si el usuario está en un canal de voz
    if not ctx.author.voice:
        await ctx.send("❌ ¡Debes estar en un canal de voz primero!")
        return

    voice_channel = ctx.author.voice.channel

    # 2. Conectar el bot al canal (o moverlo si ya está en otro)
    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    vc = ctx.voice_client

    # 3. Detener si ya hay algo sonando
    if vc.is_playing():
        vc.stop()

    await ctx.send(f"🔎 Buscando: `{search}`...")

    # 4. Extraer URL de audio con yt-dlp
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            # 'ytsearch:' permite buscar texto si no es un link directo
            info = ydl.extract_info(f"ytsearch:{search}", download=False)
            if 'entries' in info:
                info = info['entries'][0]

            url2 = info['url']
            title = info.get('title', 'Audio desconocido')

            # 5. Reproducir usando FFmpeg
            source = discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS)
            vc.play(source)
            await ctx.send(f"🎶 Reproduciendo: **{title}**")

    except Exception as e:
        await ctx.send(f"❌ Error al reproducir: {e}")
        print(e)


@bot.command()
async def stop(ctx):
    """Detiene la música y desconecta al bot."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Música detenida y desconectado.")
    else:
        await ctx.send("❌ No estoy conectado a ningún canal de voz.")


if __name__ == '__main__':
    print("[INIT] Iniciando bot...")
    bot.run(TOKEN)