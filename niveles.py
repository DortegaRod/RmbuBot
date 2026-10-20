"""
Niveles: experiencia por escribir y por estar en voz, con roles de recompensa.
Sustituye al sistema de Amari con sus mismos ajustes, para que los niveles importados no cambien:

- Mensajes: 1 de experiencia por mensaje (por el multiplicador del canal), como mucho uno cada 10 s.
- Voz: 5 de experiencia por cada 5 minutos en un canal de voz. Cuenta silenciado y estando solo;
  no cuenta ensordecido ni en el canal AFK.
- Nivel N con 20·N² − 40·N + 55 de experiencia total (la fórmula de Amari).
- Roles de recompensa acumulativos y aviso de subida de nivel en el canal configurado.

Solo cuenta en los servidores donde un admin lo activa con /setup-niveles.
"""

import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import escape_markdown

import db

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


def _fmt(number: int) -> str:
    """68565 -> '68.565'."""
    return f"{number:,}".replace(",", ".")


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


def add_xp(guild_id: int, user_id: int, amount: int) -> int:
    """
    Suma experiencia y devuelve el total nuevo. La suma y la lectura van en la misma transacción,
    así que es correcto aunque lleguen dos a la vez (mensaje y voz).
    """
    with db.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO niveles_xp (guild_id, user_id, xp) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp",
            (guild_id, user_id, amount)
        )
        row = conn.execute("SELECT xp FROM niveles_xp WHERE guild_id = ? AND user_id = ?",
                           (guild_id, user_id)).fetchone()
    return row[0]


def set_xp(guild_id: int, user_id: int, xp: int):
    with db.get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO niveles_xp (guild_id, user_id, xp) VALUES (?, ?, ?)",
                     (guild_id, user_id, xp))


def get_xp(guild_id: int, user_id: int) -> int:
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT xp FROM niveles_xp WHERE guild_id = ? AND user_id = ?",
                           (guild_id, user_id)).fetchone()
    return row[0] if row else 0


def ranking_rows(guild_id: int) -> List[Tuple[int, int]]:
    """(user_id, xp) de mayor a menor."""
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


def match_members(members: List[discord.Member], entries: List[Tuple[str, int, int]]) -> Tuple[Dict[int, int], List[str], int]:
    """
    Asocia cada nombre de la clasificación con un miembro del servidor (por usuario,
    nombre global o apodo). Devuelve ({user_id: experiencia}, problemas, nº de «Unknown»).
    """
    index: Dict[str, Dict[int, discord.Member]] = defaultdict(dict)
    for member in members:
        if member.bot:
            continue
        for name in (member.name, getattr(member, "global_name", None), member.display_name):
            if name:
                index[name.casefold()][member.id] = member

    found: Dict[int, Dict[int, str]] = defaultdict(dict)  # user_id -> {experiencia: nombre}
    problems: List[str] = []
    unknown = 0
    for name, xp, level in entries:
        if name.casefold() == "unknown":
            unknown += 1  # Gente que ya no está en el servidor
            continue
        if level_for_xp(xp) != level:
            problems.append(f"«{name}»: {_fmt(xp)} de experiencia no da nivel {level}; revísalo")
            continue
        candidates = index.get(name.casefold(), {})
        if len(candidates) == 1:
            found[next(iter(candidates))][xp] = name
        elif not candidates:
            problems.append(f"«{name}» ({_fmt(xp)} exp): no lo encuentro en el servidor")
        else:
            problems.append(f"«{name}» ({_fmt(xp)} exp): hay {len(candidates)} personas con ese nombre")

    resolved: Dict[int, int] = {}
    for user_id, by_xp in found.items():
        if len(by_xp) == 1:
            resolved[user_id] = next(iter(by_xp))  # Si sale repetido con lo mismo, se importa una vez
        else:
            amounts = " y ".join(_fmt(xp) for xp in sorted(by_xp, reverse=True))
            problems.append(f"«{next(iter(by_xp.values()))}» sale varias veces ({amounts} exp) y solo "
                            f"encuentro a una persona con ese nombre")
    return resolved, problems, unknown


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
        self.voice_xp.start()

    async def cog_unload(self):
        self.voice_xp.cancel()

    def reload_settings(self):
        self.channels, self.multipliers, self.rewards = load_settings()

    # ---------- Experiencia ----------

    async def award(self, guild: discord.Guild, member: discord.Member, amount: int, announce: bool = True):
        """Suma experiencia y, si sube de nivel, da las recompensas y lo anuncia."""
        new_xp = await asyncio.to_thread(add_xp, guild.id, member.id, amount)
        old_level, new_level = level_for_xp(new_xp - amount), level_for_xp(new_xp)
        if new_level > old_level:
            roles = await self.sync_rewards(member, new_level)
            if announce:
                await self.announce_level_up(guild, member, new_level, roles)

    async def sync_rewards(self, member: discord.Member, level: int) -> List[discord.Role]:
        """Da los roles de recompensa que le correspondan y aún no tenga (se acumulan, nunca se quitan)."""
        guild = member.guild
        wanted = [guild.get_role(role_id)
                  for nivel, role_ids in self.rewards.get(guild.id, {}).items() if nivel <= level
                  for role_id in role_ids]
        missing = [role for role in wanted if role is not None and role not in member.roles]
        if not missing:
            return []
        try:
            await member.add_roles(*missing, reason=f"Recompensa por llegar al nivel {level}")
            return missing
        except discord.Forbidden:
            logger.warning("Niveles: sin permiso para dar roles de recompensa. Dale al bot «Gestionar roles» "
                           "y pon su rol por encima de los de recompensa.")
        except discord.HTTPException as e:
            logger.warning(f"Niveles: no pude dar roles a {member}: {e}")
        return []

    async def announce_level_up(self, guild: discord.Guild, member: discord.Member, level: int,
                                roles: List[discord.Role]):
        channel = guild.get_channel(self.channels.get(guild.id, 0))
        if channel is None:
            return
        text = f"🎉 ¡{member.mention} ha subido al **nivel {level}**!"
        for role in roles:
            text += f"\n🏆 ¡y consigue el rol **{escape_markdown(role.name)}**!"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                              users=[member]))
        except discord.HTTPException as e:
            logger.warning(f"Niveles: no pude anunciar la subida de nivel en #{channel}: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.guild.id not in self.channels:
            return
        if message.author.bot or message.webhook_id or message.is_system():
            return
        key = (message.guild.id, message.author.id)
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
                        key = (guild.id, member.id)
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

    def _ranked_members(self, guild: discord.Guild) -> List[Tuple[discord.Member, int]]:
        """Clasificación con solo la gente que sigue en el servidor."""
        rows = []
        for user_id, xp in ranking_rows(guild.id):
            member = guild.get_member(user_id)
            if member is not None and not member.bot:
                rows.append((member, xp))
        return rows

    @app_commands.command(name="nivel", description="Muestra el nivel y la experiencia de alguien")
    @app_commands.guild_only()
    @app_commands.describe(usuario="De quién ver el nivel (por defecto, el tuyo)")
    async def nivel(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        member = usuario or interaction.user
        if interaction.guild_id not in self.channels:
            return await interaction.response.send_message(
                "📈 Los niveles no están activados en este servidor (un admin puede hacerlo con `/setup-niveles`).",
                ephemeral=True
            )
        xp = get_xp(interaction.guild_id, member.id)
        level = level_for_xp(xp)
        current, following = total_xp_for_level(level), total_xp_for_level(level + 1)
        position = next((i for i, (m, _) in enumerate(self._ranked_members(interaction.guild), 1)
                         if m.id == member.id), None)

        embed = discord.Embed(title=f"📈 {member.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Nivel", value=f"**{level}**")
        embed.add_field(name="Puesto", value=f"**#{position}**" if position else "—")
        embed.add_field(name="Experiencia", value=_fmt(xp))
        embed.add_field(
            name=f"Hasta el nivel {level + 1}",
            value=f"`{_progress_bar((xp - current) / (following - current))}` "
                  f"{_fmt(xp - current)} / {_fmt(following - current)}",
            inline=False
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ranking", description="Clasificación de niveles del servidor")
    @app_commands.guild_only()
    @app_commands.describe(pagina="Página de la clasificación (10 por página)")
    async def ranking(self, interaction: discord.Interaction, pagina: app_commands.Range[int, 1, 1000] = 1):
        if interaction.guild_id not in self.channels:
            return await interaction.response.send_message(
                "📈 Los niveles no están activados en este servidor.", ephemeral=True
            )
        rows = self._ranked_members(interaction.guild)
        pages = max(1, math.ceil(len(rows) / RANKING_PAGE_SIZE))
        pagina = min(pagina, pages)
        start = (pagina - 1) * RANKING_PAGE_SIZE
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{medals.get(pos, f'`{pos}.`')} {member.mention} · nivel **{level_for_xp(xp)}** · {_fmt(xp)} exp"
                 for pos, (member, xp) in enumerate(rows[start:start + RANKING_PAGE_SIZE], start + 1)]
        embed = discord.Embed(title="🏆 Ranking de niveles", description="\n".join(lines) or "Aún no hay nadie.",
                              color=discord.Color.gold())
        embed.set_footer(text=f"Página {pagina} de {pages}")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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
            return await interaction.response.send_message(embed=self._settings_embed(guild), ephemeral=True,
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

    def _settings_embed(self, guild: discord.Guild) -> discord.Embed:
        channel_id = self.channels.get(guild.id)
        embed = discord.Embed(title="📈 Configuración de niveles", color=discord.Color.blurple())
        embed.add_field(name="Estado", value=f"Activo · avisos en <#{channel_id}>" if channel_id
                        else "Desactivado (`/setup-niveles canal:#canal`)", inline=False)
        embed.add_field(name="Experiencia",
                        value=f"{MESSAGE_XP} por mensaje (uno cada {MESSAGE_COOLDOWN} s)\n"
                              f"{VOICE_RATE * VOICE_INTERVAL} cada {VOICE_INTERVAL} min en voz "
                              f"(no cuenta ensordecido ni el canal AFK)", inline=False)
        multipliers = self.multipliers.get(guild.id, {})
        embed.add_field(name="Multiplicadores",
                        value="\n".join(f"<#{c}> ×{v}" for c, v in multipliers.items()) or "Ninguno", inline=False)
        rewards = self.rewards.get(guild.id, {})
        embed.add_field(name="Recompensas (se acumulan)",
                        value="\n".join(f"Nivel {n}: " + ", ".join(f"<@&{r}>" for r in roles)
                                        for n, roles in sorted(rewards.items()) if roles) or "Ninguna", inline=False)
        return embed

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
        given = 0
        for user_id, xp in await asyncio.to_thread(ranking_rows, guild.id):
            if level_for_xp(xp) < nivel:
                break  # Están ordenados de mayor a menor experiencia
            member = guild.get_member(user_id)
            if member is not None and rol not in member.roles:
                try:
                    await member.add_roles(rol, reason=f"Recompensa por tener el nivel {nivel}")
                    given += 1
                except discord.HTTPException as e:
                    logger.warning(f"Niveles: no pude dar {rol.name} a {member}: {e}")
        if given:
            text += f"\n🏆 Se lo he dado ya a **{given}** {'persona' if given == 1 else 'personas'} con ese nivel."
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="niveles-ajustar",
                          description="Pone a alguien una experiencia o un nivel exactos (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(usuario="A quién", exp="Experiencia total exacta",
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
        await asyncio.to_thread(set_xp, interaction.guild_id, usuario.id, xp)
        await self.sync_rewards(usuario, level_for_xp(xp))
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
        resolved, problems, unknown = match_members(guild.members, entries)
        for user_id, xp in resolved.items():
            await asyncio.to_thread(set_xp, guild.id, user_id, xp)
            member = guild.get_member(user_id)
            if member is not None:
                await self.sync_rewards(member, level_for_xp(xp))

        summary = (f"✅ **{len(resolved)}** personas importadas con su experiencia exacta de Amari.\n"
                   f"👻 {unknown} «Unknown» saltados (ya no están en el servidor).")
        if problems:
            listing = "\n".join(f"• {p}" for p in problems)
            if len(listing) > 1500:
                listing = listing[:1500].rsplit("\n", 1)[0] + "\n• …"
            summary += (f"\n\n⚠️ **{len(problems)} sin resolver**: ajústalos con "
                        f"`/niveles-ajustar usuario:@persona exp:N`\n{listing}")
        await interaction.followup.send(summary, ephemeral=True)
