"""
Eventos de voz: cuando alguien con un rol entra en el canal de voz que tiene asignado, el bot crea un
evento de Discord en ese canal, con el nombre del rol, y lo inicia en el acto (sale en directo en el
icono del servidor). No escribe ni menciona a nadie.

- Un admin elige las parejas canal-rol con /evento-voz (un rol por canal).
- El evento dura mientras haya alguien en el canal, tenga el rol o no. En cuanto se queda vacío, el
  bot lo termina, sin esperar.
- Si el bot se reinicia, recupera los eventos que dejó en directo y repasa los canales.

Sustituye a los eventos con tarjeta y votos (eventos.py), que se borraron el 2026-11-07: siguen en el
historial de git.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional

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

    async def cog_load(self):
        init_db()
        self.pairs = load_pairs()

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
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent):
        """Si alguien borra a mano un evento del bot, lo olvida."""
        for channel_id, live in list(self.live.items()):
            if live.id == event.id:
                del self.live[channel_id]

    @commands.Cog.listener()
    async def on_ready(self):
        """Al arrancar: recupera los eventos que dejó en directo y repasa los canales (alguien pudo entrar o salir)."""
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

    # ---------- Comando ----------

    def _list_card(self, guild: discord.Guild) -> tarjetas.Card:
        pairs = self.pairs.get(guild.id, {})
        lines = [f"🔊 <#{channel_id}> → <@&{role_id}>" for channel_id, role_id in pairs.items()]
        card = tarjetas.Card(tarjetas.INFO)
        card.text("### 🔊 Eventos de voz\n" + ("\n".join(lines) or "Ningún canal todavía."))
        card.note(f"Al entrar alguien con el rol, se crea un evento de Discord con su nombre; se termina en "
                  f"cuanto el canal se vacía. Añade uno con {tarjetas.cmd('evento-voz')}.")
        return card

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
                 f"«{escape_markdown(rol.name)}», sin avisar a nadie. Se termina en cuanto el canal se vacía."]
        if not (guild.me.guild_permissions.create_events or guild.me.guild_permissions.manage_events):
            lines.append("⚠️ Dale al bot el permiso «Crear eventos», o no podrá crearlo.")
        voice_perms = canal.permissions_for(guild.me)
        if not (voice_perms.view_channel and voice_perms.connect):
            lines.append(f"⚠️ El bot necesita ver {canal.mention} y poder conectarse para crear ahí el evento.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)
        await self.check_channel(guild, canal.id)  # Por si ya hay alguien con el rol dentro
