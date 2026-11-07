"""
Inventario y forja: el inventario enseña las monedas de cada uno; en la forja (/craftear), 100 de cobre hacen
1 de plata y 100 de plata, 1 de oro (monedas.py), y también se puede fundir una moneda y recuperar 100 de la
anterior.

- Las dos son tarjetas que solo ve quien las abre: con /craftear o con los botones 💰 Inventario y ⚒️ Forja
  de un perfil. Desde una se pasa a la otra en la misma tarjeta.
- Las de otra persona (desde su perfil) solo se miran: no llevan botones para craftear ni fundir.
- Cada receta tiene ×1, ×10, ×100, «Todo» y «✏️ Cantidad», que pide el número exacto en una ventanita.
  Cada botón cambia las monedas de quien lo pulsa y redibuja la forja.
- Los botones son persistentes: siguen funcionando aunque el bot se reinicie.
"""

import asyncio
from typing import Callable, Optional, Union

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.utils import escape_markdown

import cuentas
import monedas
import tarjetas
from monedas import LOWER, RATE, coin
from tarjetas import NO_MENTIONS

FORGE_COLOUR = discord.Colour.dark_orange()
INVENTORY_COLOUR = discord.Colour.dark_gold()
AMOUNTS = (1, 10, 100)  # Botones de cantidad fija de cada receta (además de «Todo» y «✏️ Cantidad»)
MAX_TYPED = 9_999_999   # Lo más que se puede escribir en «✏️ Cantidad»
BAG = "💰"            # Icono del inventario y de la bolsa (emoji normal de Discord)


def _amount_buttons(action: str, coin_name: str, possible: int, emoji: Optional[str] = None) -> list:
    """×1, ×10, ×100, Todo y ✏️ Cantidad de una receta (los que no se pueden usar, apagados)."""
    return ([ForgeButton(action, coin_name, amount, label=f"×{amount}", emoji=emoji, disabled=possible < amount)
             for amount in AMOUNTS]
            + [ForgeButton(action, coin_name, "max", label="Todo", emoji=emoji, disabled=possible < 1,
                           style=discord.ButtonStyle.primary),
               ForgeButton(action, coin_name, "pedir", label="Cantidad", emoji="✏️", disabled=possible < 1)])


def forge_card(purse: monedas.Purse, owner_id: int, result: Optional[str] = None,
               viewer_of: Optional[str] = None) -> tarjetas.Card:
    """
    La forja: la bolsa, lo último que se ha hecho y las recetas, con sus botones. Con `viewer_of` (el nombre
    de su dueño) es la de otra persona: solo se mira, sin botones para craftear ni fundir.
    """
    card = tarjetas.Card(FORGE_COLOUR)
    title = f"## ⚒️ Forja de {escape_markdown(viewer_of)}" if viewer_of else "## ⚒️ Forja"
    card.text(f"{title}\n{BAG} {monedas.format_purse(purse)}")
    if result:
        card.text(result)
    can = "Puede" if viewer_of else "Puedes"
    for target in ("plata", "oro"):
        source = LOWER[target]
        possible = purse.get(source) // RATE
        card.separator()
        card.text(f"**{tarjetas.emoji(target)} {target.capitalize()}** · {RATE} {tarjetas.emoji(source)} → "
                  f"1 {tarjetas.emoji(target)}\n-# {can} craftear {monedas.fmt_number(possible)}")
        if not viewer_of:
            card.buttons(*_amount_buttons("craft", target, possible))
    card.separator()
    card.text(f"**🔥 Fundir** · 1 → {RATE} de la moneda anterior\n-# Una fila por moneda: "
              f"{tarjetas.emoji('plata')} plata en cobre, {tarjetas.emoji('oro')} oro en plata")
    if not viewer_of:
        for source in ("plata", "oro"):
            card.buttons(*_amount_buttons("melt", source, purse.get(source), tarjetas.emoji(source)))
    card.separator()
    card.buttons(OpenInventoryButton(owner_id))
    card.note(f"Es la forja de {escape_markdown(viewer_of)}: solo puedes mirarla." if viewer_of
              else "Solo tú ves tu forja.")
    return card


def inventory_card(purse: monedas.Purse, owner_id: int, viewer_of: Optional[str] = None) -> tarjetas.Card:
    """El inventario: cada moneda en su línea, con su nombre, y lo que vale todo junto."""
    card = tarjetas.Card(INVENTORY_COLOUR)
    lines = [f"{tarjetas.emoji(name)} {name.capitalize()}: **{monedas.fmt_number(purse.get(name))}**"
             for name in monedas.COIN_NAMES]
    title = f"Inventario de {escape_markdown(viewer_of)}" if viewer_of else "Inventario"
    card.text(f"## {BAG} {title}\n" + "\n".join(lines))
    whose = "es el de otra persona" if viewer_of else "solo tú ves tu inventario"
    card.note(f"Vale {monedas.coin('cobre', purse.value)} en total · {whose}.")
    card.separator()
    card.buttons(OpenForgeButton(owner_id))
    return card


async def _open(interaction: discord.Interaction, owner_id: Optional[int],
                build: Callable[..., tarjetas.Card]):
    """
    Abre la tarjeta (inventario o forja) de `owner_id`, o de quien la pide si es None. Si es de otra persona,
    solo para mirar. Desde otra tarjeta privada la cambia ahí mismo; desde un mensaje público (un perfil) o
    un comando, la manda aparte, en privado.
    """
    if interaction.guild_id is None:
        return await interaction.response.send_message("❌ Esto solo funciona dentro de un servidor.",
                                                       ephemeral=True)
    owner_id = owner_id or interaction.user.id
    viewer_of = None
    if owner_id != interaction.user.id:
        owner = interaction.guild.get_member(owner_id) if interaction.guild else None
        viewer_of = owner.display_name if owner is not None else "alguien que ya no está"
    group = cuentas.group_of(interaction.guild_id, owner_id)
    purse = await asyncio.to_thread(monedas.person_purse, interaction.guild_id, group)
    card = build(purse, owner_id, viewer_of=viewer_of)
    message = interaction.message
    if message is not None and message.flags.ephemeral:
        await tarjetas.update(interaction, card)
    else:
        await interaction.response.send_message(view=card, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def _exchange(interaction: discord.Interaction, action: str, coin_name: str, count: Optional[int]):
    """Craftea o funde `count` monedas (None = todas las que se pueda) de quien pulsa, y redibuja su forja."""
    group = cuentas.group_of(interaction.guild_id, interaction.user.id)
    lower = LOWER[coin_name]
    if action == "craft":
        purse, done = await asyncio.to_thread(monedas.craft, interaction.guild_id, group, coin_name, count)
        result = (f"✅ Has crafteado {coin(coin_name, done)} con {coin(lower, done * RATE)}." if done
                  else f"❌ No te llega: cada una cuesta {coin(lower, RATE)}.")
    else:
        purse, done = await asyncio.to_thread(monedas.melt, interaction.guild_id, group, coin_name, count)
        result = (f"🔥 Has fundido {coin(coin_name, done)} en {coin(lower, done * RATE)}." if done
                  else f"❌ No te queda {coin_name} que fundir.")
    if done and count is not None and done < count:
        result += f"\n-# Pediste {monedas.fmt_number(count)}, pero solo te llegaba para {monedas.fmt_number(done)}."
    await tarjetas.update(interaction, forge_card(purse, interaction.user.id, result))


class AmountModal(ui.Modal):
    """La ventanita de «✏️ Cantidad»: cuántas monedas craftear o fundir."""

    def __init__(self, action: str, coin_name: str):
        verb = "craftear" if action == "craft" else "fundir"
        super().__init__(title=f"¿Cuánta {coin_name} quieres {verb}?")
        self.action, self.coin_name = action, coin_name
        self.amount = ui.TextInput(label="Cantidad", placeholder="Por ejemplo, 37", min_length=1,
                                   max_length=len(str(MAX_TYPED)))
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._submit)

    async def _submit(self, interaction: discord.Interaction):
        text = self.amount.value.strip().replace(".", "")
        if not text.isdigit() or int(text) < 1:
            return await interaction.response.send_message("❌ Escribe un número entero, como 37.", ephemeral=True)
        await _exchange(interaction, self.action, self.coin_name, int(text))


class ForgeButton(ui.DynamicItem[ui.Button],
                  template=r"reimbou:forja:(?P<action>craft|melt):(?P<coin>plata|oro):(?P<amount>\d+|max|pedir)"):
    """Un botón de la forja: craftear o fundir una cantidad de una moneda (o pedir cuántas)."""

    def __init__(self, action: str, coin_name: str, amount: Union[int, str], *, label: Optional[str] = None,
                 emoji: Optional[str] = None, disabled: bool = False,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(ui.Button(label=label, emoji=emoji, disabled=disabled, style=style,
                                   custom_id=f"reimbou:forja:{action}:{coin_name}:{amount}"))
        self.action, self.coin_name, self.amount = action, coin_name, amount

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        amount = match["amount"]
        return cls(match["action"], match["coin"], amount if amount in ("max", "pedir") else int(amount))

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._press)

    async def _press(self, interaction: discord.Interaction):
        if self.amount == "pedir":
            return await interaction.response.send_modal(AmountModal(self.action, self.coin_name))
        await _exchange(interaction, self.action, self.coin_name, None if self.amount == "max" else self.amount)


class OpenInventoryButton(ui.DynamicItem[ui.Button], template=r"reimbou:bolsa:abrir(?::(?P<owner>\d+))?"):
    """
    El botón 💰 Inventario (de un perfil y de la forja): abre el inventario de su dueño; si no es quien lo
    pulsa, solo para mirar. Los botones de antes no llevan dueño: abren el de quien los pulsa.
    """

    def __init__(self, owner_id: Optional[int] = None):
        suffix = f":{owner_id}" if owner_id else ""
        super().__init__(ui.Button(label="Inventario", emoji=BAG,
                                   custom_id=f"reimbou:bolsa:abrir{suffix}"))
        self.owner_id = owner_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(int(match["owner"]) if match["owner"] else None)

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, lambda i: _open(i, self.owner_id, inventory_card))


class OpenForgeButton(ui.DynamicItem[ui.Button], template=r"reimbou:forja:abrir(?::(?P<owner>\d+))?"):
    """
    El botón ⚒️ Forja (de un perfil y del inventario): abre la forja de su dueño; si no es quien lo pulsa,
    solo para mirar. Los botones de antes no llevan dueño: abren la de quien los pulsa.
    """

    def __init__(self, owner_id: Optional[int] = None):
        suffix = f":{owner_id}" if owner_id else ""
        super().__init__(ui.Button(label="Forja", emoji="⚒️", custom_id=f"reimbou:forja:abrir{suffix}"))
        self.owner_id = owner_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(int(match["owner"]) if match["owner"] else None)

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, lambda i: _open(i, self.owner_id, forge_card))


class ForjaCog(commands.Cog):
    """/craftear y los botones de la forja y el inventario."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        monedas.init_db()
        self.bot.add_dynamic_items(ForgeButton, OpenForgeButton, OpenInventoryButton)

    @app_commands.command(name="craftear", description="Tu forja: craftea plata y oro con tus monedas, o fúndelas")
    @app_commands.guild_only()
    async def craftear(self, interaction: discord.Interaction):
        await _open(interaction, None, forge_card)
