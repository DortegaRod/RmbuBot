"""
Niveles: experiencia por escribir y por estar en voz, con roles de recompensa.
Sustituye al sistema de Amari con sus mismos ajustes, para que los niveles importados no cambien:

- Mensajes: 1 de experiencia por mensaje (por el multiplicador del canal), como mucho uno cada 10 s.
- Voz: 5 de experiencia por cada 5 minutos en un canal de voz. Cuenta silenciado y estando solo;
  no cuenta ensordecido ni en el canal AFK.
- Nivel N con 20·N² − 40·N + 55 de experiencia total (la fórmula de Amari).
- Roles de recompensa acumulativos y aviso de subida de nivel en el canal configurado.
- Todo va por persona: las cuentas vinculadas (cuentas.py) suman su experiencia y comparten
  la espera entre mensajes y el tiempo en voz (dos cuentas a la vez no dan el doble).

Solo cuenta en los servidores donde un admin lo activa con /setup-niveles.
"""

import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import escape_markdown

import cuentas
import db
import tarjetas

logger = logging.getLogger(__name__)

# Ajustes copiados de Amari en el servidor de Reimbou
MESSAGE_XP = 1          # Experiencia por mensaje
MESSAGE_COOLDOWN = 10   # Segundos mínimos entre mensajes que dan experiencia
VOICE_RATE = 1          # Amari da VOICE_RATE × VOICE_INTERVAL cada intervalo en voz
VOICE_INTERVAL = 5      # Minutos
VOICE_MIN_USERS = 1     # Personas mínimas en el canal (1 = también estando solo)
GLOBAL_MULTIPLIER = 1

RANKING_PAGE_SIZE = 10
MAX_IMPORT_BYTES = 1_000_000
LINKS_FILE = Path(__file__).parent / "datos" / "cuentas_vinculadas.txt"


def _fmt(number: int) -> str:
    """68565 -> '68.565'."""
    return f"{number:,}".replace(",", ".")


def _clip_lines(text: str, limit: int) -> str:
    """Corta un listado por la última línea entera que quepa en `limit` caracteres (contando el «…» final)."""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 2)].rsplit("\n", 1)[0] + "\n…"


# ==========================================
# FÓRMULA (la de Amari)
# ==========================================

def total_xp_for_level(level: int) -> int:
    """Experiencia total para tener `level`: 20·N² − 40·N + 55 (nivel 0 = 0)."""
    return 0 if level <= 0 else 20 * level * level - 40 * level + 55


def level_for_xp(xp: int) -> int:
    """Nivel que corresponde a una experiencia total."""
    if xp < total_xp_for_level(1):
        return 0
    # total(N) = 20·(N−1)² + 35  →  N = 1 + √((xp − 35) / 20)
    level = int(1 + math.sqrt((xp - 35) / 20))
    while total_xp_for_level(level + 1) <= xp:  # Corrige el redondeo de la raíz
        level += 1
    while level > 0 and total_xp_for_level(level) > xp:
        level -= 1
    return level


# ==========================================
# BASE DE DATOS
# ==========================================

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS niveles_xp (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS niveles_config (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS niveles_multiplicadores (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        valor INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    )""",
    """CREATE TABLE IF NOT EXISTS niveles_recompensas (
        guild_id INTEGER NOT NULL,
        nivel INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, nivel, role_id)
    )""",
)


def init_db():
    with db.get_db_connection() as conn:
        for sql in _SCHEMA:
            conn.execute(sql)


def _sum_xp(conn, guild_id: int, group: List[int]) -> int:
    placeholders = ",".join("?" * len(group))
    row = conn.execute(f"SELECT COALESCE(SUM(xp), 0) FROM niveles_xp WHERE guild_id = ? AND user_id IN ({placeholders})",
                       (guild_id, *group)).fetchone()
    return row[0]


def add_xp(guild_id: int, user_id: int, amount: int, group: List[int]) -> int:
    """
    Suma experiencia a una cuenta y devuelve el total de la persona (todas sus cuentas).
    La suma y la lectura van en la misma transacción: es correcto aunque lleguen dos a la vez.
    """
    with db.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO niveles_xp (guild_id, user_id, xp) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp",
            (guild_id, user_id, amount)
        )
        return _sum_xp(conn, guild_id, group)


def person_xp(guild_id: int, group: List[int]) -> int:
    """Experiencia total de una persona (suma de sus cuentas)."""
    with db.get_db_connection() as conn:
        return _sum_xp(conn, guild_id, group)


def set_person_xp(guild_id: int, group: List[int], xp: int):
    """Pone a una persona una experiencia total exacta: toda en la cuenta principal, el resto a 0."""
    with db.get_db_connection() as conn:
        for index, user_id in enumerate(group):
            conn.execute("INSERT OR REPLACE INTO niveles_xp (guild_id, user_id, xp) VALUES (?, ?, ?)",
                         (guild_id, user_id, xp if index == 0 else 0))


def ranking_rows(guild_id: int) -> List[Tuple[int, int]]:
    """(user_id, xp) de cada cuenta, de mayor a menor."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT user_id, xp FROM niveles_xp WHERE guild_id = ? AND xp > 0 "
                            "ORDER BY xp DESC, user_id", (guild_id,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def load_settings() -> Tuple[Dict[int, int], Dict[int, Dict[int, int]], Dict[int, Dict[int, List[int]]]]:
    """Canales de aviso, multiplicadores y recompensas de todos los servidores."""
    channels: Dict[int, int] = {}
    multipliers: Dict[int, Dict[int, int]] = defaultdict(dict)
    rewards: Dict[int, Dict[int, List[int]]] = defaultdict(lambda: defaultdict(list))
    with db.get_db_connection() as conn:
        for guild_id, channel_id in conn.execute("SELECT guild_id, channel_id FROM niveles_config"):
            channels[guild_id] = channel_id
        for guild_id, channel_id, valor in conn.execute(
                "SELECT guild_id, channel_id, valor FROM niveles_multiplicadores"):
            multipliers[guild_id][channel_id] = valor
        for guild_id, nivel, role_id in conn.execute(
                "SELECT guild_id, nivel, role_id FROM niveles_recompensas ORDER BY nivel"):
            rewards[guild_id][nivel].append(role_id)
    return channels, multipliers, rewards


def save_channel(guild_id: int, channel_id: Optional[int]):
    with db.get_db_connection() as conn:
        if channel_id is None:
            conn.execute("DELETE FROM niveles_config WHERE guild_id = ?", (guild_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO niveles_config (guild_id, channel_id) VALUES (?, ?)",
                         (guild_id, channel_id))


def save_multiplier(guild_id: int, channel_id: int, valor: int):
    with db.get_db_connection() as conn:
        if valor == 1:
            conn.execute("DELETE FROM niveles_multiplicadores WHERE guild_id = ? AND channel_id = ?",
                         (guild_id, channel_id))
        else:
            conn.execute("INSERT OR REPLACE INTO niveles_multiplicadores (guild_id, channel_id, valor) "
                         "VALUES (?, ?, ?)", (guild_id, channel_id, valor))


def save_reward(guild_id: int, nivel: int, role_id: int, remove: bool = False):
    with db.get_db_connection() as conn:
        if remove:
            conn.execute("DELETE FROM niveles_recompensas WHERE guild_id = ? AND nivel = ? AND role_id = ?",
                         (guild_id, nivel, role_id))
        else:
            conn.execute("INSERT OR IGNORE INTO niveles_recompensas (guild_id, nivel, role_id) VALUES (?, ?, ?)",
                         (guild_id, nivel, role_id))


# ==========================================
# IMPORTAR LA CLASIFICACIÓN DE AMARI
# ==========================================

_NUMBER = re.compile(r"[\d.,]+")
_ONE_LINE = re.compile(r"#?\d+[\s.)]+(.+?)\s+([\d.,]+)\s+(\d+)")


def _to_int(text: str) -> int:
    return int(re.sub(r"[.,]", "", text))


def parse_amari_leaderboard(text: str) -> List[Tuple[str, int, int]]:
    """
    Lee la clasificación copiada de la web de Amari: bloques «#1 / avatar / Nombre / 68,565 / 59».
    También acepta una persona por línea: «#1 Nombre 68565 59».
    Devuelve (nombre, experiencia, nivel).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    entries = []
    for i, line in enumerate(lines):
        if re.fullmatch(r"#\d+", line):
            rest = [l for l in lines[i + 1:i + 6] if l.lower() != "avatar"][:3]
            if len(rest) == 3 and _NUMBER.fullmatch(rest[1]) and rest[2].isdigit():
                entries.append((rest[0], _to_int(rest[1]), int(rest[2])))
            continue
        match = _ONE_LINE.fullmatch(line)
        if match:
            entries.append((match.group(1).strip(), _to_int(match.group(2)), int(match.group(3))))
    return entries


def read_link_groups() -> List[str]:
    """Grupos de datos/cuentas_vinculadas.txt (una persona por línea)."""
    try:
        lines = LINKS_FILE.read_text(encoding="utf-8-sig").splitlines()  # -sig: por si el Bloc de notas pone BOM
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def plan_import(guild_id: int, members: List[discord.Member], entries: List[Tuple[str, int, int]],
                groups: List[str] = ()) -> Tuple[Dict[int, int], List[str], int]:
    """
    Reparte la clasificación entre las personas del servidor, buscando cada nombre por usuario,
    nombre global o apodo y sumando las cuentas vinculadas.
    Los grupos confirmados (`groups`, las líneas de datos/cuentas_vinculadas.txt) mandan: todas las
    entradas con sus nombres son de esa persona, aunque alguna de sus cuentas ya no esté en el servidor.
    Devuelve ({cuenta principal: experiencia total}, problemas, nº de «Unknown»).
    """
    index: Dict[str, Dict[int, discord.Member]] = defaultdict(dict)
    for member in members:
        if member.bot:
            continue
        for name in (member.name, getattr(member, "global_name", None), member.display_name):
            if name:
                index[name.casefold()][member.id] = member

    confirmed: Dict[str, int] = {}  # Nombre de un grupo confirmado -> su persona
    unmatched: Dict[str, str] = {}  # Nombre de un grupo del que no hay ninguna cuenta aquí -> línea del grupo
    for line in groups:
        names = [n.strip().casefold() for n in line.split("|") if n.strip()]
        accounts = [user_id for name in names for user_id in sorted(index.get(name, {}))]
        for name in names:
            if accounts:
                confirmed[name] = cuentas.principal_of(guild_id, accounts[0])
            else:
                unmatched[name] = line

    found: Dict[int, Dict[Tuple[str, int], str]] = defaultdict(dict)  # principal -> {(nombre, exp): nombre original}
    trusted = set()  # Personas de un grupo confirmado: no se comprueba cuántas cuentas tienen
    missing: Dict[str, Dict[Tuple[str, int], int]] = defaultdict(dict)  # Grupo sin cuentas -> sus entradas
    problems: List[Tuple[int, str]] = []  # (experiencia en juego, texto): primero lo que más importa
    unknown = 0
    for name, xp, level in entries:
        key = name.casefold()
        shown = escape_markdown(name)  # Los _ de los nombres no deben volverse cursiva
        if key == "unknown":
            unknown += 1  # Gente que ya no está en el servidor
            continue
        if level_for_xp(xp) != level:
            problems.append((xp, f"«{shown}»: {_fmt(xp)} de experiencia no da nivel {level}; revísalo"))
            continue
        # Repetido con la misma experiencia = la misma entrada (pasa al cambiar de página en la web)
        if key in confirmed:
            found[confirmed[key]][(key, xp)] = name
            trusted.add(confirmed[key])
            continue
        if key in unmatched:
            missing[unmatched[key]][(key, xp)] = xp
            continue
        candidates = index.get(key, {})
        people = {cuentas.principal_of(guild_id, user_id) for user_id in candidates}
        if not people:
            problems.append((xp, f"«{shown}» ({_fmt(xp)} exp): no lo encuentro en el servidor"))
        elif len(people) > 1:
            problems.append((xp, f"«{shown}» ({_fmt(xp)} exp): hay {len(people)} personas distintas con ese nombre "
                                 f"(vincúlalas si son la misma)"))
        else:
            found[people.pop()][(key, xp)] = name

    for line, group_entries in missing.items():
        total = sum(group_entries.values())
        problems.append((total, f"«{escape_markdown(line)}» ({_fmt(total)} exp en total): "
                                f"no encuentro ninguna de sus cuentas"))

    totals: Dict[int, int] = {}
    for principal, person_entries in found.items():
        accounts = len(cuentas.group_of(guild_id, principal))
        if principal not in trusted and len(person_entries) > accounts:
            amounts = " y ".join(_fmt(xp) for _, xp in sorted(person_entries, key=lambda e: -e[1]))
            problems.append((sum(xp for _, xp in person_entries),
                             f"«{escape_markdown(next(iter(person_entries.values())))}» sale {len(person_entries)} "
                             f"veces ({amounts} exp) pero aquí tiene {accounts} "
                             f"{'cuenta' if accounts == 1 else 'cuentas'}; si las otras son suyas, vincúlalas"))
            continue
        totals[principal] = sum(xp for _, xp in person_entries)
    problems.sort(key=lambda problem: -problem[0])  # Si hay que recortar la lista, se ve lo que más importa
    return totals, [text for _, text in problems], unknown


# ==========================================
# COMANDOS Y CONTADORES
# ==========================================

def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


async def _deny_non_admin(interaction: discord.Interaction, command: str) -> bool:
    if _is_admin(interaction):
        return False
    await interaction.response.send_message(
        f"❌ Necesitas el permiso **Gestionar servidor** para usar `{command}`.", ephemeral=True
    )
    return True


def _progress_bar(fraction: float, length: int = 14) -> str:
    filled = max(0, min(length, round(fraction * length)))
    return "█" * filled + "░" * (length - filled)


class NivelesCog(commands.Cog):
    """Experiencia por mensajes y voz, avisos de subida de nivel y roles de recompensa."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channels: Dict[int, int] = {}                       # Servidor -> canal de avisos (activo)
        self.multipliers: Dict[int, Dict[int, int]] = {}         # Servidor -> {canal: multiplicador}
        self.rewards: Dict[int, Dict[int, List[int]]] = {}       # Servidor -> {nivel: [roles]}
        self.cooldowns: Dict[Tuple[int, int], float] = {}        # (servidor, persona) -> último mensaje que contó
        self.voice_minutes: Dict[Tuple[int, int], int] = {}      # (servidor, persona) -> minutos acumulados

    async def cog_load(self):
        init_db()
        self.reload_settings()
        cuentas.on_linked = self.on_linked
        tarjetas.register_pages("ranking", self._render_ranking)  # Flechas y «Mi puesto» del /ranking
        self.voice_xp.start()

    async def cog_unload(self):
        self.voice_xp.cancel()
        cuentas.on_linked = None

    def reload_settings(self):
        self.channels, self.multipliers, self.rewards = load_settings()

    # ---------- Experiencia ----------

    async def award(self, guild: discord.Guild, member: discord.Member, amount: int):
        """Suma experiencia y, si la persona sube de nivel, le da las recompensas y lo anuncia."""
        group = cuentas.group_of(guild.id, member.id)
        new_total = await asyncio.to_thread(add_xp, guild.id, member.id, amount, group)
        old_level, new_level = level_for_xp(new_total - amount), level_for_xp(new_total)
        if new_level > old_level:
            roles = await self.sync_person(guild, member.id, new_level)
            await self.announce_level_up(guild, member, old_level, new_level, new_total, roles)

    async def sync_person(self, guild: discord.Guild, user_id: int, level: Optional[int] = None) -> List[discord.Role]:
        """
        Da los roles de recompensa que le correspondan a la persona en todas sus cuentas
        (se acumulan, nunca se quitan). Devuelve los roles que se han dado ahora.
        """
        group = cuentas.group_of(guild.id, user_id)
        if level is None:
            level = level_for_xp(await asyncio.to_thread(person_xp, guild.id, group))
        wanted = [guild.get_role(role_id)
                  for nivel, role_ids in self.rewards.get(guild.id, {}).items() if nivel <= level
                  for role_id in role_ids]
        given: List[discord.Role] = []
        for account_id in group:
            member = guild.get_member(account_id)
            missing = [role for role in wanted if role is not None and member is not None and role not in member.roles]
            if not missing:
                continue
            try:
                await member.add_roles(*missing, reason=f"Recompensa por llegar al nivel {level}")
                given += [role for role in missing if role not in given]
            except discord.Forbidden:
                logger.warning("Niveles: sin permiso para dar roles de recompensa. Dale al bot «Gestionar roles» "
                               "y pon su rol por encima de los de recompensa.")
            except discord.HTTPException as e:
                logger.warning(f"Niveles: no pude dar roles a {member}: {e}")
        return given

    async def on_linked(self, guild: discord.Guild, principal_id: int):
        """Al vincular cuentas se suma su experiencia: puede tocarle alguna recompensa (sin aviso)."""
        await self.sync_person(guild, principal_id)

    def level_up_card(self, guild: discord.Guild, member: discord.Member, old_level: int, level: int,
                      xp: int, roles: List[discord.Role]) -> tarjetas.Card:
        """
        Aviso de subida de nivel: su avatar al lado, barra hasta el siguiente nivel y puesto, en el
        color de su rol. Si trae un rol de recompensa, dorada y con el rol en el título.
        """
        current, following = total_xp_for_level(level), total_xp_for_level(level + 1)
        people = self._ranked_people(guild)
        index = self._position_of(people, guild.id, member.id)
        if roles:
            names = " y ".join(f"**{escape_markdown(role.name)}**" for role in roles)
            card = tarjetas.Card(tarjetas.RANKING)
            heading = f"## 🏆 ¡Nivel {level} y rol nuevo!\n{member.mention} consigue {names}"
        else:
            card = tarjetas.Card(member.colour if member.colour.value else tarjetas.LEVELS)
            heading = f"## 🎉 ¡Nivel {level}!\n{member.mention} sube del {old_level} al {level}"
        card.section(heading, member.display_avatar.url, f"Avatar de {member.display_name}")
        card.text(f"`{_progress_bar((xp - current) / (following - current))}` "
                  f"{_fmt(xp - current)} / {_fmt(following - current)} hasta el nivel {level + 1}")
        card.separator()
        rank = f"🏅 Puesto #{index + 1} de {len(people)} · " if index is not None else ""
        card.note(f"{rank}{_fmt(xp)} de experiencia")
        return card

    async def announce_level_up(self, guild: discord.Guild, member: discord.Member, old_level: int, level: int,
                                xp: int, roles: List[discord.Role]):
        channel = guild.get_channel(self.channels.get(guild.id, 0))
        if channel is None:
            return
        try:
            card = self.level_up_card(guild, member, old_level, level, xp, roles)
            # La mención va dentro de la tarjeta y avisa solo a esa persona
            await channel.send(view=card, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                                   users=[member]))
        except discord.HTTPException as e:
            logger.warning(f"Niveles: no pude anunciar la subida de nivel en #{channel}: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.guild.id not in self.channels:
            return
        if message.author.bot or message.webhook_id or message.is_system():
            return
        # La espera es por persona: escribir con dos cuentas a la vez no da el doble
        key = (message.guild.id, cuentas.principal_of(message.guild.id, message.author.id))
        now = time.monotonic()
        if now - self.cooldowns.get(key, float("-inf")) < MESSAGE_COOLDOWN:
            return
        self.cooldowns[key] = now

        # En los hilos vale el multiplicador del canal al que pertenecen
        channel = message.channel
        multipliers = self.multipliers.get(message.guild.id, {})
        multiplier = multipliers.get(channel.id) or multipliers.get(getattr(channel, "parent_id", None)) or 1
        await self.award(message.guild, message.author, MESSAGE_XP * GLOBAL_MULTIPLIER * multiplier)

    @tasks.loop(minutes=1)
    async def voice_xp(self):
        """Cada minuto suma un minuto a quien esté en voz; cada VOICE_INTERVAL minutos le da la experiencia."""
        try:
            for guild_id in list(self.channels):
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                present = set()
                for channel in [*guild.voice_channels, *guild.stage_channels]:
                    if guild.afk_channel is not None and channel.id == guild.afk_channel.id:
                        continue
                    humans = [m for m in channel.members if not m.bot]
                    if len(humans) < VOICE_MIN_USERS:
                        continue
                    for member in humans:
                        voice = member.voice
                        if voice is None or voice.self_deaf or voice.deaf:
                            continue  # Ensordecido no cuenta (silenciado sí)
                        key = (guild.id, cuentas.principal_of(guild.id, member.id))
                        if key in present:
                            continue  # Otra cuenta de la misma persona ya ha contado este minuto
                        present.add(key)
                        minutes = self.voice_minutes.get(key, 0) + 1
                        if minutes >= VOICE_INTERVAL:
                            minutes = 0
                            await self.award(guild, member, VOICE_RATE * VOICE_INTERVAL)
                        self.voice_minutes[key] = minutes
                # Quien sale de voz pierde los minutos que no llegaron a un intervalo completo
                for key in [k for k in self.voice_minutes if k[0] == guild.id and k not in present]:
                    del self.voice_minutes[key]
        except Exception:
            logger.exception("Niveles: error al dar la experiencia de voz")  # El bucle sigue

    @voice_xp.before_loop
    async def before_voice_xp(self):
        await self.bot.wait_until_ready()

    # ---------- Comandos para todos ----------

    def _ranked_people(self, guild: discord.Guild) -> List[Tuple[discord.Member, int, int]]:
        """Clasificación por persona: (cuenta que se muestra, experiencia total, nº de cuentas)."""
        totals: Dict[int, int] = defaultdict(int)
        for user_id, xp in ranking_rows(guild.id):
            totals[cuentas.principal_of(guild.id, user_id)] += xp
        people = []
        for principal, total in totals.items():
            group = cuentas.group_of(guild.id, principal)
            present = [m for m in map(guild.get_member, group) if m is not None and not m.bot]
            if present and total > 0:
                people.append((present[0], total, len(group)))
        people.sort(key=lambda person: (-person[1], person[0].id))
        return people

    def _position_of(self, people: List[Tuple[discord.Member, int, int]], guild_id: int, user_id: int) -> Optional[int]:
        """Puesto (desde 0) de la persona a la que pertenece `user_id`, o None si no está en el ranking."""
        person = cuentas.principal_of(guild_id, user_id)
        return next((i for i, (shown, _, _) in enumerate(people) if cuentas.principal_of(guild_id, shown.id) == person),
                    None)

    def nivel_card(self, guild: discord.Guild, member: discord.Member) -> tarjetas.Card:
        group = cuentas.group_of(guild.id, member.id)
        xp = person_xp(guild.id, group)
        level = level_for_xp(xp)
        current, following = total_xp_for_level(level), total_xp_for_level(level + 1)
        index = self._position_of(self._ranked_people(guild), guild.id, member.id)
        position = f" · puesto **#{index + 1}**" if index is not None else ""

        card = tarjetas.Card(tarjetas.LEVELS)
        card.section(f"## 📈 {escape_markdown(member.display_name)}\n**Nivel {level}**{position}\n"
                     f"{_fmt(xp)} de experiencia", member.display_avatar.url, f"Avatar de {member.display_name}")
        card.text(f"`{_progress_bar((xp - current) / (following - current))}` "
                  f"{_fmt(xp - current)} / {_fmt(following - current)} hasta el nivel {level + 1}")
        if len(group) > 1:
            card.note("🔗 Cuentas vinculadas: " + " · ".join(f"<@{uid}>" for uid in group))
        return card

    @app_commands.command(name="nivel", description="Muestra el nivel y la experiencia de alguien")
    @app_commands.guild_only()
    @app_commands.describe(usuario="De quién ver el nivel (por defecto, el tuyo)")
    async def nivel(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        if interaction.guild_id not in self.channels:
            return await interaction.response.send_message(
                f"📈 Los niveles no están activados en este servidor (un admin puede hacerlo con "
                f"{tarjetas.cmd('setup-niveles')}).", ephemeral=True
            )
        card = self.nivel_card(interaction.guild, usuario or interaction.user)
        await interaction.response.send_message(view=card, allowed_mentions=discord.AllowedMentions.none())

    def ranking_card(self, guild: discord.Guild, owner_id: int, page: Union[int, str]) -> Union[tarjetas.Card, str]:
        """
        Una página del ranking. `owner_id` es quien la ve (su fila lleva 👈); `page` puede ser «yo»:
        la página donde está esa persona.
        """
        people = self._ranked_people(guild)
        pages = max(1, math.ceil(len(people) / RANKING_PAGE_SIZE))
        mine = self._position_of(people, guild.id, owner_id)
        if page == "yo":
            if mine is None:
                return "📍 Aún no apareces en el ranking: escribe o entra en un canal de voz para ganar experiencia."
            page = mine // RANKING_PAGE_SIZE + 1
        page = tarjetas.clamp_page(page, pages)
        start = (page - 1) * RANKING_PAGE_SIZE

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for pos, (member, xp, accounts) in enumerate(people[start:start + RANKING_PAGE_SIZE], start + 1):
            linked = f" · 🔗 {accounts} cuentas" if accounts > 1 else ""
            you = " 👈" if pos - 1 == mine else ""
            lines.append(f"{medals.get(pos, f'`{pos}.`')} {member.mention} · nivel **{level_for_xp(xp)}** · "
                         f"{_fmt(xp)} exp{linked}{you}")

        card = tarjetas.Card(tarjetas.RANKING)
        card.text("## 🏆 Ranking de niveles")
        card.text("\n".join(lines) or "Aún no hay nadie.")
        if people:
            card.separator()
            count = f"{len(people)} {'persona' if len(people) == 1 else 'personas'}"
            card.note(f"Página {page} de {pages} · {count}" if pages > 1 else count)
        extra = [tarjetas.PageButton("ranking", owner_id, "yo", emoji="📍", label="Mi puesto")] if pages > 1 else []
        card.buttons(*tarjetas.page_controls("ranking", owner_id, page, pages, *extra))
        return card

    async def _render_ranking(self, interaction: discord.Interaction, owner_id: int, page: Union[int, str]):
        if interaction.guild_id not in self.channels:
            return "📈 Los niveles no están activados en este servidor."
        return self.ranking_card(interaction.guild, owner_id, page)

    @app_commands.command(name="ranking", description="Clasificación de niveles del servidor")
    @app_commands.guild_only()
    @app_commands.describe(pagina="Página de la clasificación (10 por página)")
    async def ranking(self, interaction: discord.Interaction, pagina: app_commands.Range[int, 1, 1000] = 1):
        if interaction.guild_id not in self.channels:
            return await interaction.response.send_message(
                "📈 Los niveles no están activados en este servidor.", ephemeral=True
            )
        card = self.ranking_card(interaction.guild, interaction.user.id, pagina)
        await interaction.response.send_message(view=card, allowed_mentions=discord.AllowedMentions.none())

    # ---------- Comandos de admins ----------

    @app_commands.command(name="setup-niveles", description="Activa los niveles y elige el canal de avisos (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal donde se anuncian las subidas de nivel (vacío = ver la configuración)",
                           quitar="Deja de contar experiencia en este servidor (no se borra la que hay)")
    async def setup_niveles(self, interaction: discord.Interaction,
                            canal: Optional[Union[discord.TextChannel, discord.VoiceChannel]] = None,
                            quitar: bool = False):
        if await _deny_non_admin(interaction, "/setup-niveles"):
            return
        guild = interaction.guild
        if quitar:
            await asyncio.to_thread(save_channel, guild.id, None)
            self.reload_settings()
            return await interaction.response.send_message(
                "✅ Niveles desactivados: ya no se cuenta experiencia. La que había se conserva."
            )
        if canal is None:
            return await interaction.response.send_message(view=self._settings_card(guild), ephemeral=True,
                                                           allowed_mentions=discord.AllowedMentions.none())
        perms = canal.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(
                f"⚠️ No puedo escribir en {canal.mention}: dame **Ver canal** y **Enviar mensajes** ahí.",
                ephemeral=True
            )
        await asyncio.to_thread(save_channel, guild.id, canal.id)
        self.reload_settings()
        text = f"✅ Niveles activados. Las subidas de nivel se anunciarán en {canal.mention}."
        if self.rewards.get(guild.id) and not guild.me.guild_permissions.manage_roles:
            text += "\n⚠️ Para dar los roles de recompensa, el bot necesita el permiso **Gestionar roles**."
        await interaction.response.send_message(text)

    def _settings_card(self, guild: discord.Guild) -> tarjetas.Card:
        channel_id = self.channels.get(guild.id)
        state = (f"✅ Activos · las subidas se anuncian en <#{channel_id}>" if channel_id
                 else f"⛔ Desactivados · actívalos con {tarjetas.cmd('setup-niveles')} y un canal")
        multipliers = self.multipliers.get(guild.id, {})
        rewards = self.rewards.get(guild.id, {})
        card = tarjetas.Card(tarjetas.LEVELS)
        card.text(f"## ⚙️ Configuración de niveles\n{state}")
        card.text(f"**Experiencia:** {MESSAGE_XP} por mensaje (uno cada {MESSAGE_COOLDOWN} s) · "
                  f"{VOICE_RATE * VOICE_INTERVAL} cada {VOICE_INTERVAL} min en voz")
        card.note("En voz no cuenta estar ensordecido ni el canal AFK.")
        card.separator()
        card.text("**Multiplicadores**\n" + ("\n".join(f"<#{c}> ×{v}" for c, v in multipliers.items()) or "Ninguno"))
        card.text("**Recompensas** (se acumulan)\n" + ("\n".join(
            f"Nivel {n}: " + ", ".join(f"<@&{r}>" for r in roles) for n, roles in sorted(rewards.items()) if roles
        ) or "Ninguna"))
        return card

    @app_commands.command(name="niveles-multiplicador",
                          description="Multiplica la experiencia de los mensajes en un canal (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal", valor="Multiplicador (1 = quitarlo)")
    async def multiplicador(self, interaction: discord.Interaction,
                            canal: Union[discord.TextChannel, discord.VoiceChannel],
                            valor: app_commands.Range[int, 1, 100]):
        if await _deny_non_admin(interaction, "/niveles-multiplicador"):
            return
        await asyncio.to_thread(save_multiplier, interaction.guild_id, canal.id, valor)
        self.reload_settings()
        await interaction.response.send_message(
            f"✅ Los mensajes en {canal.mention} dan ahora **×{valor}** de experiencia." if valor > 1
            else f"✅ {canal.mention} ya no tiene multiplicador."
        )

    @app_commands.command(name="niveles-recompensa",
                          description="Añade o quita un rol de recompensa por nivel (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(nivel="Nivel al que se da el rol", rol="Rol de recompensa",
                           quitar="Quita esta recompensa (no le quita el rol a quien ya lo tenga)")
    async def recompensa(self, interaction: discord.Interaction, nivel: app_commands.Range[int, 1, 1000],
                         rol: discord.Role, quitar: bool = False):
        if await _deny_non_admin(interaction, "/niveles-recompensa"):
            return
        guild = interaction.guild
        if rol.is_default() or rol.managed:
            return await interaction.response.send_message(
                "❌ Elige un rol normal: ni @everyone ni el rol propio de un bot.", ephemeral=True
            )
        await asyncio.to_thread(save_reward, guild.id, nivel, rol.id, quitar)
        self.reload_settings()
        if quitar:
            return await interaction.response.send_message(
                f"✅ El nivel {nivel} ya no da {rol.mention}.", allowed_mentions=discord.AllowedMentions.none()
            )
        text = f"✅ Al llegar al **nivel {nivel}** se dará {rol.mention}."
        if not guild.me.guild_permissions.manage_roles:
            text += "\n⚠️ Falta darle al bot el permiso **Gestionar roles**."
            return await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())
        if rol >= guild.me.top_role:
            text += f"\n⚠️ Sube el rol del bot por encima de {rol.mention} en la lista de roles, o no podrá darlo."
            return await interaction.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())

        # Quien ya tenga ese nivel lo recibe ahora, sin esperar a volver a subir
        await interaction.response.defer()
        people = set()
        for shown, xp, _ in self._ranked_people(guild):
            if level_for_xp(xp) < nivel:
                break  # Están ordenados de mayor a menor experiencia
            if rol in await self.sync_person(guild, shown.id):
                people.add(cuentas.principal_of(guild.id, shown.id))
        if people:
            text += f"\n🏆 Se lo he dado ya a **{len(people)}** {'persona' if len(people) == 1 else 'personas'} con ese nivel."
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="niveles-ajustar",
                          description="Pone a alguien una experiencia o un nivel exactos (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(usuario="A quién (si tiene cuentas vinculadas, es el total de la persona)",
                           exp="Experiencia total exacta",
                           nivel="O bien un nivel: se pone la experiencia justa para tenerlo")
    async def ajustar(self, interaction: discord.Interaction, usuario: discord.Member,
                      exp: Optional[app_commands.Range[int, 0, 100_000_000]] = None,
                      nivel: Optional[app_commands.Range[int, 0, 1000]] = None):
        if await _deny_non_admin(interaction, "/niveles-ajustar"):
            return
        if (exp is None) == (nivel is None):
            return await interaction.response.send_message("❌ Indica `exp` **o** `nivel` (uno de los dos).",
                                                           ephemeral=True)
        xp = exp if exp is not None else total_xp_for_level(nivel)
        await asyncio.to_thread(set_person_xp, interaction.guild_id, cuentas.group_of(interaction.guild_id, usuario.id), xp)
        await self.sync_person(interaction.guild, usuario.id, level_for_xp(xp))
        await interaction.response.send_message(
            f"✅ {usuario.mention} tiene ahora **{_fmt(xp)}** de experiencia (nivel **{level_for_xp(xp)}**).",
            allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="niveles-importar",
                          description="Importa la clasificación copiada de la web de Amari (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(archivo="Archivo .txt con la clasificación de Amari copiada tal cual")
    async def importar(self, interaction: discord.Interaction, archivo: discord.Attachment):
        if await _deny_non_admin(interaction, "/niveles-importar"):
            return
        if archivo.size > MAX_IMPORT_BYTES:
            return await interaction.response.send_message("❌ El archivo es demasiado grande.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        text = (await archivo.read()).decode("utf-8", errors="replace")
        entries = parse_amari_leaderboard(text)
        if not entries:
            return await interaction.followup.send(
                "❌ No encuentro ninguna clasificación en ese archivo. Copia la página de Amari tal cual "
                "(«#1», nombre, experiencia y nivel) y guárdala en un .txt.", ephemeral=True
            )
        guild = interaction.guild
        # Primero las cuentas de la misma persona, para que su experiencia se sume
        groups = read_link_groups()
        links_report = cuentas.link_by_names(guild.id, guild.members, groups)
        totals, problems, unknown = plan_import(guild.id, guild.members, entries, groups)
        for principal, xp in totals.items():
            await asyncio.to_thread(set_person_xp, guild.id, cuentas.group_of(guild.id, principal), xp)
            await self.sync_person(guild, principal, level_for_xp(xp))

        card = tarjetas.Card(tarjetas.SUCCESS if not problems else tarjetas.WARNING)
        card.text(f"## 📥 Importación de Amari\n"
                  f"✅ **{len(totals)}** personas importadas con su experiencia exacta.\n"
                  f"👻 {unknown} «Unknown» saltados (ya no están en el servidor).")
        if links_report:
            card.separator()
            card.text("**🔗 Cuentas vinculadas**\n" + _clip_lines("\n".join(links_report), 600))
        if problems:
            card.separator()
            head = f"**⚠️ {len(problems)} sin resolver**\n"
            hint = ("Ajústalos con /niveles-ajustar usuario:@persona exp:N, "
                    "o vincula sus cuentas y vuelve a importar.")
            # Lo que quepa: Discord no admite más de 4000 caracteres de texto por mensaje
            room = tarjetas.MAX_TEXT - card.content_length() - len(head) - len(hint) - 20
            card.text(head + _clip_lines("\n".join(f"• {p}" for p in problems), room))
            card.note(hint)
        await interaction.followup.send(view=card, ephemeral=True)
