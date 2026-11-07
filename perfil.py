"""
Perfil de personaje (/perfil): título, nivel, puesto, monedas y desde cuándo está la persona en el servidor.

- El título es un rol. Los admins marcan con /titulos qué roles lo son, y sale el más alto (en la lista
  de roles del servidor) de esos que tenga la persona. Sin ninguno, el perfil no lleva título.
- El nivel y el puesto salen si los niveles están activados en el servidor (niveles.py); las monedas,
  siempre (monedas.py).
- Las cuentas vinculadas comparten nivel y monedas; la fecha es la de la que entró antes al servidor.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Dict, Optional, Set

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

import cuentas
import db
import monedas
import niveles
import tarjetas
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)


# ==========================================
# BASE DE DATOS
# ==========================================

def init_db():
    with db.get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS titulos (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, role_id)
        )""")


def load_titles() -> Dict[int, Set[int]]:
    """Servidor -> roles que son títulos."""
    titles: Dict[int, Set[int]] = defaultdict(set)
    with db.get_db_connection() as conn:
        for guild_id, role_id in conn.execute("SELECT guild_id, role_id FROM titulos"):
            titles[guild_id].add(role_id)
    return titles


def save_title(guild_id: int, role_id: int, remove: bool = False):
    with db.get_db_connection() as conn:
        if remove:
            conn.execute("DELETE FROM titulos WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        else:
            conn.execute("INSERT OR IGNORE INTO titulos (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))


# ==========================================
# COG
# ==========================================

class PerfilCog(commands.Cog):
    """/perfil y /titulos."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.titles: Dict[int, Set[int]] = defaultdict(set)  # Servidor -> roles que son títulos

    async def cog_load(self):
        init_db()
        monedas.init_db()
        self.titles = load_titles()

    def title_of(self, member: discord.Member) -> Optional[discord.Role]:
        """El título de alguien: el más alto de sus roles marcados como título."""
        titles = self.titles.get(member.guild.id, set())
        return max((role for role in member.roles if role.id in titles), default=None)

    def levels(self, guild_id: int) -> Optional[niveles.NivelesCog]:
        """Los niveles, si están activados en el servidor."""
        cog = self.bot.get_cog("NivelesCog")
        return cog if cog is not None and guild_id in cog.channels else None

    async def perfil_card(self, member: discord.Member) -> tarjetas.Card:
        guild = member.guild
        group = cuentas.group_of(guild.id, member.id)
        money = await asyncio.to_thread(monedas.person_money, guild.id, group)

        # Tres bloques separados: quién es (junto al avatar), su progreso y su bolsa. Sin emojis de adorno:
        # lo importante en negrita y lo secundario en pequeño (-#)
        card = tarjetas.Card(member.colour if member.colour.value else tarjetas.LEVELS)
        identity = [f"## {escape_markdown(member.display_name)}"]
        title = self.title_of(member)
        if title is not None:
            identity.append(title.mention)
        joined = [account.joined_at for account in map(guild.get_member, group)
                  if account is not None and account.joined_at is not None]
        if joined:
            since = int(min(joined).timestamp())
            identity.append(f"-# En el gremio desde el <t:{since}:D> (<t:{since}:R>)")
        card.section("\n".join(identity), member.display_avatar.url, f"Avatar de {member.display_name}")

        levels = self.levels(guild.id)
        if levels is not None:
            xp = await asyncio.to_thread(niveles.person_xp, guild.id, group)
            index = levels.position_of(levels.ranked_people(guild), guild.id, member.id)
            position = f" · Puesto #{index + 1}" if index is not None else ""
            card.separator()
            # El nivel como subtítulo: es lo que más se mira, que se vea grande
            card.text(f"### Nivel {niveles.level_for_xp(xp)}{position}\n{niveles.level_progress(xp)}\n"
                      f"-# {monedas.fmt_number(xp)} de experiencia en total")

        card.separator()
        # El valor total en oro, para comparar; con oro justo (o nada) repetiría lo mismo
        total = f"  ({monedas.format_total(money)})" if money % monedas.ORO else ""
        card.text(f"**Bolsa**  {monedas.format_money(money)}{total}")
        if len(group) > 1:
            card.note("Cuentas vinculadas: " + " · ".join(f"<@{user_id}>" for user_id in group))
        return card

    @app_commands.command(name="perfil", description="Perfil de personaje: título, nivel, puesto, monedas y antigüedad")
    @app_commands.guild_only()
    @app_commands.describe(usuario="De quién ver el perfil (por defecto, el tuyo)")
    async def perfil(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        card = await self.perfil_card(usuario or interaction.user)
        await interaction.response.send_message(view=card, allowed_mentions=NO_MENTIONS)

    # ---------- Títulos (admins) ----------

    def _titles_card(self, guild: discord.Guild) -> tarjetas.Card:
        roles = sorted(filter(None, map(guild.get_role, self.titles.get(guild.id, set()))), reverse=True)
        card = tarjetas.Card(tarjetas.INFO)
        card.text("### 🏷️ Títulos\n" + ("\n".join(role.mention for role in roles) or "Ningún rol todavía."))
        card.note(f"En el perfil sale el más alto de estos que tenga cada persona (por el orden de la lista de "
                  f"roles del servidor). Añade o quita uno con {tarjetas.cmd('titulos')}.")
        return card

    @app_commands.command(name="titulos", description="Qué roles salen como título en el perfil (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(rol="Rol que pasa a ser un título (vacío = ver la lista)",
                           quitar="Que ese rol deje de ser un título")
    async def titulos(self, interaction: discord.Interaction, rol: Optional[discord.Role] = None,
                      quitar: bool = False):
        guild = interaction.guild
        if rol is None:
            return await interaction.response.send_message(view=self._titles_card(guild), ephemeral=True,
                                                           allowed_mentions=NO_MENTIONS)
        if quitar:
            await asyncio.to_thread(save_title, guild.id, rol.id, True)
            self.titles[guild.id].discard(rol.id)
            return await interaction.response.send_message(f"✅ {rol.mention} ya no es un título.",
                                                           ephemeral=True, allowed_mentions=NO_MENTIONS)
        if rol.is_default() or rol.managed:
            return await interaction.response.send_message(
                "❌ Elige un rol normal: ni @everyone ni el rol propio de un bot.", ephemeral=True
            )
        await asyncio.to_thread(save_title, guild.id, rol.id)
        self.titles[guild.id].add(rol.id)
        await interaction.response.send_message(
            f"✅ {rol.mention} es ahora un título: sale en el perfil de quien lo tenga (si tiene varios títulos, "
            f"el más alto).", ephemeral=True, allowed_mentions=NO_MENTIONS
        )
