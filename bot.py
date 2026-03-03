"""
Módulo Principal del Bot de Discord.
Maneja la conexión con la API de Discord, enrutamiento de comandos (Slash Commands),
y eventos de auditoría (logs de mensajes borrados).
"""

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
from music import (
    music_manager, search_youtube, play_next,
    LOOP_OFF, LOOP_CURRENT, LOOP_QUEUE, Song
)

# --- CONFIGURACIÓN DE RADIOS ---
# Diccionario con nombres y URLs directas de streaming de las emisoras
# --- LISTA DE RADIOS ESPAÑOLAS (URLs Oficiales Actualizadas) ---
RADIOS_ES = {
    "los40":       ("Los 40 Principales", "https://playerservices.streamtheworld.com/api/livestream-redirect/Los40.mp3"),
    "cadena100":   ("Cadena 100",         "https://flucast09-h-cloud.flumotion.com/cope/cadena100.mp3"),
    "europafm":    ("Europa FM",          "https://icecast-streaming.nice264.com/europafm"),
    "rockfm":      ("Rock FM",            "https://flucast09-h-cloud.flumotion.com/cope/rockfm.mp3"),
    "kissfm":      ("Kiss FM",            "http://kissfm.kissfmradio.cires21.com/kissfm.mp3"),
    "cadenaser":   ("Cadena SER",         "https://playerservices.streamtheworld.com/api/livestream-redirect/CADENASER.mp3"),
    "cope":        ("COPE",               "https://flucast09-h-cloud.flumotion.com/cope/net1.mp3"),
    "ondacero":    ("Onda Cero",          "https://icecast-streaming.nice264.com/ondacero"),
    "hitfm":       ("Hit FM",             "http://hitfm.kissfmradio.cires21.com/hitfm.mp3"),
    "radiola":     ("Radiolé",            "https://playerservices.streamtheworld.com/api/livestream-redirect/RADIOLE.mp3"),
    "los40urban":  ("Los 40 Urban",       "https://playerservices.streamtheworld.com/api/livestream-redirect/LOS40_URBAN.mp3"),
    "locafm":      ("Loca FM",            "http://audio-online.net:2300/live"),
    "ibizaglobal": ("Ibiza Global Radio", "http://ibizaglobalradio.streaming-pro.com:8024"),
}

# --- INICIALIZACIÓN ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True


class MusicBot(commands.Bot):
    """Instancia principal del bot heredando de commands.Bot"""

    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        # Sincroniza los comandos de barra (Slash Commands) con Discord al iniciar
        await self.tree.sync()


bot = MusicBot()


# ==========================================
# EVENTOS DEL SISTEMA Y LOGS
# ==========================================

@bot.event
async def on_ready():
    """Se dispara cuando el bot establece conexión exitosa con los servidores de Discord."""
    logger.info(f"✅ Bot conectado como {bot.user}")
    db.init_db()


@bot.event
async def on_voice_state_update(member, before, after):
    """Monitoriza cambios de estado en canales de voz (conexiones, desconexiones, muteos)."""
    guild_id = member.guild.id
    vc = member.guild.voice_client

    # Limpieza de memoria si el bot es expulsado/desconectado manualmente
    if member.id == bot.user.id and after.channel is None:
        music_manager.remove_player(guild_id)
        return

    # Opcional: Implementar lógica de abandono por canal vacío aquí


@bot.event
async def on_message(message: discord.Message):
    """Captura mensajes nuevos para alimentar la base de datos y la caché de logs."""
    if message.author.bot or not message.guild: return
    try:
        content = message.content or ("[Embed]" if message.embeds else "[Sin contenido]")
        db.save_message(message.id, message.author.id, content, message.channel.id)
        cache.cache_message(message.id, message.author.id, content)
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Captura el evento crudo de eliminación de un mensaje y procesa su auditoría."""
    if not payload.guild_id: return

    # Intenta recuperar de memoria volátil (Caché), si falla, acude a SQLite (DB)
    cached = cache.get_cached(payload.message_id)
    content = cached[1] if cached else None
    author_id = cached[0] if cached else None

    if not content:
        rec = db.get_message(payload.message_id)
        if rec: content, author_id = rec['content'], rec['author_id']
    if not content: return

    # Espera preventiva para dar tiempo a los servidores de Discord a registrar el Audit Log
    await asyncio.sleep(AUDIT_WAIT_SECONDS)
    try:
        guild = bot.get_guild(payload.guild_id)
        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel: return

        # Consultar quién borró el mensaje (Requiere permisos 'View Audit Log')
        entry = await find_audit_entry_for_channel(guild, payload.channel_id)
        executor = entry.user if entry else None

        # Ignorar si el usuario borró su propio mensaje
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


# ==========================================
# COMANDOS DE REPRODUCCIÓN (SLASH COMMANDS)
# ==========================================

def check_music_channel(interaction: discord.Interaction) -> bool:
    """Verifica si la interacción ocurre en el canal de comandos de música autorizado."""
    return not MUSIC_CHANNEL_ID or interaction.channel_id == MUSIC_CHANNEL_ID


@bot.tree.command(name="play", description="Reproduce música o playlists desde YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    """Busca en YouTube y añade una canción o playlist a la cola del servidor."""
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
        # Lógica de conexión y movimiento de canal
        if not vc:
            vc = await voice_channel.connect(self_deaf=True)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    except Exception as e:
        return await interaction.followup.send(f"❌ Error de conexión: {e}")

    # Inyección de metadatos del solicitante
    for s in songs:
        s.requester = interaction.user
        player.add_song(s)

    # Iniciar motor si estaba en reposo
    is_playing_now = False
    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        is_playing_now = True

    # Generación de UI (Embed)
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


@bot.tree.command(name="radio", description="Sintoniza una radio española en directo")
@app_commands.choices(emisora=[
    app_commands.Choice(name="📻 Los 40 Principales", value="los40"),
    app_commands.Choice(name="📻 Los 40 Urban", value="los40urban"),
    app_commands.Choice(name="🎸 Rock FM", value="rockfm"),
    app_commands.Choice(name="💯 Cadena 100", value="cadena100"),
    app_commands.Choice(name="🌍 Europa FM", value="europafm"),
    app_commands.Choice(name="💋 Kiss FM", value="kissfm"),
    app_commands.Choice(name="🗣️ Cadena SER", value="cadenaser"),
    app_commands.Choice(name="🗣️ COPE", value="cope"),
    app_commands.Choice(name="🗣️ Onda Cero", value="ondacero"),
    app_commands.Choice(name="💃 Radiolé", value="radiola"),
    app_commands.Choice(name="🤪 Loca FM", value="locafm"),
    app_commands.Choice(name="🏝️ Ibiza Global", value="ibizaglobal")
])
async def radio(interaction: discord.Interaction, emisora: app_commands.Choice[str]):
    """Se conecta directamente a un flujo (stream) HTTP de una emisora de radio."""
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    await interaction.response.defer()

    # Extraemos los datos del diccionario configurado arriba
    nombre_radio, stream_url = RADIOS_ES[emisora.value]

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

    # Instanciamos el objeto Song definiendo `stream_url`.
    # Al tener esto, music.py se salta yt-dlp y conecta directamente a la IP de la emisora.
    radio_song = Song(
        title=f"🔴 {nombre_radio} (En Directo)",
        webpage_url=stream_url,
        thumbnail="https://i.imgur.com/QzpbK1o.png",  # Icono genérico de radio
        stream_url=stream_url,
        requester=interaction.user
    )

    player.add_song(radio_song)

    if not vc.is_playing() and not player.current:
        await play_next(vc, player)

    embed = discord.Embed(
        title="📻 Sintonizando Radio",
        description=f"**{radio_song.title}**",
        color=discord.Color.gold()
    )
    embed.set_footer(text="Emisión en directo 24/7", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="loop", description="Configura el modo de repetición (Bucle)")
@app_commands.choices(modo=[
    app_commands.Choice(name="⛔ Desactivado", value=0),
    app_commands.Choice(name="🔂 Canción Actual", value=1),
    app_commands.Choice(name="🔁 Toda la Cola", value=2)
])
async def loop(interaction: discord.Interaction, modo: app_commands.Choice[int]):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)

    player = music_manager.get_player(interaction.guild)
    player.loop_mode = modo.value

    msgs = {0: "Modo bucle **desactivado**.", 1: "🔂 Bucle: **Canción Actual**.", 2: "🔁 Bucle: **Toda la Cola**."}
    await interaction.response.send_message(msgs[modo.value])


@bot.tree.command(name="shuffle", description="Mezcla de forma aleatoria la cola de reproducción")
async def shuffle(interaction: discord.Interaction):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)

    player = music_manager.get_player(interaction.guild)
    if len(player.queue) < 2:
        return await interaction.response.send_message("❌ Necesitas al menos 2 canciones en la cola.", ephemeral=True)

    player.shuffle_queue()
    await interaction.response.send_message("🔀 **Cola mezclada** aleatoriamente.")


@bot.tree.command(name="skip", description="Termina la pista actual y pasa a la siguiente")
async def skip(interaction: discord.Interaction):
    if not check_music_channel(interaction): return await interaction.response.send_message(
        f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()  # Al detener el reproductor, se dispara automáticamente el evento 'after'
        await interaction.response.send_message("⏭️ Pista saltada.")
    else:
        await interaction.response.send_message("❌ Nada sonando.", ephemeral=True)


@bot.tree.command(name="stop", description="Detiene la música, limpia la cola y expulsa al bot")
async def stop(interaction: discord.Interaction):
    if not check_music_channel(interaction): return await interaction.response.send_message(
        f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    if interaction.guild.voice_client:
        music_manager.remove_player(interaction.guild.id)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Adiós.")
    else:
        await interaction.response.send_message("❌ No estoy conectado.", ephemeral=True)


@bot.tree.command(name="queue", description="Muestra las próximas 10 canciones en la cola")
async def queue(interaction: discord.Interaction):
    player = music_manager.get_player(interaction.guild)
    if not player.queue and not player.current:
        return await interaction.response.send_message("📭 La cola está vacía.")

    desc = ""
    if player.current:
        desc += f"▶️ **Sonando ahora:**\n[{player.current.title}]({player.current.webpage_url})\n\n"

    for i, s in enumerate(list(player.queue)[:10], 1):
        desc += f"`{i}.` {s.title}\n"

    if len(player.queue) > 10:
        desc += f"\n*...y {len(player.queue) - 10} más*"

    modes = {0: "Off", 1: "🔂 Canción", 2: "🔁 Cola"}
    embed = discord.Embed(title="🎵 Cola de Reproducción", description=desc, color=discord.Color.blue())
    embed.set_footer(text=f"Modo Bucle: {modes[player.loop_mode]} | Total: {len(player.queue)}")

    await interaction.response.send_message(embed=embed)


if __name__ == '__main__':
    # Arranca el cliente de Discord escuchando el Gateway y enrutando eventos
    bot.run(TOKEN)