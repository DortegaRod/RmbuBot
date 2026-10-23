"""
Eventos: un admin crea un evento (para un rol, p. ej. @SCP 🐊, si quiere) y el bot se encarga del resto.

- La tarjeta del evento sale en el canal de eventos (/setup-eventos), con botones para votar:
  ✅ Voy · 💭 Puede · ❌ No voy. Puede votar cualquiera; pulsar otra vez tu voto lo quita.
- Si tiene rol, a todos los que lo tienen les llega un DM con un botón que lleva a la tarjeta.
- A su hora, el bot menciona al rol en el canal, cierra la votación con el resumen y manda un DM
  a quienes votaron Voy o Puede. Con /evento empezar se puede adelantar.
- Al empezar, el bot crea también el evento de Discord y lo inicia en el acto (sale en directo en el
  icono del servidor). Con canal de voz, lleva a ese canal y Discord lo termina cuando se vacía;
  sin canal, dura DISCORD_EVENT_HOURS.
- /evento cancelar: la tarjeta se queda con el resumen y «Cancelado», sale un aviso en el canal
  y les llega un DM a quienes votaron Voy o Puede.
"""

import asyncio
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple, Union

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from discord.utils import escape_markdown

import db
import tarjetas

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    SPAIN_TZ = ZoneInfo("Europe/Madrid")
except Exception:  # Sistema sin zonas horarias y sin el paquete tzdata
    SPAIN_TZ = timezone.utc

NO_MENTIONS = discord.AllowedMentions.none()
START_GRACE = 30 * 60  # Si el bot estaba apagado a la hora y vuelve más tarde que esto, empieza sin avisar a nadie
DM_PAUSE = 0.5         # Segundos entre DM y DM, para no parecerle spam a Discord
DISCORD_EVENT_HOURS = 3  # Sin canal de voz, lo que dura el evento de Discord (luego Discord lo termina)
NO_ROLE = 0            # role_id de los eventos sin rol
# Voto -> (emoji, botón, cómo se cuenta en la tarjeta)
VOTES = {"si": ("✅", "Voy", "Van"), "puede": ("💭", "Puede", "Puede"), "no": ("❌", "No voy", "No van")}


# ==========================================
# BASE DE DATOS
# ==========================================

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER,
        titulo TEXT NOT NULL,
        descripcion TEXT NOT NULL DEFAULT '',
        role_id INTEGER NOT NULL,
        organizer_id INTEGER NOT NULL,
        inicio INTEGER NOT NULL,
        estado TEXT NOT NULL DEFAULT 'programado',
        motivo TEXT NOT NULL DEFAULT '',
        voice_channel_id INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS evento_votos (
        evento_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        voto TEXT NOT NULL,
        PRIMARY KEY (evento_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS eventos_config (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL
    )""",
)


def init_db():
    with db.get_db_connection() as conn:
        for sql in _SCHEMA:
            conn.execute(sql)
        # Las bases de datos de antes del canal de voz no tienen esa columna
        if "voice_channel_id" not in {row[1] for row in conn.execute("PRAGMA table_info(eventos)")}:
            conn.execute("ALTER TABLE eventos ADD COLUMN voice_channel_id INTEGER")


@dataclass
class Event:
    id: int
    guild_id: int
    channel_id: int
    message_id: Optional[int]
    titulo: str
    descripcion: str
    role_id: int   # NO_ROLE si es para todos
    organizer_id: int
    inicio: int    # Marca de tiempo (segundos)
    estado: str    # programado, empezado o cancelado
    motivo: str
    voice_channel_id: Optional[int] = None

    @property
    def jump_url(self) -> str:
        """Enlace a la tarjeta del evento en el servidor."""
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


_SELECT = ("SELECT id, guild_id, channel_id, message_id, titulo, descripcion, role_id, organizer_id, inicio, "
           "estado, motivo, voice_channel_id FROM eventos ")


def get_events_channel(guild_id: int) -> Optional[int]:
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT channel_id FROM eventos_config WHERE guild_id = ?", (guild_id,)).fetchone()
    return row[0] if row else None


def set_events_channel(guild_id: int, channel_id: int):
    with db.get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO eventos_config (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))


def create_event(guild_id: int, channel_id: int, titulo: str, descripcion: str, role_id: int,
                 organizer_id: int, inicio: int, voice_channel_id: Optional[int] = None) -> int:
    with db.get_db_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO eventos (guild_id, channel_id, titulo, descripcion, role_id, organizer_id, inicio, "
            "voice_channel_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, titulo, descripcion, role_id, organizer_id, inicio, voice_channel_id)
        )
        return cursor.lastrowid


def set_message(event_id: int, message_id: int):
    with db.get_db_connection() as conn:
        conn.execute("UPDATE eventos SET message_id = ? WHERE id = ?", (message_id, event_id))


def get_event(event_id: int) -> Optional[Event]:
    with db.get_db_connection() as conn:
        row = conn.execute(_SELECT + "WHERE id = ?", (event_id,)).fetchone()
    return Event(*row) if row else None


def upcoming_events(guild_id: int) -> List[Event]:
    """Eventos que aún no han empezado, del más cercano al más lejano."""
    with db.get_db_connection() as conn:
        rows = conn.execute(_SELECT + "WHERE guild_id = ? AND estado = 'programado' ORDER BY inicio, id",
                            (guild_id,)).fetchall()
    return [Event(*row) for row in rows]


def due_events(now: int) -> List[Event]:
    """Eventos a los que ya les toca empezar."""
    with db.get_db_connection() as conn:
        rows = conn.execute(_SELECT + "WHERE estado = 'programado' AND inicio <= ? ORDER BY inicio, id",
                            (now,)).fetchall()
    return [Event(*row) for row in rows]


def set_state(event_id: int, estado: str, *, motivo: str = "", inicio: Optional[int] = None) -> bool:
    """Cambia el estado de un evento programado. Devuelve False si ya había empezado o se había cancelado."""
    with db.get_db_connection() as conn:
        cursor = conn.execute(
            "UPDATE eventos SET estado = ?, motivo = ?, inicio = COALESCE(?, inicio) "
            "WHERE id = ? AND estado = 'programado'", (estado, motivo, inicio, event_id)
        )
        return cursor.rowcount == 1


def event_votes(event_id: int) -> Dict[int, str]:
    """{persona: voto}, en el orden en que votaron (el último cambio, al final)."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT user_id, voto FROM evento_votos WHERE evento_id = ? ORDER BY rowid",
                            (event_id,)).fetchall()
    return {user_id: vote for user_id, vote in rows}


def toggle_vote(event_id: int, user_id: int, vote: str) -> Optional[str]:
    """Pone el voto; si ya era ese, lo quita. Devuelve el voto que queda (o None)."""
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT voto FROM evento_votos WHERE evento_id = ? AND user_id = ?",
                           (event_id, user_id)).fetchone()
        conn.execute("DELETE FROM evento_votos WHERE evento_id = ? AND user_id = ?", (event_id, user_id))
        if row and row[0] == vote:
            return None
        conn.execute("INSERT INTO evento_votos (evento_id, user_id, voto) VALUES (?, ?, ?)", (event_id, user_id, vote))
        return vote


# ==========================================
# FECHAS (hora de España)
# ==========================================

_MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
           "noviembre", "diciembre"]
_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_ACCENTS = str.maketrans("áéíóúüñ", "aeiouun")  # Para comparar «mañana», «sábado»... sin tildes ni eñes
_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}|\d{2}))?\b")
_TIME = re.compile(r"\b(\d{1,2})(?:[:.h](\d{2}))?\s*h?\b")


def now_spain() -> datetime:
    return datetime.now(SPAIN_TZ)


def describe(moment: datetime) -> str:
    """«sábado 25 de octubre, 22:00» (hora de España)."""
    local = moment.astimezone(SPAIN_TZ)
    return f"{_WEEKDAYS[local.weekday()]} {local.day} de {_MONTHS[local.month - 1]}, {local:%H:%M}"


def parse_when(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Fecha y hora a partir de lo que se escribe: «sábado 22:00», «mañana 21:30», «25/10 22:00»,
    «hoy a las 20», «22:00» (la próxima vez que sea esa hora)... Hora de España. None si falta
    la hora o no se entiende.
    """
    now = now or now_spain()
    raw = (text or "").strip()
    if not raw:
        return None
    try:  # El valor que pone la lista que sale al escribir
        moment = datetime.fromisoformat(raw)
        return moment if moment.tzinfo else moment.replace(tzinfo=SPAIN_TZ)
    except ValueError:
        pass

    plain = raw.lower().translate(_ACCENTS)
    day: Optional[date] = None
    weekday: Optional[int] = None
    found = _DATE.search(plain)
    if found:
        year = int(found[3]) if found[3] else now.year
        year += 2000 if year < 100 else 0
        try:
            day = date(year, int(found[2]), int(found[1]))
        except ValueError:
            return None
        if not found[3] and day < now.date():
            day = day.replace(year=year + 1)  # «25/01» dicho en diciembre: el año que viene
        plain = plain[:found.start()] + " " + plain[found.end():]
    else:
        # El día de la semana va antes que «mañana», que también puede ser «por la mañana»
        weekday = next((i for i, name in enumerate(_WEEKDAYS)
                        if re.search(rf"\b{name.translate(_ACCENTS)}\b", plain)), None)
        if weekday is not None:
            day = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
        elif "pasado manana" in plain:
            day = now.date() + timedelta(days=2)
        elif "manana" in plain:
            day = now.date() + timedelta(days=1)
        elif "hoy" in plain:
            day = now.date()

    clock_match = _TIME.search(plain)
    if clock_match is None:
        return None
    hour, minute = int(clock_match[1]), int(clock_match[2] or 0)
    if hour > 23 or minute > 59:
        return None
    moment = datetime.combine(day or now.date(), time(hour, minute), tzinfo=SPAIN_TZ)
    if moment <= now and (day is None or weekday is not None):
        moment += timedelta(days=7 if weekday is not None else 1)  # Ya ha pasado: la próxima vez
    return moment


# ==========================================
# TARJETAS Y MENSAJES
# ==========================================

def icon_for(role: Optional[discord.Role]) -> str:
    """El emoji del nombre del rol (🐊 en «SCP 🐊»), o 📅 si no tiene."""
    for char in role.name if role else "":
        if ord(char) >= 0x1F300 or 0x2600 <= ord(char) <= 0x27BF:
            return char
    return "📅"


def _vote_lines(event: Event) -> str:
    votes = event_votes(event.id)
    lines = []
    for key, (emoji, _, label) in VOTES.items():
        people = [user_id for user_id, vote in votes.items() if vote == key]
        line = f"{emoji} **{label} ({len(people)})**"
        if people:
            line += ": " + " · ".join(f"<@{user_id}>" for user_id in people)
        lines.append(line)
    return "\n".join(lines)


def event_card(event: Event, guild: Optional[discord.Guild]) -> tarjetas.Card:
    """La tarjeta del evento: cuándo, quién va y los botones para votar (mientras no empiece)."""
    role = guild.get_role(event.role_id) if guild and event.role_id else None
    title = escape_markdown(event.titulo)
    voice = f"\n🔊 <#{event.voice_channel_id}>" if event.voice_channel_id else ""
    if event.estado == "cancelado":
        colour = tarjetas.DANGER
        head = f"## ❌ ~~{title}~~\nCancelado" + (f": {escape_markdown(event.motivo)}" if event.motivo else ".")
    elif event.estado == "empezado":
        colour = tarjetas.SUCCESS
        head = f"## {icon_for(role)} {title}\n▶️ Empezó <t:{event.inicio}:R>{voice}"
    else:
        colour = role.colour if role is not None and role.colour.value else tarjetas.INFO
        head = f"## {icon_for(role)} {title}\n🗓️ <t:{event.inicio}:F> (<t:{event.inicio}:R>){voice}"
    card = tarjetas.Card(colour)
    card.text(head)
    if event.descripcion:
        card.text(event.descripcion)
    card.separator()
    card.text(_vote_lines(event))
    if event.role_id:
        role_text = role.mention if role is not None else "un rol que ya no existe"
        card.note(f"Para {role_text} · lo organiza <@{event.organizer_id}>")
    else:
        card.note(f"Lo organiza <@{event.organizer_id}>")
    if event.estado == "programado":
        card.buttons(*(VoteButton(event.id, key) for key in VOTES))
    return card


def _dm_card(colour: discord.Colour, text: str, note: str, url: str, label: str) -> tarjetas.Card:
    card = tarjetas.Card(colour).text(text)
    card.note(note)
    card.buttons(ui.Button(style=discord.ButtonStyle.link, url=url, label=label, emoji="📅"))
    return card


def invite_dm(event: Event, guild: discord.Guild, role: discord.Role) -> tarjetas.Card:
    """DM a los que tienen el rol cuando se crea el evento."""
    text = (f"### {icon_for(role)} Nuevo evento: {escape_markdown(event.titulo)}\n"
            f"🗓️ <t:{event.inicio}:F> (<t:{event.inicio}:R>)")
    if event.descripcion:
        text += f"\n{event.descripcion}"
    note = (f"En {escape_markdown(guild.name)}, para quienes tenéis el rol {escape_markdown(role.name)}. "
            f"Vota si vas en la tarjeta del evento.")
    return _dm_card(role.colour if role.colour.value else tarjetas.INFO, text, note, event.jump_url, "Votar en el servidor")


def start_dm(event: Event, guild: discord.Guild, role: Optional[discord.Role], vote: str) -> tarjetas.Card:
    """DM a quien votó Voy o Puede cuando empieza el evento."""
    greeting = ("Dijiste que venías: te estamos esperando." if vote == "si"
                else "Dijiste que a lo mejor te apuntabas: aún estás a tiempo.")
    text = f"### {icon_for(role)} ¡Empieza {escape_markdown(event.titulo)}!\n{greeting}"
    if event.voice_channel_id:
        url = f"https://discord.com/channels/{event.guild_id}/{event.voice_channel_id}"
        return _dm_card(tarjetas.SUCCESS, text, f"En {escape_markdown(guild.name)}.", url, "Entrar al canal de voz")
    return _dm_card(tarjetas.SUCCESS, text, f"En {escape_markdown(guild.name)}.", event.jump_url, "Ir al servidor")


def cancel_dm(event: Event, guild: discord.Guild) -> tarjetas.Card:
    """DM a quien votó Voy o Puede cuando se cancela el evento."""
    text = f"### ❌ Se ha cancelado {escape_markdown(event.titulo)}\n"
    text += f"Motivo: {escape_markdown(event.motivo)}" if event.motivo else f"Estaba previsto para el <t:{event.inicio}:F>."
    return _dm_card(tarjetas.DANGER, text, f"En {escape_markdown(guild.name)}.", event.jump_url, "Ver el evento")


async def send_dms(members: List[discord.Member], build: Callable[[discord.Member], tarjetas.Card]) -> Tuple[int, int]:
    """Manda un DM a cada uno, con una pausa entre ellos. Devuelve (enviados, fallidos: DM cerrados)."""
    sent = failed = 0
    for member in members:
        try:
            await member.send(view=build(member))
            sent += 1
        except discord.HTTPException:
            failed += 1
        await asyncio.sleep(DM_PAUSE)
    return sent, failed


# ==========================================
# BOTONES DE VOTO
# ==========================================

class VoteButton(ui.DynamicItem[ui.Button], template=r"reimbou:evento:(?P<id>\d+):(?P<vote>si|puede|no)"):
    """✅ Voy · 💭 Puede · ❌ No voy (persistentes: siguen funcionando tras reiniciar el bot)."""

    def __init__(self, event_id: int, vote: str):
        emoji, label, _ = VOTES[vote]
        style = {"si": discord.ButtonStyle.success, "puede": discord.ButtonStyle.primary}.get(vote,
                                                                                           discord.ButtonStyle.secondary)
        super().__init__(ui.Button(emoji=emoji, label=label, style=style, custom_id=f"reimbou:evento:{event_id}:{vote}"))
        self.event_id, self.vote = event_id, vote

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(int(match["id"]), match["vote"])

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._vote)

    async def _vote(self, interaction: discord.Interaction):
        event = get_event(self.event_id)
        if event is None:
            return await interaction.response.send_message("⌛ Este evento ya no existe.", ephemeral=True)
        if event.estado != "programado":
            return await interaction.response.send_message("⌛ La votación de este evento ya está cerrada.",
                                                           ephemeral=True)
        toggle_vote(event.id, interaction.user.id, self.vote)  # Pulsar otra vez tu voto lo quita
        await tarjetas.update(interaction, event_card(get_event(event.id), interaction.guild))


# ==========================================
# COMANDOS Y RELOJ
# ==========================================

def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


def event_choices(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """Los eventos que aún no han empezado, para elegirlos de la lista que sale al escribir."""
    text = (current or "").casefold().strip()
    choices = []
    for event in upcoming_events(interaction.guild_id):
        if text and text not in event.titulo.casefold():
            continue
        moment = datetime.fromtimestamp(event.inicio, SPAIN_TZ)
        choices.append(app_commands.Choice(name=f"{event.titulo} · {describe(moment)}"[:100], value=f"#{event.id}"))
    return choices[:25]


def resolve_event(interaction: discord.Interaction, value: str) -> Union[Event, str]:
    """El evento elegido (de la lista o escrito a mano), o el motivo por el que no se puede usar."""
    value = (value or "").strip()
    upcoming = upcoming_events(interaction.guild_id)
    if value.startswith("#") and value[1:].isdigit():
        event = next((e for e in upcoming if e.id == int(value[1:])), None)
    else:
        matches = [e for e in upcoming if e.titulo.casefold() == value.casefold()]
        if len(matches) > 1:
            return "❌ Hay varios eventos con ese nombre: elígelo de la lista que sale al escribir."
        event = matches[0] if matches else None
    if event is None:
        return "❌ No encuentro ese evento entre los que aún no han empezado: elígelo de la lista que sale al escribir."
    return event


class EventosCog(commands.Cog):
    """/setup-eventos, /evento crear · empezar · cancelar, los votos y el reloj que los empieza a su hora."""

    evento = app_commands.Group(name="evento", description="Eventos para un rol: crear, empezar y cancelar (solo admins)",
                                guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        init_db()
        self.bot.add_dynamic_items(VoteButton)
        self.starter.start()

    async def cog_unload(self):
        self.starter.cancel()

    # ---------- Empezar y cancelar ----------

    async def _refresh_card(self, guild: discord.Guild, event: Event):
        channel = guild.get_channel(event.channel_id)
        if channel is None or event.message_id is None:
            return
        try:
            await channel.get_partial_message(event.message_id).edit(view=event_card(event, guild),
                                                                     allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            logger.warning(f"Eventos: no pude actualizar la tarjeta de «{event.titulo}»: {e}")

    async def _notice(self, guild: discord.Guild, event: Event, text: str, role: Optional[discord.Role] = None):
        """Aviso en el canal del evento, como respuesta a su tarjeta (mencionando al rol si se pasa)."""
        channel = guild.get_channel(event.channel_id)
        if channel is None:
            return
        reference = None
        if event.message_id is not None:
            reference = discord.MessageReference(message_id=event.message_id, channel_id=event.channel_id,
                                                 guild_id=event.guild_id, fail_if_not_exists=False)
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else False)
        try:
            await channel.send(text, reference=reference, mention_author=False, allowed_mentions=mentions)
        except discord.HTTPException as e:
            logger.warning(f"Eventos: no pude avisar en #{channel} de «{event.titulo}»: {e}")

    def _voters(self, guild: discord.Guild, event: Event) -> Tuple[List[discord.Member], Dict[int, str]]:
        """Quienes votaron Voy o Puede (y siguen en el servidor), con su voto."""
        votes = {user_id: vote for user_id, vote in event_votes(event.id).items() if vote in ("si", "puede")}
        return [m for m in map(guild.get_member, votes) if m is not None and not m.bot], votes

    async def start_event(self, event: Event, *, quiet: bool = False, now: Optional[int] = None) -> bool:
        """
        Empieza el evento: cierra la votación, menciona al rol en el canal y avisa por DM a quienes
        votaron Voy o Puede. Con `quiet` (el bot estaba apagado a la hora) solo cierra la votación.
        """
        if not set_state(event.id, "empezado", inicio=now):
            return False  # Ya había empezado o se había cancelado
        event = get_event(event.id)
        guild = self.bot.get_guild(event.guild_id)
        if guild is None:
            return True
        await self._refresh_card(guild, event)
        if quiet:
            logger.warning(f"Eventos: «{event.titulo}» tocaba antes de que el bot volviera; se cierra sin avisar.")
            return True
        role = guild.get_role(event.role_id) if event.role_id else None
        voice = guild.get_channel(event.voice_channel_id) if event.voice_channel_id else None
        text = f"{icon_for(role)} ¡Empieza **{escape_markdown(event.titulo)}**!"
        text += f" {role.mention}" if role is not None else ""
        text += f"\n🔊 {voice.mention}" if voice is not None else ""
        await self._notice(guild, event, text, role)
        await self._discord_event(guild, event, voice)
        members, votes = self._voters(guild, event)
        await send_dms(members, lambda member: start_dm(event, guild, role, votes[member.id]))
        return True

    async def _discord_event(self, guild: discord.Guild, event: Event, voice: Optional[discord.abc.GuildChannel]):
        """
        Evento de Discord en directo mientras dura: sale en el icono del servidor. Se crea al empezar y se
        inicia en el acto. Con canal de voz, Discord lo termina cuando el canal se vacía; si no, a las
        DISCORD_EVENT_HOURS horas.
        """
        start = discord.utils.utcnow() + timedelta(minutes=1)  # Discord no deja crearlo con la hora ya pasada
        description = (f"{event.descripcion}\n\n" if event.descripcion else "") + f"Tarjeta del evento: {event.jump_url}"
        options = dict(name=event.titulo[:100], description=description[:1000], start_time=start,
                       privacy_level=discord.PrivacyLevel.guild_only)
        if voice is not None:
            options.update(entity_type=discord.EntityType.voice, channel=voice)
        else:
            channel = guild.get_channel(event.channel_id)
            options.update(entity_type=discord.EntityType.external, location=f"#{channel.name}" if channel else "Discord",
                           end_time=start + timedelta(hours=DISCORD_EVENT_HOURS))
        try:
            scheduled = await guild.create_scheduled_event(**options, reason="Empieza el evento")
            await scheduled.start(reason="Empieza el evento")
        except discord.HTTPException as e:
            logger.warning(f"Eventos: no pude crear el evento de Discord de «{event.titulo}» "
                           f"(¿le falta al bot el permiso «Crear eventos»?): {e}")

    async def cancel_event(self, event: Event, motivo: str = "") -> bool:
        if not set_state(event.id, "cancelado", motivo=motivo):
            return False
        event = get_event(event.id)
        guild = self.bot.get_guild(event.guild_id)
        if guild is None:
            return True
        await self._refresh_card(guild, event)
        text = f"❌ Se ha cancelado **{escape_markdown(event.titulo)}**."
        if motivo:
            text += f"\n-# Motivo: {escape_markdown(motivo)}"
        await self._notice(guild, event, text)
        members, _ = self._voters(guild, event)
        await send_dms(members, lambda member: cancel_dm(event, guild))
        return True

    @tasks.loop(seconds=30)
    async def starter(self):
        """Empieza los eventos a su hora."""
        try:
            now = int(clock.time())
            for event in due_events(now):
                await self.start_event(event, quiet=now - event.inicio > START_GRACE)
        except Exception:
            logger.exception("Eventos: error al empezar los eventos")  # El reloj sigue

    @starter.before_loop
    async def before_starter(self):
        await self.bot.wait_until_ready()

    # ---------- Comandos ----------

    @app_commands.command(name="setup-eventos", description="Canal donde salen los eventos (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de los eventos (vacío = ver la configuración)")
    async def setup_eventos(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "❌ Necesitas el permiso **Gestionar servidor** para usar `/setup-eventos`.", ephemeral=True
            )
        if canal is None:
            channel_id = get_events_channel(interaction.guild_id)
            status = (f"Los eventos salen en <#{channel_id}>." if channel_id
                      else "Aún no hay canal: elígelo con /setup-eventos canal:#canal.")
            card = tarjetas.Card(tarjetas.INFO).text(f"### ⚙️ Eventos\n{status}")
            card.note(f"Crea uno con {tarjetas.cmd('evento crear')}.")
            return await interaction.response.send_message(view=card, ephemeral=True)
        perms = canal.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(
                f"⚠️ No puedo escribir en {canal.mention}: dame **Ver canal** y **Enviar mensajes** ahí.", ephemeral=True
            )
        set_events_channel(interaction.guild_id, canal.id)
        await interaction.response.send_message(
            f"✅ Los eventos saldrán en {canal.mention}.\n-# Crea uno con {tarjetas.cmd('evento crear')}."
        )

    @evento.command(name="crear", description="Crea un evento: tarjeta para votar y aviso a los del rol (si eliges uno)")
    @app_commands.describe(titulo="Nombre del evento", cuando="Día y hora: «sábado 22:00», «mañana 21:30» o «25/10 22:00»",
                           rol="Rol al que va dirigido: les llega un DM y se le menciona al empezar (opcional)",
                           canal_voz="Canal de voz del evento: Discord lo termina cuando se vacía (opcional)",
                           descripcion="Detalles del evento (opcional)")
    async def crear(self, interaction: discord.Interaction, titulo: app_commands.Range[str, 1, 80], cuando: str,
                    rol: Optional[discord.Role] = None, canal_voz: Optional[discord.VoiceChannel] = None,
                    descripcion: Optional[app_commands.Range[str, 1, 500]] = None):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "❌ Necesitas el permiso **Gestionar servidor** para crear eventos.", ephemeral=True
            )
        guild = interaction.guild
        channel_id = get_events_channel(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return await interaction.response.send_message(
                f"❌ Primero elige el canal de los eventos con {tarjetas.cmd('setup-eventos')}.", ephemeral=True
            )
        if rol is not None and (rol.is_default() or rol.managed):
            return await interaction.response.send_message(
                "❌ Elige un rol normal: ni @everyone ni el rol propio de un bot.", ephemeral=True
            )
        now = now_spain()
        moment = parse_when(cuando, now)
        if moment is None:
            return await interaction.response.send_message(
                "❌ No entiendo cuándo es. Escríbelo como «sábado 22:00», «mañana 21:30» o «25/10 22:00».", ephemeral=True
            )
        if moment <= now:
            return await interaction.response.send_message(f"❌ Esa hora ya ha pasado ({describe(moment)}).",
                                                           ephemeral=True)
        if moment > now + timedelta(days=365):
            return await interaction.response.send_message("❌ Como mucho, con un año de antelación.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        event_id = create_event(guild.id, channel.id, " ".join(titulo.split()), (descripcion or "").strip(),
                                rol.id if rol else NO_ROLE, interaction.user.id, int(moment.timestamp()),
                                canal_voz.id if canal_voz else None)
        message = await channel.send(view=event_card(get_event(event_id), guild), allowed_mentions=NO_MENTIONS)
        set_message(event_id, message.id)
        event = get_event(event_id)

        lines = [f"✅ Evento creado en {channel.mention} para el {describe(moment)}."]
        if rol is not None:
            members = [member for member in rol.members if not member.bot]
            sent, failed = await send_dms(members, lambda member: invite_dm(event, guild, rol))
            lines.append(f"📨 DM a **{sent}** de {len(members)} con el rol {rol.mention}"
                         + (f" ({failed} {'tiene' if failed == 1 else 'tienen'} los DM cerrados)." if failed else "."))
        else:
            lines.append("Sin rol: no se ha mandado ningún DM y al empezar no se mencionará a nadie "
                         "(a quienes voten Voy o Puede sí les llegará su DM).")
        if not (guild.me.guild_permissions.create_events or guild.me.guild_permissions.manage_events):
            lines.append("⚠️ Para que al empezar salga el evento de Discord (y el aviso en el icono del servidor), "
                         "dale al bot el permiso «Crear eventos».")
        if canal_voz is not None:
            voice_perms = canal_voz.permissions_for(guild.me)
            if not (voice_perms.view_channel and voice_perms.connect):
                lines.append(f"⚠️ El bot necesita ver {canal_voz.mention} y poder conectarse para crear ahí el evento "
                             f"de Discord.")
        perms = channel.permissions_for(guild.me)
        if rol is not None and not (rol.mentionable or perms.mention_everyone):
            lines.append(f"⚠️ Al empezar, la mención a {rol.mention} **no avisaría**: en Ajustes del servidor → Roles, "
                         f"activa «Permitir que cualquiera @mencione este rol» (o dale al bot «Mencionar @everyone, "
                         f"@here y todos los roles»).")
        await interaction.followup.send("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @crear.autocomplete("cuando")
    async def _cuando(self, interaction: discord.Interaction, current: str):
        now = now_spain()
        moment = parse_when(current, now)
        if moment is None:
            return [app_commands.Choice(name="Escribe día y hora: «sábado 22:00», «mañana 21:30», «25/10 22:00»",
                                        value=(current or "?")[:100])]
        if moment <= now:
            return [app_commands.Choice(name=f"⚠️ Ya ha pasado: {describe(moment)}", value=current[:100])]
        return [app_commands.Choice(name=f"🗓️ {describe(moment)}", value=moment.isoformat(timespec="minutes"))]

    @evento.command(name="empezar", description="Empieza ya un evento: menciona al rol y avisa a los que van")
    @app_commands.describe(evento="El evento")
    async def empezar(self, interaction: discord.Interaction, evento: str):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "❌ Necesitas el permiso **Gestionar servidor** para empezar eventos.", ephemeral=True
            )
        found = resolve_event(interaction, evento)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.start_event(found, now=min(found.inicio, int(clock.time())))
        await interaction.followup.send(f"▶️ Empezado: **{escape_markdown(found.titulo)}**.", ephemeral=True)

    @evento.command(name="cancelar", description="Cancela un evento (la tarjeta se queda con el resumen)")
    @app_commands.describe(evento="El evento", motivo="Por qué se cancela (opcional)")
    async def cancelar(self, interaction: discord.Interaction, evento: str,
                       motivo: Optional[app_commands.Range[str, 1, 200]] = None):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "❌ Necesitas el permiso **Gestionar servidor** para cancelar eventos.", ephemeral=True
            )
        found = resolve_event(interaction, evento)
        if isinstance(found, str):
            return await interaction.response.send_message(found, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.cancel_event(found, (motivo or "").strip())
        await interaction.followup.send(f"❌ Cancelado: **{escape_markdown(found.titulo)}**.", ephemeral=True)

    @empezar.autocomplete("evento")
    async def _empezar_eventos(self, interaction: discord.Interaction, current: str):
        return event_choices(interaction, current)

    @cancelar.autocomplete("evento")
    async def _cancelar_eventos(self, interaction: discord.Interaction, current: str):
        return event_choices(interaction, current)
