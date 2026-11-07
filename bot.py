"""
Módulo Principal del Bot de Discord.
Maneja la conexión con la API de Discord, carga los módulos (cogs), registra los comandos de barra
y los eventos de auditoría (logs de mensajes borrados). La música está en musica.py.
"""

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

from config import TOKEN, ADMIN_LOG_CHANNEL_ID, AUDIT_WAIT_SECONDS
import audit
import ayuda
import cache
import db
import tarjetas
from notifier import send_deletion_log, send_bulk_delete_log
from musica import MusicaCog
from palabra import PalabraCog
from cuentas import CuentasCog
from niveles import NivelesCog
from perfil import PerfilCog
from forja import ForjaCog
from eventos_voz import EventosVozCog
from tarjetas import NO_MENTIONS

# --- INICIALIZACIÓN ---
logger = logging.getLogger(__name__)

# Huella de los comandos que se subieron a Discord la última vez. Bórralo para forzar la subida.
SYNC_STAMP = Path(__file__).parent / ".comandos_sincronizados"

# Nombres en español de los permisos que piden los comandos (para el mensaje de error)
PERMISSION_NAMES = {"manage_guild": "Gestionar servidor"}

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
        # Botones y menús persistentes: siguen respondiendo tras un reinicio
        self.add_dynamic_items(tarjetas.PageButton, ayuda.HelpMenu)
        # Música: /play, /radio, /queue... y la tarjeta «Sonando ahora» con botones
        await self.add_cog(MusicaCog(self))
        # Juego diario: /palabra, /palabra-stats, /setup-palabra y el anuncio de cada día
        await self.add_cog(PalabraCog(self))
        # Cuentas vinculadas (varias cuentas de una misma persona); los niveles las usan
        await self.add_cog(CuentasCog(self))
        # Niveles: experiencia por mensajes y voz, avisos y roles de recompensa
        await self.add_cog(NivelesCog(self))
        # Perfil de personaje: /perfil (título, nivel, monedas) y /titulos
        await self.add_cog(PerfilCog(self))
        # Forja: /craftear y sus botones (plata y oro se craftean con cobre)
        await self.add_cog(ForjaCog(self))
        # Eventos de voz: /evento-voz; evento de Discord automático al entrar alguien con el rol en su canal
        await self.add_cog(EventosVozCog(self))
        await self.register_commands()

    async def register_commands(self):
        """
        Sube los comandos de barra a Discord solo si han cambiado desde la última vez: Discord limita
        cuántas veces al día se pueden subir, y un bucle de reinicios podría agotarlas. Si no han cambiado,
        solo se piden sus IDs, que permiten mencionarlos en los mensajes (se ven como /comando y se pueden pulsar).
        """
        local = [command.to_dict(self.tree) for command in self.tree.get_commands()]
        digest = hashlib.sha256(json.dumps(local, sort_keys=True).encode()).hexdigest()
        try:
            uploaded = SYNC_STAMP.read_text(encoding="utf-8").strip() == digest
        except OSError:
            uploaded = False

        registered = await self.tree.fetch_commands() if uploaded else []
        if {command.name for command in registered} != {command["name"] for command in local}:
            # Han cambiado, o Discord no los tiene (p. ej. el TOKEN es de otro bot)
            registered = await self.tree.sync()
            try:
                SYNC_STAMP.write_text(digest, encoding="utf-8")
            except OSError as e:
                logger.warning(f"⚠️ No pude guardar {SYNC_STAMP.name}: se volverán a subir en el próximo arranque ({e})")
            logger.info(f"✅ {len(registered)} comandos de barra subidos a Discord: "
                        + ", ".join(sorted(command.name for command in registered)))
        else:
            logger.info(f"✅ {len(registered)} comandos de barra sin cambios: no hace falta subirlos")

        for command in registered:
            tarjetas.command_ids[command.name] = command.id


bot = MusicBot()


# ==========================================
# EVENTOS DEL SISTEMA
# ==========================================

@bot.event
async def on_ready():
    """Se dispara cuando el bot establece conexión exitosa con los servidores de Discord."""
    global log_guild_id
    logger.info(f"✅ Bot conectado como {bot.user}")
    # Emojis propios (monedas, barra de progreso): los de la aplicación (o de un servidor) por su nombre
    await tarjetas.find_emojis(bot)

    # Autodetección del servidor de auditoría a partir del canal de logs.
    admin_channel = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
    if admin_channel and admin_channel.guild:
        log_guild_id = admin_channel.guild.id
        logger.info(f"📓 Auditoría de mensajes activa SOLO en: {admin_channel.guild.name} ({log_guild_id})")
        await audit.prime(admin_channel.guild)
    else:
        logger.warning("⚠️ No se encontró el canal admin; la auditoría de mensajes está desactivada. "
                       "Revisa ADMIN_LOG_CHANNEL_ID y que el bot esté en ese servidor.")


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
    author_name = message.author.name  # El mismo nombre que llega al editar (campo «username»)
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
    await send_deletion_log(
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
# COMANDO DE CONFIGURACIÓN
# ==========================================

@bot.tree.command(name="setup", description="Configura el canal donde el bot acepta comandos (solo admins)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
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
    if quitar:
        db.clear_music_channel(interaction.guild_id)
        return await interaction.response.send_message(
            "✅ Restricción quitada: los comandos del bot funcionan en cualquier canal."
        )

    if canal is None:
        configured = db.get_music_channel(interaction.guild_id)
        status = (f"Los comandos del bot solo funcionan en <#{configured}>." if configured
                  else "No hay canal fijado: los comandos funcionan en cualquier canal.")
        card = tarjetas.Card(tarjetas.INFO).text(f"### ⚙️ Canal de comandos\n{status}")
        card.note("Cámbialo con /setup canal:#canal, o quita la restricción con /setup quitar:True.")
        return await interaction.response.send_message(view=card, ephemeral=True)

    perms = canal.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"⚠️ No puedo escribir bien en {canal.mention}: necesito **Ver canal**, **Enviar mensajes** "
            f"e **Insertar enlaces** ahí. Dame esos permisos y repite `/setup`.", ephemeral=True
        )

    if db.set_music_channel(interaction.guild_id, canal.id):
        await interaction.response.send_message(
            f"✅ Listo. A partir de ahora los comandos del bot se usan en {canal.mention}.\n"
            f"-# Vuelve a usar /setup para cambiarlo."
        )
    else:
        await interaction.response.send_message("❌ No pude guardar la configuración. Revisa los logs.", ephemeral=True)


@bot.tree.command(name="help", description="Muestra la lista de comandos del bot")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    """Tarjeta de ayuda con un menú por secciones (solo la ve quien la pide)."""
    await interaction.response.send_message(view=ayuda.help_card(interaction.user), ephemeral=True,
                                            allowed_mentions=NO_MENTIONS)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Responde con un mensaje claro en vez de 'La aplicación no respondió'."""
    if isinstance(error, app_commands.NoPrivateMessage):
        message = "❌ Este comando solo funciona dentro de un servidor."
    elif isinstance(error, app_commands.MissingPermissions):
        names = ", ".join(PERMISSION_NAMES.get(name, name) for name in error.missing_permissions)
        command = f" para usar `/{interaction.command.qualified_name}`" if interaction.command else ""
        message = f"❌ Necesitas el permiso **{names}**{command}."
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
