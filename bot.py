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
from typing import Optional
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, AUDIT_WAIT_SECONDS
import db
import cache
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel
from music import (
    music_manager, search_youtube, play_next, empty_channel_disconnect,
    LOOP_OFF, LOOP_CURRENT, LOOP_QUEUE, Song
)

# --- CONFIGURACIÓN DE RADIOS ---
# Diccionario con nombres y URLs directas de streaming de las emisoras
# --- LISTA DE RADIOS ESPAÑOLAS (URLs Oficiales Actualizadas) ---
RADIOS_ES = {
    "los40":       ("Los 40 Principales", "https://playerservices.streamtheworld.com/api/livestream-redirect/Los40.mp3"),
    "cadena100":   ("Cadena 100",         "https://flucast09-h-cloud.flumotion.com/cope/cadena100.mp3"),
    "europafm":    ("Europa FM",          "https://radio-atres-live.ondacero.es/api/livestream-redirect/EFMAAC.aac"),
    "rockfm":      ("Rock FM",            "https://flucast09-h-cloud.flumotion.com/cope/rockfm.mp3"),
    "kissfm":      ("Kiss FM",            "http://kissfm.kissfmradio.cires21.com/kissfm.mp3"),
    "cadenaser":   ("Cadena SER",         "https://playerservices.streamtheworld.com/api/livestream-redirect/CADENASER.mp3"),
    "cope":        ("COPE",               "https://flucast09-h-cloud.flumotion.com/cope/net1.mp3"),
    "ondacero":    ("Onda Cero",          "https://radio-atres-live.ondacero.es/api/livestream-redirect/OCAAC.aac"),
    "hitfm":       ("Hit FM",             "http://hitfm.kissfmradio.cires21.com/hitfm.mp3"),
    "radiola":     ("Radiolé",            "https://playerservices.streamtheworld.com/api/livestream-redirect/RADIOLE.mp3"),
    "los40urban":  ("Los 40 Urban",       "https://playerservices.streamtheworld.com/api/livestream-redirect/LOS40_URBAN.mp3"),
    "locafm":      ("Loca FM",            "https://s3.we4stream.com:2020/stream/locafm"),
    "ibizaglobal": ("Ibiza Global Radio", "http://ibizaglobalradio.streaming-pro.com:8024"),
}

# --- INICIALIZACIÓN ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

# Servidor donde SÍ se guardan mensajes y se registran los borrados.
# Se autodetecta al arrancar como el servidor que contiene ADMIN_LOG_CHANNEL_ID.
log_guild_id: Optional[int] = None


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
    global log_guild_id
    logger.info(f"✅ Bot conectado como {bot.user}")
    db.init_db()

    # Autodetección del servidor de auditoría a partir del canal de logs.
    admin_channel = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
    if admin_channel and admin_channel.guild:
        log_guild_id = admin_channel.guild.id
        logger.info(f"📓 Auditoría de mensajes activa SOLO en: {admin_channel.guild.name} ({log_guild_id})")
    else:
        logger.warning("⚠️ No se encontró el canal admin; la auditoría de mensajes está desactivada. "
                       "Revisa ADMIN_LOG_CHANNEL_ID y que el bot esté en ese servidor.")


@bot.event
async def on_voice_state_update(member, before, after):
    """Monitoriza cambios de estado en canales de voz (conexiones, desconexiones)."""
    guild = member.guild

    # Limpieza de memoria si el bot es expulsado/desconectado manualmente
    if member.id == bot.user.id and after.channel is None:
        music_manager.remove_player(guild.id)
        return

    vc = guild.voice_client
    if not vc or not vc.channel:
        return

    # Auto-salida: si el canal de voz del bot se queda sin humanos, programar desconexión.
    player = music_manager.get_player(guild)
    humans = [m for m in vc.channel.members if not m.bot]

    if not humans:
        if not player.empty_task:
            player.empty_task = asyncio.create_task(empty_channel_disconnect(vc, player))
    else:
        # Volvió alguien: cancelar la salida programada
        if player.empty_task:
            player.empty_task.cancel()
            player.empty_task = None


@bot.event
async def on_message(message: discord.Message):
    """Captura mensajes nuevos para alimentar la base de datos y la caché de logs."""
    if message.author.bot or not message.guild:
        return
    # Solo auditamos el servidor configurado
    if log_guild_id is None or message.guild.id != log_guild_id:
        return
    try:
        content = message.content or ("[Embed]" if message.embeds else "[Sin contenido]")
        # Guardado en un hilo aparte para no bloquear el event loop del bot
        await asyncio.to_thread(db.save_message, message.id, message.author.id, content, message.channel.id)
        cache.cache_message(message.id, message.author.id, content)
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Captura el evento crudo de eliminación de un mensaje y procesa su auditoría."""
    if not payload.guild_id:
        return
    # Solo auditamos el servidor configurado
    if log_guild_id is None or payload.guild_id != log_guild_id:
        return

    # Intenta recuperar de memoria volátil (Caché), si falla, acude a SQLite (DB)
    cached = cache.get_cached(payload.message_id)
    content = cached[1] if cached else None
    author_id = cached[0] if cached else None

    if not content:
        rec = await asyncio.to_thread(db.get_message, payload.message_id)
        if rec:
            content, author_id = rec['content'], rec['author_id']
    if not content:
        return

    # Espera preventiva para dar tiempo a los servidores de Discord a registrar el Audit Log
    await asyncio.sleep(AUDIT_WAIT_SECONDS)
    try:
        guild = bot.get_guild(payload.guild_id)
        admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not admin_channel:
            return

        # Consultar quién borró el mensaje (Requiere permisos 'View Audit Log')
        entry = await find_audit_entry_for_channel(guild, payload.channel_id)
        executor = entry.user if entry else None

        # Ignorar si el usuario borró su propio mensaje
        if author_id and executor and executor.id == author_id:
            return

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
# UTILIDADES DE COMANDOS
# ==========================================

def _fmt_time(seconds: float) -> str:
    """Formatea segundos como M:SS o H:MM:SS."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _progress_bar(elapsed: float, total: float, length: int = 18) -> str:
    """Devuelve una barra tipo ─────🔘──────── según el progreso."""
    if not total or total <= 0:
        return ""
    frac = max(0.0, min(1.0, elapsed / total))
    pos = int(frac * (length - 1))
    return "─" * pos + "🔘" + "─" * (length - 1 - pos)


async def deny_wrong_channel(interaction: discord.Interaction) -> bool:
    """
    Comprueba si la interacción ocurre en el canal de comandos configurado con /setup.
    Si NO está permitido, responde con un aviso efímero y devuelve True (denegar).
    Si el servidor no ha hecho /setup, se permite en cualquier canal.
    """
    if interaction.guild_id is None:
        return False
    configured = db.get_music_channel(interaction.guild_id)
    if configured is not None and interaction.channel_id != configured:
        await interaction.response.send_message(
            f"❌ Usa los comandos del bot en <#{configured}>.", ephemeral=True
        )
        return True
    return False


# ==========================================
# COMANDO DE CONFIGURACIÓN
# ==========================================

@bot.tree.command(name="setup", description="Fija el canal donde el bot escuchará los comandos (solo admins)")
@app_commands.describe(canal="Canal de texto donde se usarán los comandos del bot")
async def setup(interaction: discord.Interaction, canal: discord.TextChannel):
    """Configura, por servidor, el canal de comandos autorizado. Persiste en la base de datos."""
    if interaction.guild is None:
        return await interaction.response.send_message("❌ Este comando solo funciona en un servidor.", ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(
            "❌ Necesitas el permiso **Gestionar servidor** para usar `/setup`.", ephemeral=True
        )

    ok = db.set_music_channel(interaction.guild_id, canal.id)
    if ok:
        await interaction.response.send_message(
            f"✅ Listo. A partir de ahora los comandos del bot se usan en {canal.mention}.\n"
            f"*(Vuelve a ejecutar `/setup` para cambiarlo.)*"
        )
    else:
        await interaction.response.send_message("❌ No pude guardar la configuración. Revisa los logs.", ephemeral=True)


# ==========================================
# COMANDOS DE REPRODUCCIÓN (SLASH COMMANDS)
# ==========================================

@bot.tree.command(name="play", description="Reproduce música o playlists desde YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    """Busca en YouTube y añade una canción o playlist a la cola del servidor."""
    if await deny_wrong_channel(interaction):
        return
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
    if await deny_wrong_channel(interaction):
        return
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

    # Instanciamos el objeto Song definiendo `stream_url` y marcándolo como radio.
    # Al tener esto, music.py se salta yt-dlp y conecta directamente a la IP de la emisora.
    radio_song = Song(
        title=f"🔴 {nombre_radio} (En Directo)",
        webpage_url=stream_url,
        thumbnail="https://i.imgur.com/QzpbK1o.png",  # Icono genérico de radio
        stream_url=stream_url,
        requester=interaction.user,
        is_radio=True
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


@bot.tree.command(name="current", description="Muestra la canción que suena ahora con barra de progreso")
async def current(interaction: discord.Interaction):
    """Muestra la pista/emisora en reproducción con su progreso (o 'EN DIRECTO' en radios)."""
    player = music_manager.get_player(interaction.guild)
    vc = interaction.guild.voice_client
    if not player.current:
        return await interaction.response.send_message("🔇 No hay nada sonando ahora mismo.", ephemeral=True)

    s = player.current
    embed = discord.Embed(
        title="▶️ Sonando ahora",
        description=f"**[{s.title}]({s.webpage_url})**",
        color=discord.Color.green()
    )
    if s.thumbnail:
        embed.set_thumbnail(url=s.thumbnail)

    # Línea de progreso: barra + tiempos para canciones; "EN DIRECTO" para radios
    paused = bool(vc and vc.is_paused())
    if s.is_radio or not s.duration:
        embed.add_field(name="​", value="🔴 **EN DIRECTO**", inline=False)
    else:
        elapsed = min(player.get_elapsed(), s.duration)
        bar = _progress_bar(elapsed, s.duration)
        icon = "⏸️" if paused else "▶️"
        embed.add_field(
            name="​",
            value=f"{icon} `{_fmt_time(elapsed)}` {bar} `{_fmt_time(s.duration)}`",
            inline=False
        )

    modes = {LOOP_OFF: "Off", LOOP_CURRENT: "🔂 Canción", LOOP_QUEUE: "🔁 Cola"}
    footer = f"Volumen: {int(player.volume * 100)}% | Bucle: {modes[player.loop_mode]} | En cola: {len(player.queue)}"
    if s.requester:
        footer = f"Pedido por {s.requester.display_name} • {footer}"
    embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="loop", description="Configura el modo de repetición (Bucle)")
@app_commands.choices(modo=[
    app_commands.Choice(name="⛔ Desactivado", value=0),
    app_commands.Choice(name="🔂 Canción Actual", value=1),
    app_commands.Choice(name="🔁 Toda la Cola", value=2)
])
async def loop(interaction: discord.Interaction, modo: app_commands.Choice[int]):
    if await deny_wrong_channel(interaction):
        return

    player = music_manager.get_player(interaction.guild)
    player.loop_mode = modo.value

    msgs = {0: "Modo bucle **desactivado**.", 1: "🔂 Bucle: **Canción Actual**.", 2: "🔁 Bucle: **Toda la Cola**."}
    await interaction.response.send_message(msgs[modo.value])


@bot.tree.command(name="shuffle", description="Mezcla de forma aleatoria la cola de reproducción")
async def shuffle(interaction: discord.Interaction):
    if await deny_wrong_channel(interaction):
        return

    player = music_manager.get_player(interaction.guild)
    if len(player.queue) < 2:
        return await interaction.response.send_message("❌ Necesitas al menos 2 canciones en la cola.", ephemeral=True)

    player.shuffle_queue()
    await interaction.response.send_message("🔀 **Cola mezclada** aleatoriamente.")


@bot.tree.command(name="skip", description="Termina la pista actual y pasa a la siguiente")
async def skip(interaction: discord.Interaction):
    if await deny_wrong_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # Al detener el reproductor, se dispara automáticamente el evento 'after'
        await interaction.response.send_message("⏭️ Pista saltada.")
    else:
        await interaction.response.send_message("❌ Nada sonando.", ephemeral=True)


@bot.tree.command(name="pause", description="Pausa la reproducción actual")
async def pause(interaction: discord.Interaction):
    if await deny_wrong_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        music_manager.get_player(interaction.guild).mark_paused()
        await interaction.response.send_message("⏸️ Reproducción **pausada**. Usa `/resume` para continuar.")
    else:
        await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)


@bot.tree.command(name="resume", description="Reanuda la reproducción pausada")
async def resume(interaction: discord.Interaction):
    if await deny_wrong_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        music_manager.get_player(interaction.guild).mark_resumed()
        await interaction.response.send_message("▶️ Reproducción **reanudada**.")
    else:
        await interaction.response.send_message("❌ No hay nada pausado.", ephemeral=True)


@bot.tree.command(name="remove", description="Quita una canción de la cola por su número (mira /queue)")
@app_commands.describe(posicion="Número de la canción en la cola")
async def remove(interaction: discord.Interaction, posicion: int):
    if await deny_wrong_channel(interaction):
        return
    player = music_manager.get_player(interaction.guild)
    if not player.queue:
        return await interaction.response.send_message("📭 La cola está vacía.", ephemeral=True)
    if posicion < 1 or posicion > len(player.queue):
        return await interaction.response.send_message(
            f"❌ Número inválido. La cola tiene **{len(player.queue)}** canciones.", ephemeral=True
        )

    removed = player.remove_at(posicion - 1)
    if removed:
        await interaction.response.send_message(f"🗑️ Quitada de la cola: **{removed.title}**")
    else:
        await interaction.response.send_message("❌ No pude quitar esa canción.", ephemeral=True)


@bot.tree.command(name="volume", description="Ajusta el volumen de reproducción (0-100)")
@app_commands.describe(nivel="Volumen de 0 a 100")
async def volume(interaction: discord.Interaction, nivel: app_commands.Range[int, 0, 100]):
    if await deny_wrong_channel(interaction):
        return
    player = music_manager.get_player(interaction.guild)
    player.volume = nivel / 100

    # Aplicar en vivo si hay algo sonando
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume

    await interaction.response.send_message(f"🔊 Volumen ajustado al **{nivel}%**.")


@bot.tree.command(name="clear", description="Vacía la cola (sin cortar la canción que suena ahora)")
async def clear(interaction: discord.Interaction):
    if await deny_wrong_channel(interaction):
        return
    player = music_manager.get_player(interaction.guild)
    count = len(player.queue)
    if count == 0:
        return await interaction.response.send_message("📭 La cola ya está vacía.", ephemeral=True)

    player.clear_queue()
    await interaction.response.send_message(
        f"🧹 Cola vaciada (**{count}** canciones eliminadas). La canción actual sigue sonando."
    )


@bot.tree.command(name="stop", description="Detiene la música, limpia la cola y expulsa al bot")
async def stop(interaction: discord.Interaction):
    if await deny_wrong_channel(interaction):
        return
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


@bot.tree.command(name="help", description="Muestra la lista de comandos del bot")
async def help_cmd(interaction: discord.Interaction):
    """Lista todos los comandos disponibles."""
    embed = discord.Embed(
        title="🎧 Comandos de ReimbouBOT",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🎵 Música",
        value=(
            "`/play <búsqueda>` — Reproduce de YouTube o una playlist\n"
            "`/radio <emisora>` — Radio española en directo\n"
            "`/current` — Qué suena ahora mismo\n"
            "`/queue` — Ver la cola\n"
            "`/skip` — Saltar a la siguiente\n"
            "`/pause` · `/resume` — Pausar / reanudar\n"
            "`/loop <modo>` — Bucle (off / canción / cola)\n"
            "`/shuffle` — Mezclar la cola\n"
            "`/remove <nº>` — Quitar una canción de la cola\n"
            "`/clear` — Vaciar la cola\n"
            "`/volume <0-100>` — Ajustar el volumen\n"
            "`/stop` — Parar y desconectar el bot"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Administración",
        value="`/setup <canal>` — Fija el canal de comandos del bot *(requiere Gestionar servidor)*",
        inline=False
    )
    embed.set_footer(text="ReimbouBOT")
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == '__main__':
    # Arranca el cliente de Discord escuchando el Gateway y enrutando eventos
    bot.run(TOKEN)
