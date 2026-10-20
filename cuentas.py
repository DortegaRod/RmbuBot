"""
Cuentas vinculadas: varias cuentas de Discord que son de la misma persona.
Su experiencia se suma como si fueran una sola (y servirá igual para la futura moneda).

- Cada persona tiene una cuenta principal; las demás apuntan a ella.
- Cualquiera puede vincular otra cuenta suya con /vincular, pero la otra cuenta tiene que
  confirmarlo (si no, cualquiera podría quedarse con la experiencia de otra persona).
- Los admins vinculan directamente con /vincular-cuentas.
- /niveles-importar vincula además los grupos de datos/cuentas_vinculadas.txt.
"""

import logging
import time
from typing import Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_markdown

import db
import tarjetas

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
REQUEST_TIMEOUT = 600  # Segundos que tiene la otra cuenta para confirmar

_SCHEMA = """CREATE TABLE IF NOT EXISTS cuentas_vinculadas (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    principal_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
)"""

# Copia en memoria: servidor -> {cuenta secundaria: cuenta principal}
_links: Dict[int, Dict[int, int]] = {}

# Se llama después de vincular (lo registra niveles.py para dar los roles de recompensa)
on_linked: Optional[Callable] = None


def init_db():
    with db.get_db_connection() as conn:
        conn.execute(_SCHEMA)
        rows = conn.execute("SELECT guild_id, user_id, principal_id FROM cuentas_vinculadas").fetchall()
    _links.clear()
    for guild_id, user_id, principal_id in rows:
        _links.setdefault(guild_id, {})[user_id] = principal_id


def principal_of(guild_id: int, user_id: int) -> int:
    """Cuenta principal de la persona a la que pertenece `user_id`."""
    return _links.get(guild_id, {}).get(user_id, user_id)


def group_of(guild_id: int, user_id: int) -> List[int]:
    """Todas las cuentas de la persona: la principal primero y luego las demás, de más antigua a más nueva."""
    principal = principal_of(guild_id, user_id)
    return [principal] + sorted(u for u, p in _links.get(guild_id, {}).items() if p == principal)


def link(guild_id: int, principal_id: int, other_id: int) -> int:
    """Une la persona de `other_id` (con todas sus cuentas) a la de `principal_id`. Devuelve la principal."""
    principal = principal_of(guild_id, principal_id)
    absorbed = [u for u in group_of(guild_id, other_id) if u != principal]
    guild_links = _links.setdefault(guild_id, {})
    with db.get_db_connection() as conn:
        for user_id in absorbed:
            conn.execute("INSERT OR REPLACE INTO cuentas_vinculadas (guild_id, user_id, principal_id) "
                         "VALUES (?, ?, ?)", (guild_id, user_id, principal))
            guild_links[user_id] = principal
    return principal


def unlink(guild_id: int, user_id: int) -> bool:
    """Saca una cuenta de su grupo. Si era la principal, pasa a serlo la más antigua de las demás."""
    group = group_of(guild_id, user_id)
    if len(group) < 2:
        return False
    guild_links = _links.setdefault(guild_id, {})
    with db.get_db_connection() as conn:
        if group[0] == user_id:
            new_principal, others = group[1], group[2:]
            conn.execute("DELETE FROM cuentas_vinculadas WHERE guild_id = ? AND user_id = ?", (guild_id, new_principal))
            guild_links.pop(new_principal, None)
            for other in others:
                conn.execute("UPDATE cuentas_vinculadas SET principal_id = ? WHERE guild_id = ? AND user_id = ?",
                             (new_principal, guild_id, other))
                guild_links[other] = new_principal
        else:
            conn.execute("DELETE FROM cuentas_vinculadas WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            guild_links.pop(user_id, None)
    return True


def link_by_names(guild_id: int, members: List[discord.Member], lines: List[str]) -> List[str]:
    """
    Vincula grupos escritos por nombre («Alvort | AlvortMobile»). Un nombre que tengan varias cuentas
    las vincula todas. La principal es la cuenta más antigua del primer nombre. Devuelve un informe.
    """
    index: Dict[str, List[discord.Member]] = {}
    for member in members:
        if member.bot:
            continue
        for name in {member.name, getattr(member, "global_name", None), member.display_name}:
            if name:
                index.setdefault(name.casefold(), []).append(member)

    report = []
    for line in lines:
        names = [n.strip() for n in line.split("|") if n.strip()]
        accounts: List[discord.Member] = []
        for name in names:
            for member in sorted(index.get(name.casefold(), []), key=lambda m: m.id):
                if member not in accounts:
                    accounts.append(member)
        if len(accounts) < 2:
            found = "solo encuentro 1 cuenta" if accounts else "no encuentro ninguna cuenta"
            report.append(f"«{escape_markdown(line)}»: {found}, no vinculo nada")
            continue
        for member in accounts[1:]:
            link(guild_id, accounts[0].id, member.id)
        report.append(f"🔗 {escape_markdown(names[0])}: {len(accounts)} cuentas")
    return report


# ==========================================
# COMANDOS
# ==========================================

class LinkRequest(tarjetas.Card):
    """Tarjeta que pregunta a la otra cuenta si de verdad es de la misma persona. Solo ella puede responder."""

    def __init__(self, requester: discord.Member, target: discord.Member):
        super().__init__(tarjetas.LEVELS, timeout=REQUEST_TIMEOUT)
        self.requester, self.target = requester, target
        self.message: Optional[discord.Message] = None
        expires = int(time.time()) + REQUEST_TIMEOUT
        self.text(f"{target.mention}\n### 🔗 ¿Es tuya esta cuenta?\n"
                  f"{requester.mention} dice que esta cuenta también es suya. Si es verdad, confírmalo "
                  f"desde aquí y vuestra experiencia se sumará.")
        self.note(f"Caduca <t:{expires}:R>")
        confirm = discord.ui.Button(label="Sí, es mía", emoji="✅", style=discord.ButtonStyle.success)
        reject = discord.ui.Button(label="No", emoji="❌", style=discord.ButtonStyle.secondary)
        confirm.callback, reject.callback = self.confirm, self.reject
        self.buttons(confirm, reject)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(f"Solo {self.target.mention} puede responder a esto.",
                                                    ephemeral=True, allowed_mentions=NO_MENTIONS)
            return False
        return True

    async def confirm(self, interaction: discord.Interaction):
        self.stop()
        principal = link(interaction.guild_id, self.requester.id, self.target.id)
        await tarjetas.update(interaction, tarjetas.simple(
            tarjetas.SUCCESS, f"🔗 Hecho: {self.requester.mention} y {self.target.mention} son la misma persona. "
                              f"Su experiencia se suma."
        ))
        if on_linked is not None:
            await on_linked(interaction.guild, principal)

    async def reject(self, interaction: discord.Interaction):
        self.stop()
        await tarjetas.update(interaction, tarjetas.simple(tarjetas.DANGER, "❌ Vinculación cancelada."))

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=tarjetas.simple(discord.Colour.dark_grey(),
                                                             "⌛ La solicitud de vinculación ha caducado."),
                                        allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                pass


class CuentasCog(commands.Cog):
    """Comandos /vincular, /desvincular y /vincular-cuentas."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        init_db()

    @app_commands.command(name="vincular", description="Vincula otra cuenta tuya: vuestra experiencia se sumará")
    @app_commands.guild_only()
    @app_commands.describe(cuenta="Tu otra cuenta (tendrá que confirmarlo desde ella)")
    async def vincular(self, interaction: discord.Interaction, cuenta: discord.Member):
        if cuenta.bot or cuenta.id == interaction.user.id:
            return await interaction.response.send_message("❌ Elige otra cuenta tuya (no un bot ni esta misma).",
                                                           ephemeral=True)
        if principal_of(interaction.guild_id, cuenta.id) == principal_of(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message("🔗 Esas cuentas ya están vinculadas.", ephemeral=True)
        view = LinkRequest(interaction.user, cuenta)
        # Solo se avisa a la otra cuenta (la mención va en la tarjeta)
        await interaction.response.send_message(
            view=view, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[cuenta])
        )
        view.message = await interaction.original_response()

    @app_commands.command(name="desvincular", description="Separa una cuenta vinculada")
    @app_commands.guild_only()
    @app_commands.describe(cuenta="La cuenta que quieres separar (por defecto, esta)")
    async def desvincular(self, interaction: discord.Interaction, cuenta: Optional[discord.Member] = None):
        target = cuenta or interaction.user
        guild_id = interaction.guild_id
        own = principal_of(guild_id, target.id) == principal_of(guild_id, interaction.user.id)
        if not own and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Solo puedes separar cuentas tuyas.", ephemeral=True)
        if not unlink(guild_id, target.id):
            return await interaction.response.send_message("ℹ️ Esa cuenta no está vinculada a ninguna otra.",
                                                           ephemeral=True)
        await interaction.response.send_message(
            f"✅ {target.mention} ya no está vinculada. Cada cuenta conserva la experiencia que tenga guardada "
            f"(la importada de Amari está en la cuenta principal).", allowed_mentions=NO_MENTIONS
        )

    @app_commands.command(name="vincular-cuentas",
                          description="Vincula cuentas de la misma persona sin pedir confirmación (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(principal="Cuenta principal de la persona", cuenta="Otra cuenta suya",
                           otra_cuenta="Otra más, si tiene tres")
    async def vincular_cuentas(self, interaction: discord.Interaction, principal: discord.Member,
                               cuenta: discord.Member, otra_cuenta: Optional[discord.Member] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ Necesitas el permiso **Gestionar servidor** para usar `/vincular-cuentas`.", ephemeral=True
            )
        others = [m for m in (cuenta, otra_cuenta) if m is not None and m.id != principal.id and not m.bot]
        if not others or principal.bot:
            return await interaction.response.send_message("❌ Indica al menos dos cuentas distintas (que no sean bots).",
                                                           ephemeral=True)
        group_principal = principal.id
        for member in others:
            group_principal = link(interaction.guild_id, principal.id, member.id)
        await interaction.response.send_message(
            f"🔗 Vinculadas como la misma persona: {', '.join(m.mention for m in (principal, *others))}.",
            allowed_mentions=NO_MENTIONS
        )
        if on_linked is not None:
            await on_linked(interaction.guild, group_principal)
