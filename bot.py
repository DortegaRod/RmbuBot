"""
Módulo Principal del Bot de Discord.
Maneja la conexión con la API de Discord, enrutamiento de comandos (Slash Commands),
y eventos de auditoría (logs de mensajes borrados).
"""

import asyncio
import logging
import re
from typing import List, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from config import TOKEN, ADMIN_LOG_CHANNEL_ID, AUDIT_WAIT_SECONDS, MAX_QUEUE_SIZE
import audit
import cache
import controls
import db
from notifier import send_admin_embed, send_bulk_delete_log
from music import (
    music_manager, search_youtube, play_next, disconnect_player, empty_channel_disconnect,
    log_dependency_status, LOOP_LABELS, SearchError, Song
)
from radios import RADIOS, RADIOS_BY_KEY, RADIO_ICON, fetch_now_playing
from palabra import PalabraCog

# --- INICIALIZACIÓN ---
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
        db.init_db()
        log_dependency_status()
        # Ganchos del motor de música: tarjeta "Sonando ahora" y avisos en el canal de texto
        music_manager.on_track_start = controls.announce_now_playing
        music_manager.on_track_error = controls.announce_error
        music_manager.on_player_end = controls.delete_now_playing
        # Botones persistentes: las tarjetas antiguas siguen respondiendo tras un reinicio
        self.add_view(controls.NowPlayingView())
        # Juego diario: /palabra, /palabra-stats, /setup-palabra y el anuncio de cada día
        await self.add_cog(PalabraCog(self))
        # Sincroniza los comandos de barra (Slash Commands) con Discord al iniciar
        await self.tree.sync()


bot = MusicBot()


# ==========================================
# EVENTOS DEL SISTEMA
# ==========================================

@bot.event
async def on_ready():
    """Se dispara cuando el bot establece conexión exitosa con los servidores de Discord."""
    global log_guild_id
    logger.info(f"✅ Bot conectado como {bot.user}")

    # Autodetección del servidor de auditoría a partir del canal de logs.
    admin_channel = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
    if admin_channel and admin_channel.guild:
        log_guild_id = admin_channel.guild.id
        logger.info(f"📓 Auditoría de mensajes activa SOLO en: {admin_channel.guild.name} ({log_guild_id})")
        await audit.prime(admin_channel.guild)
    else:
        logger.warning("⚠️ No se encontró el canal admin; la auditoría de mensajes está desactivada. "
                       "Revisa ADMIN_LOG_CHANNEL_ID y que el bot esté en ese servidor.")


@bot.event
async def on_voice_state_update(member, before, after):
    """Monitoriza cambios de estado en canales de voz (conexiones, desconexiones)."""
    guild = member.guild

    # Limpieza si el bot es expulsado/desconectado manualmente (incluida su tarjeta de botones)
    if member.id == bot.user.id and after.channel is None:
        player = music_manager.remove_player(guild.id)
        if player:
            await music_manager.notify("on_player_end", player)
        return

    vc = guild.voice_client
    player = music_manager.peek(guild.id)
    if vc is None or vc.channel is None or player is None:
        return

    # Auto-salida: si el canal de voz del bot se queda sin humanos, programar desconexión.
    humans = [m for m in vc.channel.members if not m.bot]
    if not humans:
        if player.empty_task is None:
            player.empty_task = asyncio.create_task(empty_channel_disconnect(vc, player))
    elif player.empty_task is not None:
        # Volvió alguien: cancelar la salida programada
        player.empty_task.cancel()
        player.empty_task = None


# ==========================================
# AUDITORÍA DE MENSAJES (solo el servidor de logs)
# ==========================================

def _describe_message(text: str, attachments: List[Tuple[str, str]], stickers: List[str], has_embeds: bool) -> str:
    """Texto que se guarda de un mensaje: su contenido más adjuntos y stickers (antes se perdían)."""
    parts = [text] if text else []
    parts += [f"📎 [{escape_markdown(name)}]({url})" for name, url in attachments]
    parts += [f"🏷️ Sticker: {escape_markdown(name)}" for name in stickers]
    if not parts:
        parts.append("[Embed]" if has_embeds else "[Sin contenido]")
    return "\n".join(parts)


def _without_urls(text: Optional[str]) -> str:
    """Para comparar versiones sin contar los cambios de firma de las URLs de los adjuntos."""
    return re.sub(r"\]\(https?://[^)]*\)", "]", text or "")


def _user_display(user_id: Optional[int], name: Optional[str]) -> str:
    if user_id is None:
        return "Desconocido"
    return f"<@{user_id}> ({escape_markdown(name)})" if name else f"<@{user_id}>"


def _channel_display(guild: discord.Guild, channel_id: int) -> str:
    """Mención del canal. Funciona también con hilos (antes el log fallaba con ellos)."""
    channel = guild.get_channel_or_thread(channel_id)
    return channel.mention if channel else f"<#{channel_id}>"


def _admin_channel(guild_id: int) -> Optional[discord.abc.Messageable]:
    guild = bot.get_guild(guild_id)
    return guild.get_channel(ADMIN_LOG_CHANNEL_ID) if guild else None


@bot.event
async def on_message(message: discord.Message):
    """Captura mensajes nuevos para alimentar la base de datos y la caché de logs."""
    if message.author.bot or message.guild is None or message.is_system():
        return
    # Solo auditamos el servidor configurado
    if log_guild_id is None or message.guild.id != log_guild_id:
        return

    content = _describe_message(
        message.content,
        [(a.filename, a.url) for a in message.attachments],
        [s.name for s in message.stickers],
        bool(message.embeds),
    )
    author_name = str(message.author)
    # Primero en memoria (instantáneo): así no se pierde ni un mensaje borrado al momento
    cache.cache_message(message.id, message.author.id, content, author_name=author_name, channel_id=message.channel.id)
    # Guardado en un hilo aparte para no bloquear el event loop del bot
    await asyncio.to_thread(db.save_message, message.id, message.author.id, content, message.channel.id, author_name)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """Guarda la versión editada: si luego se borra, el log dirá qué ponía (y qué ponía antes)."""
    if log_guild_id is None or payload.guild_id != log_guild_id:
        return
    data = payload.data
    author = data.get("author") or {}
    # Las vistas previas de enlaces también llegan como "edición", pero sin edited_timestamp
    if not data.get("edited_timestamp") or "content" not in data or author.get("bot") or data.get("webhook_id"):
        return

    content = _describe_message(
        data.get("content") or "",
        [(a.get("filename", "archivo"), a.get("url", "")) for a in data.get("attachments") or []],
        [s.get("name", "sticker") for s in data.get("sticker_items") or []],
        bool(data.get("embeds")),
    )
    cached = cache.get_cached(payload.message_id)
    if cached is not None:
        if _without_urls(cached.get("edited_content") or cached.get("content")) == _without_urls(content):
            return  # No cambió el texto
        cache.update_cached(payload.message_id, edited_content=content)

    author_id = int(author["id"]) if author.get("id") else None
    await asyncio.to_thread(
        db.update_message_content, payload.message_id, content, author_id, payload.channel_id, author.get("username")
    )


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Captura el evento crudo de eliminación de un mensaje y procesa su auditoría."""
    # Solo auditamos el servidor configurado
    if log_guild_id is None or payload.guild_id != log_guild_id:
        return

    # Intenta recuperar de memoria volátil (Caché), si falla, acude a SQLite (DB)
    record = cache.get_cached(payload.message_id)
    if record is None:
        record = await asyncio.to_thread(db.get_message, payload.message_id)
    cache.remove_cached(payload.message_id)
    if not record or not record.get("content"):
        return

    # Espera preventiva para dar tiempo a los servidores de Discord a registrar el Audit Log
    await asyncio.sleep(AUDIT_WAIT_SECONDS)
    admin_channel = _admin_channel(payload.guild_id)
    if admin_channel is None:
        return
    guild = admin_channel.guild

    # Consultar quién borró el mensaje (Requiere permisos 'View Audit Log')
    author_id = record.get("author_id")
    executor, audit_ok = await audit.find_deleter(guild, payload.channel_id, author_id)
    if executor is not None:
        executor_display = executor.mention
    elif audit_ok:
        executor_display = "🙋 El propio autor"  # Discord no registra los autoborrados en la auditoría
    else:
        executor_display = "Desconocido *(el bot no tiene permiso para Ver el registro de auditoría)*"

    edited = record.get("edited_content")
    original = record["content"]
    await send_admin_embed(
        admin_channel,
        author_display=_user_display(author_id, record.get("author_name")),
        executor_display=executor_display,
        channel_display=_channel_display(guild, payload.channel_id),
        content=edited or original,
        original_content=original if edited and _without_urls(edited) != _without_urls(original) else None,
        message_id=payload.message_id,
    )


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    """Registra las purgas (muchos mensajes borrados a la vez por un moderador o un bot)."""
    if log_guild_id is None or payload.guild_id != log_guild_id:
        return

    ids = sorted(payload.message_ids)  # Los IDs de Discord son cronológicos
    records = {}
    for message_id in ids:
        record = cache.get_cached(message_id)
        cache.remove_cached(message_id)
        if record is not None:
            records[message_id] = dict(record, message_id=message_id)
    missing = [message_id for message_id in ids if message_id not in records]
    if missing:
        records.update(await asyncio.to_thread(db.get_messages, missing))

    await asyncio.sleep(AUDIT_WAIT_SECONDS)
    admin_channel = _admin_channel(payload.guild_id)
    if admin_channel is None:
        return
    guild = admin_channel.guild

    executor, audit_ok = await audit.find_bulk_deleter(guild, payload.channel_id)
    if executor is not None:
        executor_display = executor.mention
    else:
        executor_display = "Desconocido" if audit_ok else "Desconocido *(sin permiso para Ver el registro de auditoría)*"

    await send_bulk_delete_log(
        admin_channel,
        channel_display=_channel_display(guild, payload.channel_id),
        executor_display=executor_display,
        total=len(ids),
        records=[records[message_id] for message_id in ids if message_id in records],
    )


# ==========================================
# UTILIDADES DE COMANDOS
# ==========================================

async def deny_wrong_channel(interaction: discord.Interaction) -> bool:
    """
    Comprueba si la interacción ocurre en el canal de comandos configurado con /setup.
    Si NO está permitido, responde con un aviso efímero y devuelve True (denegar).
    Si el servidor no ha hecho /setup, se permite en cualquier canal.
    """
    configured = db.get_music_channel(interaction.guild_id)
    if configured is not None and interaction.channel_id != configured:
        await interaction.response.send_message(
            f"❌ Usa los comandos del bot en <#{configured}>.", ephemeral=True
        )
        return True
    return False


async def deny_control(interaction: discord.Interaction) -> bool:
    """Comprobaciones de los comandos que controlan la música: canal de /setup y estar en la llamada."""
    if await deny_wrong_channel(interaction):
        return True
    error = controls.control_error(interaction.user)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return True
    return False


async def deny_join(interaction: discord.Interaction) -> bool:
    """Comprobaciones previas de /play y /radio."""
    if await deny_wrong_channel(interaction):
        return True
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)
        return True
    error = controls.join_error(interaction.user)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return True
    player = music_manager.peek(interaction.guild_id)
    if player is not None and player.free_slots() == 0:
        await interaction.response.send_message(
            f"📭 La cola está llena ({MAX_QUEUE_SIZE} canciones). Usa `/clear` o espera a que avance.", ephemeral=True
        )
        return True
    return False


async def connect_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """Conecta (o mueve) al bot al canal de voz del usuario. Si falla, lo explica y devuelve None."""
    voice = interaction.user.voice
    if voice is None or voice.channel is None:
        await interaction.followup.send("❌ Ya no estás en un canal de voz.")
        return None
    channel = voice.channel
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.connect and perms.speak):
        await interaction.followup.send(f"❌ No tengo permiso para conectarme y hablar en {channel.mention}.")
        return None

    vc = interaction.guild.voice_client
    try:
        if vc is not None and not vc.is_connected():
            await vc.disconnect(force=True)  # Conexión anterior colgada
            vc = None
        # Lógica de conexión y movimiento de canal
        if vc is None:
            vc = await channel.connect(self_deaf=True, timeout=20)
        elif vc.channel != channel:
            await vc.move_to(channel)
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ No pude conectarme al canal de voz (tiempo agotado).")
        return None
    except Exception as e:
        logger.exception("Error al conectar al canal de voz")
        await interaction.followup.send(f"❌ Error de conexión: {e}")
        return None
    return vc


# ==========================================
# COMANDO DE CONFIGURACIÓN
# ==========================================

@bot.tree.command(name="setup", description="Configura el canal donde el bot acepta comandos (solo admins)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    canal="Canal para los comandos del bot (déjalo vacío para ver la configuración actual)",
    quitar="Quita la restricción: los comandos funcionarán en cualquier canal",
)
async def setup(
        interaction: discord.Interaction,
        canal: Optional[Union[discord.TextChannel, discord.VoiceChannel]] = None,
        quitar: bool = False
):
    """Configura, por servidor, el canal de comandos autorizado. Persiste en la base de datos."""
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(
            "❌ Necesitas el permiso **Gestionar servidor** para usar `/setup`.", ephemeral=True
        )

    if quitar:
        db.clear_music_channel(interaction.guild_id)
        return await interaction.response.send_message(
            "✅ Restricción quitada: los comandos del bot funcionan en cualquier canal."
        )

    if canal is None:
        configured = db.get_music_channel(interaction.guild_id)
        status = (f"📌 Los comandos del bot solo funcionan en <#{configured}>." if configured
                  else "📌 No hay canal configurado: los comandos funcionan en cualquier canal.")
        return await interaction.response.send_message(
            f"{status}\nUsa `/setup canal:#canal` para cambiarlo o `/setup quitar:True` para quitar la restricción.",
            ephemeral=True
        )

    perms = canal.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"⚠️ No puedo escribir bien en {canal.mention}: necesito **Ver canal**, **Enviar mensajes** "
            f"e **Insertar enlaces** ahí. Dame esos permisos y repite `/setup`.", ephemeral=True
        )

    if db.set_music_channel(interaction.guild_id, canal.id):
        await interaction.response.send_message(
            f"✅ Listo. A partir de ahora los comandos del bot se usan en {canal.mention}.\n"
            f"*(Vuelve a ejecutar `/setup` para cambiarlo.)*"
        )
    else:
        await interaction.response.send_message("❌ No pude guardar la configuración. Revisa los logs.", ephemeral=True)


# ==========================================
# COMANDOS DE REPRODUCCIÓN (SLASH COMMANDS)
# ==========================================

@bot.tree.command(name="play", description="Reproduce música de YouTube (búsqueda, enlace o playlist)")
@app_commands.guild_only()
@app_commands.describe(busqueda="Nombre de la canción, o enlace de YouTube / playlist")
async def play(interaction: discord.Interaction, busqueda: str):
    """Busca en YouTube y añade una canción o playlist a la cola del servidor."""
    if await deny_join(interaction):
        return
    if "spotify.com" in busqueda or busqueda.strip().startswith("spotify:"):
        return await interaction.response.send_message(
            "❌ Spotify no permite reproducir su música fuera de su app. "
            "Escribe el nombre de la canción y la busco en YouTube.", ephemeral=True
        )

    await interaction.response.defer()
    existing = music_manager.peek(interaction.guild_id)
    try:
        songs = await search_youtube(busqueda, limit=existing.free_slots() if existing else MAX_QUEUE_SIZE)
    except SearchError as e:
        return await interaction.followup.send(f"❌ No pude abrir eso: {e}.")
    if not songs:
        return await interaction.followup.send("❌ No encontré resultados.")

    vc = await connect_voice(interaction)
    if vc is None:
        return
    player = music_manager.get_player(interaction.guild)
    player.text_channel = interaction.channel

    # Inyección de metadatos del solicitante
    added = []
    for song in songs:
        song.requester = interaction.user
        if not player.add_song(song):
            break
        added.append(song)
    if not added:
        return await interaction.followup.send(f"📭 La cola está llena ({MAX_QUEUE_SIZE} canciones).")

    starts_now = player.current is None and not vc.is_playing() and not vc.is_paused()

    # Generación de UI (Embed)
    if len(songs) > 1:
        description = f"Se han añadido **{len(added)}** canciones."
        if len(added) < len(songs):
            description += f"\n*(La cola está llena: {len(songs) - len(added)} no cupieron.)*"
        embed = discord.Embed(title="📂 Playlist añadida", description=description, color=discord.Color.purple())
    else:
        song = added[0]
        description = f"**{controls.song_label(song)}**"
        if song.duration:
            description += f"\nDuración: `{controls.fmt_time(song.duration)}`"
        embed = discord.Embed(
            title="🎶 Empieza a sonar" if starts_now else f"📝 En cola · posición {len(player.queue)}",
            description=description,
            color=discord.Color.green() if starts_now else discord.Color.blue()
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text=f"Pedido por {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

    # Iniciar motor si estaba en reposo (después de responder, para que la tarjeta salga detrás)
    if starts_now:
        await play_next(vc, player)


@bot.tree.command(name="radio", description="Sintoniza una radio española en directo")
@app_commands.guild_only()
@app_commands.describe(emisora="Emisora que quieres escuchar")
@app_commands.choices(emisora=[app_commands.Choice(name=f"{r.emoji} {r.name}", value=r.key) for r in RADIOS])
async def radio(interaction: discord.Interaction, emisora: app_commands.Choice[str]):
    """Se conecta directamente a un flujo (stream) HTTP de una emisora de radio."""
    if await deny_join(interaction):
        return

    await interaction.response.defer()
    vc = await connect_voice(interaction)
    if vc is None:
        return

    station = RADIOS_BY_KEY[emisora.value]
    player = music_manager.get_player(interaction.guild)
    player.text_channel = interaction.channel

    # Song con `stream_url` y marcada como radio: music.py se salta yt-dlp y conecta directamente a la emisora.
    radio_song = Song(
        title=station.name,
        webpage_url=station.url,
        thumbnail=RADIO_ICON,
        stream_url=station.url,
        requester=interaction.user,
        is_radio=True
    )
    if not player.add_song(radio_song):
        return await interaction.followup.send(f"📭 La cola está llena ({MAX_QUEUE_SIZE} canciones).")
    starts_now = player.current is None and not vc.is_playing() and not vc.is_paused()

    if starts_now:
        embed = discord.Embed(title="📻 Sintonizando radio", description=f"**{station.emoji} {station.name}**",
                              color=discord.Color.gold())
    else:
        embed = discord.Embed(
            title=f"📝 Radio en cola · posición {len(player.queue)}",
            description=f"**{station.emoji} {station.name}**\n*Usa `/skip` para llegar antes.*",
            color=discord.Color.gold()
        )
    embed.set_thumbnail(url=RADIO_ICON)
    embed.set_footer(text="Emisión en directo 24/7", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

    if starts_now:
        await play_next(vc, player)


@bot.tree.command(name="current", description="Muestra lo que suena ahora (barra de progreso, o la canción de la radio)")
@app_commands.guild_only()
async def current(interaction: discord.Interaction):
    """Muestra la pista en reproducción con su progreso; en radios, el título que emiten."""
    player = music_manager.peek(interaction.guild_id)
    if player is None or player.current is None:
        return await interaction.response.send_message("🔇 No hay nada sonando ahora mismo.", ephemeral=True)

    song = player.current
    if not song.is_radio:
        return await interaction.response.send_message(embed=controls.build_current_embed(player, song))

    # Radio: leer el título que está emitiendo (puede tardar un par de segundos)
    await interaction.response.defer()
    live_title = await fetch_now_playing(song.stream_url, station_name=song.title)
    await interaction.followup.send(embed=controls.build_current_embed(player, song, live_title))


@bot.tree.command(name="queue", description="Muestra la canción actual y las próximas 10 de la cola")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    player = music_manager.peek(interaction.guild_id)
    if player is None or (not player.queue and not player.current):
        return await interaction.response.send_message("📭 La cola está vacía.")

    lines = []
    if player.current:
        lines.append(f"▶️ **Sonando ahora:**\n{controls.song_label(player.current)}\n")

    for i, song in enumerate(list(player.queue)[:10], 1):
        if song.is_radio:
            extra = " · 🔴 directo"
        else:
            extra = f" · `{controls.fmt_time(song.duration)}`" if song.duration else ""
        lines.append(f"`{i}.` {controls.song_label(song, link=False)}{extra}")

    if len(player.queue) > 10:
        lines.append(f"\n*...y {len(player.queue) - 10} más*")

    footer = f"Modo Bucle: {LOOP_LABELS[player.loop_mode]} | Total: {len(player.queue)}"
    total_seconds = sum(song.duration or 0 for song in player.queue if not song.is_radio)
    if total_seconds:
        footer += f" | Duración: {controls.fmt_time(total_seconds)}"
    embed = discord.Embed(title="🎵 Cola de Reproducción", description="\n".join(lines), color=discord.Color.blue())
    embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="skip", description="Termina la pista actual y pasa a la siguiente")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    if await deny_control(interaction):
        return
    skipped = controls.request_skip(interaction.guild)
    if skipped is None:
        return await interaction.response.send_message("❌ Nada sonando.", ephemeral=True)
    await interaction.response.send_message(f"⏭️ Saltada: **{controls.song_label(skipped, link=False)}**")


@bot.tree.command(name="pause", description="Pausa la reproducción actual")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    if await deny_control(interaction):
        return
    if not controls.pause(interaction.guild):
        return await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
    await interaction.response.send_message("⏸️ Reproducción **pausada**. Usa `/resume` para continuar.")
    await controls.refresh_card(interaction.guild)


@bot.tree.command(name="resume", description="Reanuda la reproducción pausada")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    if await deny_control(interaction):
        return
    if not controls.resume(interaction.guild):
        return await interaction.response.send_message("❌ No hay nada pausado.", ephemeral=True)
    await interaction.response.send_message("▶️ Reproducción **reanudada**.")
    await controls.refresh_card(interaction.guild)


@bot.tree.command(name="loop", description="Configura el modo de repetición (Bucle)")
@app_commands.guild_only()
@app_commands.choices(modo=[
    app_commands.Choice(name="⛔ Desactivado", value=0),
    app_commands.Choice(name="🔂 Canción Actual", value=1),
    app_commands.Choice(name="🔁 Toda la Cola", value=2)
])
async def loop(interaction: discord.Interaction, modo: app_commands.Choice[int]):
    if await deny_control(interaction):
        return

    player = music_manager.get_player(interaction.guild)
    player.loop_mode = modo.value

    msgs = {0: "Modo bucle **desactivado**.", 1: "🔂 Bucle: **Canción Actual**.", 2: "🔁 Bucle: **Toda la Cola**."}
    await interaction.response.send_message(msgs[modo.value])
    await controls.refresh_card(interaction.guild)


@bot.tree.command(name="shuffle", description="Mezcla de forma aleatoria la cola de reproducción")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    if await deny_control(interaction):
        return

    player = music_manager.peek(interaction.guild_id)
    if player is None or len(player.queue) < 2:
        return await interaction.response.send_message("❌ Necesitas al menos 2 canciones en la cola.", ephemeral=True)

    player.shuffle_queue()
    await interaction.response.send_message("🔀 **Cola mezclada** aleatoriamente.")


@bot.tree.command(name="remove", description="Quita una canción de la cola por su número (mira /queue)")
@app_commands.guild_only()
@app_commands.describe(posicion="Número de la canción en la cola")
async def remove(interaction: discord.Interaction, posicion: int):
    if await deny_control(interaction):
        return
    player = music_manager.peek(interaction.guild_id)
    if player is None or not player.queue:
        return await interaction.response.send_message("📭 La cola está vacía.", ephemeral=True)
    if posicion < 1 or posicion > len(player.queue):
        return await interaction.response.send_message(
            f"❌ Número inválido. La cola tiene **{len(player.queue)}** canciones.", ephemeral=True
        )

    removed = player.remove_at(posicion - 1)
    if removed:
        await interaction.response.send_message(f"🗑️ Quitada de la cola: **{controls.song_label(removed, link=False)}**")
    else:
        await interaction.response.send_message("❌ No pude quitar esa canción.", ephemeral=True)


@bot.tree.command(name="volume", description="Ajusta el volumen de reproducción (0-100)")
@app_commands.guild_only()
@app_commands.describe(nivel="Volumen de 0 a 100")
async def volume(interaction: discord.Interaction, nivel: app_commands.Range[int, 0, 100]):
    if await deny_control(interaction):
        return
    player = music_manager.get_player(interaction.guild)
    player.volume = nivel / 100

    # Aplicar en vivo si hay algo sonando
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = player.volume

    await interaction.response.send_message(f"🔊 Volumen ajustado al **{nivel}%**.")


@bot.tree.command(name="clear", description="Vacía la cola (sin cortar la canción que suena ahora)")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    if await deny_control(interaction):
        return
    player = music_manager.peek(interaction.guild_id)
    count = len(player.queue) if player else 0
    if count == 0:
        return await interaction.response.send_message("📭 La cola ya está vacía.", ephemeral=True)

    player.clear_queue()
    await interaction.response.send_message(
        f"🧹 Cola vaciada (**{count}** canciones eliminadas). La canción actual sigue sonando."
    )


@bot.tree.command(name="stop", description="Detiene la música, limpia la cola y expulsa al bot")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if await deny_control(interaction):
        return
    if interaction.guild.voice_client is None:
        return await interaction.response.send_message("❌ No estoy conectado.", ephemeral=True)
    await interaction.response.send_message("👋 Adiós.")
    await disconnect_player(interaction.guild)


@bot.tree.command(name="help", description="Muestra la lista de comandos del bot")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    """Lista todos los comandos disponibles."""
    embed = discord.Embed(
        title="🎧 Comandos de ReimbouBOT",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🎵 Música",
        value=(
            "`/play <búsqueda>` — Reproduce de YouTube (búsqueda, enlace o playlist)\n"
            "`/radio <emisora>` — Radio española en directo\n"
            "`/current` — Qué suena ahora (en radios, la canción que emiten)\n"
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
        name="🎛️ Botones y normas",
        value=(
            "Cada canción se anuncia con botones ⏯️ ⏭️ ⏹️ 🔁.\n"
            "Para controlar la música tienes que estar en el canal de voz del bot "
            "*(los moderadores pueden desde cualquier sitio)*."
        ),
        inline=False
    )
    embed.add_field(
        name="🟩 Palabra del día",
        value=(
            "`/palabra` — Adivina la palabra de hoy en 6 intentos (Wordle en español)\n"
            "`/palabra-stats [usuario]` — Estadísticas y rachas"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Administración",
        value=(
            "`/setup <canal>` — Fija el canal de comandos del bot\n"
            "`/setup-palabra <canal> [hora] [rol]` — Canal, hora y rol al que avisar del anuncio diario de la palabra\n"
            "*(requieren Gestionar servidor)*"
        ),
        inline=False
    )
    embed.set_footer(text="ReimbouBOT")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Responde con un mensaje claro en vez de 'La aplicación no respondió'."""
    if isinstance(error, app_commands.NoPrivateMessage):
        message = "❌ Este comando solo funciona dentro de un servidor."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "❌ No tienes permisos para usar este comando."
    else:
        command = interaction.command.name if interaction.command else "?"
        logger.error(f"Error en /{command}", exc_info=getattr(error, "original", error))
        message = "❌ Ha ocurrido un error inesperado. Ha quedado registrado en el log del bot."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == '__main__':
    # Arranca el cliente de Discord escuchando el Gateway y enrutando eventos.
    # log_handler=None: el logging ya está configurado en config.py (evita líneas duplicadas).
    bot.run(TOKEN, log_handler=None)
