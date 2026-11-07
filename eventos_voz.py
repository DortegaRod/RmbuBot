"""
Eventos de voz: cuando alguien con un rol entra en el canal de voz que tiene asignado, el bot crea un
evento de Discord en ese canal, con el nombre del rol, y lo inicia en el acto (sale en directo en el
icono del servidor). No menciona a nadie.

- Un admin elige las parejas canal-rol con /evento-voz (un rol por canal).
- El evento dura mientras haya alguien en el canal, tenga el rol o no. En cuanto se queda vacío, el
  bot lo termina, sin esperar.
- Si un admin elige un canal de avisos con /setup-eventos, al empezar cualquier evento (del bot o creado
  a mano) se publica ahí su enlace; al terminar, el aviso dice que ha terminado y pierde el enlace.
- Si el bot se reinicia, recupera los eventos que dejó en directo, repasa los canales y cierra los
  avisos de los eventos que terminaron mientras estaba apagado.

Sustituye a los eventos con tarjeta y votos (eventos.py), que se borraron el 2026-11-07: siguen en el
historial de git.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

import db
import tarjetas
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)

START_DELAY = timedelta(minutes=1)  # Discord no deja crearlo con la hora ya pasada: se crea a un minuto y se inicia ya


# ==========================================
# BASE DE DATOS
# ==========================================

def init_db():
    with db.get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS eventos_voz (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL
        )""")
        # Canal donde se avisa de los eventos que empiezan (/setup-eventos)
        conn.execute("""CREATE TABLE IF NOT EXISTS eventos_voz_config (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL
        )""")
        # Avisos de eventos aún en directo: se editan al terminar (aunque el bot se haya reiniciado)
        conn.execute("""CREATE TABLE IF NOT EXISTS eventos_voz_avisos (
            event_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            nombre TEXT NOT NULL
        )""")


def load_pairs() -> Dict[int, Dict[int, int]]:
    """Servidor -> {canal de voz: rol}."""
    pairs: Dict[int, Dict[int, int]] = defaultdict(dict)
    with db.get_db_connection() as conn:
        for channel_id, guild_id, role_id in conn.execute("SELECT channel_id, guild_id, role_id FROM eventos_voz"):
            pairs[guild_id][channel_id] = role_id
    return pairs


def save_pair(guild_id: int, channel_id: int, role_id: Optional[int]):
    """Asigna un rol al canal (sustituye al que tuviera) o, con role_id None, se lo quita."""
    with db.get_db_connection() as conn:
        if role_id is None:
            conn.execute("DELETE FROM eventos_voz WHERE channel_id = ?", (channel_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO eventos_voz (channel_id, guild_id, role_id) VALUES (?, ?, ?)",
                         (channel_id, guild_id, role_id))


def load_notice_channels() -> Dict[int, int]:
    """Servidor -> canal de avisos."""
    with db.get_db_connection() as conn:
        return dict(conn.execute("SELECT guild_id, channel_id FROM eventos_voz_config").fetchall())


def save_notice_channel(guild_id: int, channel_id: Optional[int]):
    """Fija el canal de avisos de un servidor o, con None, deja de avisar."""
    with db.get_db_connection() as conn:
        if channel_id is None:
            conn.execute("DELETE FROM eventos_voz_config WHERE guild_id = ?", (guild_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO eventos_voz_config (guild_id, channel_id) VALUES (?, ?)",
                         (guild_id, channel_id))


def save_notice(event: discord.ScheduledEvent, message: discord.Message):
    with db.get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO eventos_voz_avisos (event_id, guild_id, channel_id, message_id, nombre) "
                     "VALUES (?, ?, ?, ?, ?)", (event.id, event.guild_id, message.channel.id, message.id, event.name))


def pop_notice(event_id: int) -> Optional[Tuple[int, int, str]]:
    """(canal, mensaje, nombre del evento) del aviso de un evento, que deja de estar pendiente."""
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT channel_id, message_id, nombre FROM eventos_voz_avisos WHERE event_id = ?",
                           (event_id,)).fetchone()
        conn.execute("DELETE FROM eventos_voz_avisos WHERE event_id = ?", (event_id,))
    return (row[0], row[1], row[2]) if row else None


def pending_notices(guild_id: int) -> List[int]:
    """Eventos de un servidor cuyo aviso aún dice que están en directo."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT event_id FROM eventos_voz_avisos WHERE guild_id = ?", (guild_id,)).fetchall()
    return [row[0] for row in rows]


def _humans(channel: discord.abc.GuildChannel) -> List[discord.Member]:
    return [member for member in channel.members if not member.bot]


# ==========================================
# COG
# ==========================================

class EventosVozCog(commands.Cog):
    """/evento-voz y los eventos de Discord que se crean y se terminan solos según quién esté en voz."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pairs: Dict[int, Dict[int, int]] = defaultdict(dict)       # Servidor -> {canal de voz: rol}
        self.live: Dict[int, discord.ScheduledEvent] = {}                # Canal de voz -> su evento en directo
        self.locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # Por canal: crear y terminar, de uno en uno
        self.notice_channels: Dict[int, int] = {}                        # Servidor -> canal de avisos

    async def cog_load(self):
        init_db()
        self.pairs = load_pairs()
        self.notice_channels = load_notice_channels()

    # ---------- El evento de cada canal ----------

    def live_event(self, guild: discord.Guild, channel_id: int) -> Optional[discord.ScheduledEvent]:
        """El evento que el bot tiene en directo en ese canal, o None si no hay o ya ha terminado."""
        event = self.live.get(channel_id)
        if event is None:
            return None
        event = guild.get_scheduled_event(event.id) or event  # El de la caché lo tiene al día Discord
        if event.status is not discord.EventStatus.active:
            del self.live[channel_id]  # Lo terminó Discord o alguien a mano
            return None
        return event

    async def check_channel(self, guild: discord.Guild, channel_id: int):
        """
        Pone al día un canal: si hay alguien con su rol y no hay evento, lo crea; si se ha quedado vacío
        con el evento en marcha, lo termina.
        """
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        # De uno en uno y mirando quién hay ya dentro del candado: si entran dos a la vez, el segundo ya
        # encuentra el evento creado; si alguien se va mientras se crea, después se termina
        async with self.locks[channel_id]:
            humans = _humans(channel)
            event = self.live_event(guild, channel_id)
            if not humans:
                if event is not None:
                    await self._end_event(channel, event)
                return
            role = guild.get_role(self.pairs.get(guild.id, {}).get(channel_id, 0))
            if event is None and role is not None and any(role in member.roles for member in humans):
                await self._start_event(guild, channel, role)

    async def _start_event(self, guild: discord.Guild, channel: discord.VoiceChannel, role: discord.Role):
        reason = f"Ha entrado alguien con el rol {role.name}"
        try:
            event = await guild.create_scheduled_event(
                name=role.name[:100], start_time=discord.utils.utcnow() + START_DELAY,
                entity_type=discord.EntityType.voice, channel=channel,
                privacy_level=discord.PrivacyLevel.guild_only, reason=reason
            )
        except discord.HTTPException as e:
            logger.warning(f"Eventos de voz: no pude crear el evento de #{channel} "
                           f"(¿le falta al bot el permiso «Crear eventos»?): {e}")
            return
        try:
            event = await event.start(reason=reason)
        except discord.HTTPException as e:
            logger.warning(f"Eventos de voz: creé el evento de #{channel} pero no pude iniciarlo: {e}")
            try:
                await event.delete(reason="No se pudo iniciar")
            except discord.HTTPException:
                pass
            return
        self.live[channel.id] = event

    async def _end_event(self, channel: discord.VoiceChannel, event: discord.ScheduledEvent):
        del self.live[channel.id]
        try:
            await event.end(reason="El canal se ha quedado vacío")
        except discord.HTTPException as e:
            logger.warning(f"Eventos de voz: no pude terminar el evento de #{channel}: {e}")

    # ---------- Avisos en el canal de eventos ----------

    async def _announce(self, event: discord.ScheduledEvent):
        """Publica el enlace del evento que acaba de empezar (Discord lo enseña como su tarjeta)."""
        channel = event.guild.get_channel(self.notice_channels.get(event.guild_id, 0))
        if channel is None:
            return
        where = f" en {event.channel.mention}" if event.channel else ""
        try:
            message = await channel.send(f"🔴 **{escape_markdown(event.name)}** está en directo{where}\n{event.url}",
                                         allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            logger.warning(f"Eventos de voz: no pude avisar del evento «{event.name}» en #{channel}: {e}")
            return
        await asyncio.to_thread(save_notice, event, message)

    async def _close_notice(self, guild: discord.Guild, event_id: int):
        """El aviso de un evento que ha terminado lo dice y pierde el enlace (y con él, la tarjeta del evento)."""
        notice = await asyncio.to_thread(pop_notice, event_id)
        channel = guild.get_channel(notice[0]) if notice else None
        if channel is None:
            return
        try:
            await channel.get_partial_message(notice[1]).edit(
                content=f"⚫ **{escape_markdown(notice[2])}** ha terminado.", embeds=[], allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException:
            pass  # Habían borrado el aviso

    # ---------- Lo que pasa en el servidor ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return  # Silenciarse o ensordecerse no cambia quién está en el canal
        watched = self.pairs.get(member.guild.id, {})
        for channel in (before.channel, after.channel):
            if channel is not None and (channel.id in watched or channel.id in self.live):
                await self.check_channel(member.guild, channel.id)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        """Avisa de los eventos que empiezan (los del bot y los de cualquiera) y cierra el aviso al terminar."""
        active = discord.EventStatus.active
        if before.status is not active and after.status is active:
            await self._announce(after)
        elif before.status is active and after.status is not active:
            await self._close_notice(after.guild, after.id)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent):
        """Si alguien borra a mano un evento del bot, lo olvida (y cierra su aviso, si lo tenía)."""
        for channel_id, live in list(self.live.items()):
            if live.id == event.id:
                del self.live[channel_id]
        await self._close_notice(event.guild, event.id)

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Al arrancar: cierra los avisos de los eventos que terminaron con el bot apagado, recupera los eventos
        que dejó en directo y repasa los canales (alguien pudo entrar o salir).
        """
        for guild in self.bot.guilds:
            live_ids = {event.id for event in guild.scheduled_events if event.status is discord.EventStatus.active}
            for event_id in await asyncio.to_thread(pending_notices, guild.id):
                if event_id not in live_ids:
                    await self._close_notice(guild, event_id)
        for guild_id, pairs in list(self.pairs.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None or not pairs:
                continue
            await self._recover(guild)
            for channel_id in list(pairs):
                await self.check_channel(guild, channel_id)

    async def _recover(self, guild: discord.Guild):
        try:
            events = await guild.fetch_scheduled_events(with_counts=False)
        except discord.HTTPException as e:
            logger.warning(f"Eventos de voz: no pude leer los eventos de {guild}: {e}")
            return
        for event in events:
            if (event.creator_id == self.bot.user.id and event.status is discord.EventStatus.active
                    and event.channel_id in self.pairs.get(guild.id, {})):
                self.live.setdefault(event.channel_id, event)

    # ---------- Comandos ----------

    def _list_card(self, guild: discord.Guild) -> tarjetas.Card:
        pairs = self.pairs.get(guild.id, {})
        lines = [f"🔊 <#{channel_id}> → <@&{role_id}>" for channel_id, role_id in pairs.items()]
        notice_channel = self.notice_channels.get(guild.id)
        notices = (f"📣 Se avisa con el enlace en <#{notice_channel}> al empezar cada evento." if notice_channel
                   else f"📣 Sin canal de avisos (elígelo con {tarjetas.cmd('setup-eventos')}).")
        card = tarjetas.Card(tarjetas.INFO)
        card.text("### 🔊 Eventos de voz\n" + ("\n".join(lines) or "Ningún canal todavía.") + f"\n{notices}")
        card.note(f"Al entrar alguien con el rol, se crea un evento de Discord con su nombre; se termina en "
                  f"cuanto el canal se vacía. Añade uno con {tarjetas.cmd('evento-voz')}.")
        return card

    @app_commands.command(name="setup-eventos",
                          description="Canal donde se avisa con el enlace cuando empieza un evento (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de los avisos (vacío = ver la configuración)",
                           quitar="Deja de avisar cuando empieza un evento")
    async def setup_eventos(self, interaction: discord.Interaction,
                            canal: Optional[Union[discord.TextChannel, discord.VoiceChannel]] = None,
                            quitar: bool = False):
        guild = interaction.guild
        if quitar:
            await asyncio.to_thread(save_notice_channel, guild.id, None)
            self.notice_channels.pop(guild.id, None)
            return await interaction.response.send_message("✅ Ya no se avisa cuando empieza un evento.",
                                                           ephemeral=True)
        if canal is None:
            return await interaction.response.send_message(view=self._list_card(guild), ephemeral=True,
                                                           allowed_mentions=NO_MENTIONS)
        perms = canal.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"⚠️ No puedo avisar bien en {canal.mention}: necesito **Ver canal**, **Enviar mensajes** e "
                f"**Insertar enlaces** ahí (sin este, el enlace no se ve como la tarjeta del evento).", ephemeral=True
            )
        await asyncio.to_thread(save_notice_channel, guild.id, canal.id)
        self.notice_channels[guild.id] = canal.id
        await interaction.response.send_message(
            f"✅ Cuando empiece un evento, se avisará en {canal.mention} con su enlace, sin mencionar a nadie. "
            f"Al terminar, el aviso lo dirá.", ephemeral=True
        )

    @app_commands.command(name="evento-voz",
                          description="Evento de Discord automático al entrar alguien con un rol en un canal de voz "
                                      "(solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de voz (vacío = ver los que hay)",
                           rol="Al entrar alguien con este rol, se crea un evento con su nombre",
                           quitar="Que ese canal deje de crear eventos")
    async def evento_voz(self, interaction: discord.Interaction, canal: Optional[discord.VoiceChannel] = None,
                         rol: Optional[discord.Role] = None, quitar: bool = False):
        guild = interaction.guild
        if canal is None:
            return await interaction.response.send_message(view=self._list_card(guild), ephemeral=True,
                                                           allowed_mentions=NO_MENTIONS)
        if quitar:
            save_pair(guild.id, canal.id, None)
            self.pairs[guild.id].pop(canal.id, None)
            return await interaction.response.send_message(f"✅ {canal.mention} ya no crea eventos.", ephemeral=True)
        if rol is None:
            return await interaction.response.send_message(
                "❌ Elige también el rol (o marca `quitar` para que ese canal deje de crear eventos).", ephemeral=True
            )
        if rol.is_default() or rol.managed:
            return await interaction.response.send_message(
                "❌ Elige un rol normal: ni @everyone ni el rol propio de un bot.", ephemeral=True
            )
        save_pair(guild.id, canal.id, rol.id)
        self.pairs[guild.id][canal.id] = rol.id
        lines = [f"✅ Cuando alguien con {rol.mention} entre en {canal.mention}, se creará el evento "
                 f"«{escape_markdown(rol.name)}», sin mencionar a nadie. Se termina en cuanto el canal se vacía."]
        if not (guild.me.guild_permissions.create_events or guild.me.guild_permissions.manage_events):
            lines.append("⚠️ Dale al bot el permiso «Crear eventos», o no podrá crearlo.")
        voice_perms = canal.permissions_for(guild.me)
        if not (voice_perms.view_channel and voice_perms.connect):
            lines.append(f"⚠️ El bot necesita ver {canal.mention} y poder conectarse para crear ahí el evento.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)
        await self.check_channel(guild, canal.id)  # Por si ya hay alguien con el rol dentro
