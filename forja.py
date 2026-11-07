"""
Forja (/craftear): 100 de cobre hacen 1 de plata y 100 de plata, 1 de oro (monedas.py). También se puede
fundir una moneda y recuperar 100 de la anterior.

La forja es una tarjeta que solo ve quien la abre (con /craftear o con el botón ⚒️ Forja de su perfil): su
bolsa y una fila de botones por receta. Cada botón cambia las monedas de quien lo pulsa y redibuja la forja.
Los botones son persistentes: siguen funcionando aunque el bot se reinicie.
"""

import asyncio
from typing import Optional, Union

import discord
from discord import app_commands, ui
from discord.ext import commands

import cuentas
import monedas
import tarjetas
from monedas import LOWER, RATE, coin
from tarjetas import NO_MENTIONS

FORGE_COLOUR = discord.Colour.dark_orange()
CRAFT_AMOUNTS = (1, 10)  # Botones de cantidad fija de cada receta (además de «Todo»)


def forge_card(purse: monedas.Purse, result: Optional[str] = None) -> tarjetas.Card:
    """La forja: la bolsa, lo último que se ha hecho y los botones de cada receta (los imposibles, apagados)."""
    card = tarjetas.Card(FORGE_COLOUR)
    card.text(f"## ⚒️ Forja\n{tarjetas.emoji('bolsa')} {monedas.format_purse(purse)}")
    if result:
        card.text(result)
    for target in ("plata", "oro"):
        source = LOWER[target]
        possible = purse.get(source) // RATE
        card.separator()
        card.text(f"**{tarjetas.emoji(target)} {target.capitalize()}** · {RATE} {tarjetas.emoji(source)} → "
                  f"1 {tarjetas.emoji(target)}\n-# Puedes craftear {monedas.fmt_number(possible)}")
        card.buttons(*(ForgeButton("craft", target, amount, label=f"×{amount}", disabled=possible < amount)
                       for amount in CRAFT_AMOUNTS),
                     ForgeButton("craft", target, "max", label="Todo", disabled=possible < 1,
                                 style=discord.ButtonStyle.primary))
    card.separator()
    card.text(f"**🔥 Fundir** · 1 → {RATE} de la moneda anterior")
    card.buttons(*(ForgeButton("melt", source, 1, label=f"Fundir 1 de {source}", emoji=tarjetas.emoji(source),
                               disabled=purse.get(source) < 1)
                   for source in ("plata", "oro")))
    card.note("Solo tú ves tu forja.")
    return card


async def open_forge(interaction: discord.Interaction):
    """Manda la forja de quien la pide, en privado."""
    if interaction.guild_id is None:
        return await interaction.response.send_message("❌ La forja solo funciona dentro de un servidor.",
                                                       ephemeral=True)
    group = cuentas.group_of(interaction.guild_id, interaction.user.id)
    purse = await asyncio.to_thread(monedas.person_purse, interaction.guild_id, group)
    await interaction.response.send_message(view=forge_card(purse), ephemeral=True, allowed_mentions=NO_MENTIONS)


class ForgeButton(ui.DynamicItem[ui.Button],
                  template=r"reimbou:forja:(?P<action>craft|melt):(?P<coin>plata|oro):(?P<amount>\d+|max)"):
    """Un botón de la forja: craftear o fundir una cantidad de una moneda."""

    def __init__(self, action: str, coin_name: str, amount: Union[int, str], *, label: Optional[str] = None,
                 emoji: Optional[str] = None, disabled: bool = False,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(ui.Button(label=label, emoji=emoji, disabled=disabled, style=style,
                                   custom_id=f"reimbou:forja:{action}:{coin_name}:{amount}"))
        self.action, self.coin_name, self.amount = action, coin_name, amount

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        amount = match["amount"]
        return cls(match["action"], match["coin"], amount if amount == "max" else int(amount))

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._exchange)

    async def _exchange(self, interaction: discord.Interaction):
        group = cuentas.group_of(interaction.guild_id, interaction.user.id)
        count = None if self.amount == "max" else self.amount
        lower = LOWER[self.coin_name]
        if self.action == "craft":
            purse, done = await asyncio.to_thread(monedas.craft, interaction.guild_id, group, self.coin_name, count)
            result = (f"✅ Has crafteado {coin(self.coin_name, done)} con {coin(lower, done * RATE)}." if done
                      else f"❌ No te llega: cada una cuesta {coin(lower, RATE)}.")
        else:
            purse, done = await asyncio.to_thread(monedas.melt, interaction.guild_id, group, self.coin_name, count)
            result = (f"🔥 Has fundido {coin(self.coin_name, done)} en {coin(lower, done * RATE)}." if done
                      else f"❌ No te queda {self.coin_name} que fundir.")
        await tarjetas.update(interaction, forge_card(purse, result))


class OpenForgeButton(ui.DynamicItem[ui.Button], template=r"reimbou:forja:abrir"):
    """El botón ⚒️ Forja del perfil: abre la forja de quien lo pulsa (no la del dueño del perfil)."""

    def __init__(self):
        super().__init__(ui.Button(label="Forja", emoji="⚒️", custom_id="reimbou:forja:abrir"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, open_forge)


class ForjaCog(commands.Cog):
    """/craftear y los botones de la forja."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        monedas.init_db()
        self.bot.add_dynamic_items(ForgeButton, OpenForgeButton)

    @app_commands.command(name="craftear", description="Tu forja: craftea plata y oro con tus monedas, o fúndelas")
    @app_commands.guild_only()
    async def craftear(self, interaction: discord.Interaction):
        await open_forge(interaction)
