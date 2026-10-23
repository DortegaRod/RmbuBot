"""
Tarjetas: los mensajes con formato del bot, hechos con los componentes nuevos de Discord (Components V2).

- Una tarjeta es un contenedor con una barra de color. Dentro lleva, en orden: títulos y texto,
  notas en gris pequeño, separadores, una miniatura al lado del texto y los botones.
- Los mensajes cortos (confirmaciones y errores) siguen siendo una línea de texto normal.
- Los botones y menús que tienen que funcionar aunque el bot se reinicie son DynamicItem:
  guardan lo que necesitan (página, dueño...) en su custom_id.

Límites de Discord por mensaje: 40 componentes y 4000 caracteres de texto entre todos.
Un mensaje con tarjeta no puede llevar texto normal ni embeds, ni volver a tenerlos.
"""

import logging
from typing import Awaitable, Callable, Dict, List, Optional, Union

import discord
from discord import ui

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
MAX_TEXT = 4000  # Caracteres de texto entre todos los componentes de un mensaje

# Color de la barra de cada tipo de tarjeta
MUSIC = discord.Colour.green()
RADIO = discord.Colour.gold()
QUEUE = discord.Colour.blue()
PLAYED = discord.Colour.dark_grey()  # Canciones que ya han sonado (el registro del canal)
LEVELS = discord.Colour.blurple()
RANKING = discord.Colour.gold()
WORDLE = discord.Colour.green()
INFO = discord.Colour.blurple()
SUCCESS = discord.Colour.green()
WARNING = discord.Colour.orange()
DANGER = discord.Colour.red()

# IDs de los comandos de barra (se rellenan al sincronizarlos): con ellos se pueden pulsar en el texto
command_ids: Dict[str, int] = {}


def cmd(name: str) -> str:
    """Mención de un comando: se ve como /nombre y al pulsarla se escribe el comando. Sin ID, texto normal."""
    command_id = command_ids.get(name)
    return f"</{name}:{command_id}>" if command_id else f"`/{name}`"


class Card(ui.LayoutView):
    """Una tarjeta: un contenedor con barra de color al que se le añaden bloques en orden."""

    def __init__(self, colour: discord.Colour, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.box = ui.Container(accent_colour=colour)
        self.add_item(self.box)

    def text(self, content: str) -> "Card":
        """Texto con formato de Discord (## para títulos, **negrita**...)."""
        self.box.add_item(ui.TextDisplay(content))
        return self

    def note(self, content: str) -> "Card":
        """Texto pequeño y gris, para detalles y pies de tarjeta."""
        return self.text("\n".join(f"-# {line}" for line in content.splitlines() if line.strip()))

    def section(self, content: str, thumbnail: Optional[str] = None, description: Optional[str] = None) -> "Card":
        """Texto con una imagen pequeña a la derecha (carátula, avatar). Sin imagen, solo el texto."""
        if not thumbnail:
            return self.text(content)
        self.box.add_item(ui.Section(ui.TextDisplay(content),
                                     accessory=ui.Thumbnail(thumbnail, description=description)))
        return self

    def separator(self, *, visible: bool = True, large: bool = False) -> "Card":
        spacing = discord.SeparatorSpacing.large if large else discord.SeparatorSpacing.small
        self.box.add_item(ui.Separator(visible=visible, spacing=spacing))
        return self

    def buttons(self, *items: ui.Item) -> "Card":
        """Una fila de hasta 5 botones (o un menú desplegable)."""
        if items:
            self.box.add_item(ui.ActionRow(*items))
        return self

    def attachment(self, filename: str) -> "Card":
        """Muestra un archivo adjunto al mensaje (en las tarjetas no se ven si no)."""
        self.box.add_item(ui.File(f"attachment://{filename}"))
        return self


def simple(colour: discord.Colour, content: str) -> Card:
    """Tarjeta de un solo texto (para sustituir el contenido de una tarjeta que ya no necesita botones)."""
    return Card(colour).text(content)


async def update(interaction: discord.Interaction, view: ui.LayoutView, **kwargs):
    """
    Cambia el mensaje del botón pulsado por `view`. Si era un mensaje de antes de las tarjetas
    (texto o embed), Discord exige vaciarlo en la misma edición.
    """
    message = interaction.message
    if message is not None and not message.flags.components_v2:
        kwargs.update(content=None, embed=None)
    kwargs.setdefault("allowed_mentions", NO_MENTIONS)
    await interaction.response.edit_message(view=view, **kwargs)


async def run_safely(interaction: discord.Interaction, handler: Callable[[discord.Interaction], Awaitable[None]]):
    """
    Ejecuta lo que hace un botón persistente. Sus errores no llegan al manejador general de los
    comandos (discord.py solo los registra), así que aquí se avisa también a quien lo pulsó.
    """
    try:
        await handler(interaction)
    except Exception:
        logger.exception("Error en un botón o menú")
        message = "❌ Algo ha fallado con ese botón. Ha quedado registrado en el log del bot."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


# ==========================================
# LISTADOS POR PÁGINAS (/ranking, /queue)
# ==========================================

# Lo que devuelve el dibujante de una página: la tarjeta, o un aviso que se manda solo a quien pulsó
PageResult = Union[Card, str]
# (interacción, dueño de la tarjeta, página o «yo» = la de quien pulsa) -> resultado
PageRenderer = Callable[[discord.Interaction, int, Union[int, str]], Awaitable[PageResult]]
_renderers: Dict[str, PageRenderer] = {}


def register_pages(kind: str, renderer: PageRenderer):
    """Registra cómo se dibujan las páginas de un listado («ranking», «cola»...)."""
    _renderers[kind] = renderer


def page_controls(kind: str, owner_id: int, page: int, pages: int, *extra: ui.Item) -> List[ui.Item]:
    """
    Flechas para pasar de página (si hay más de una), el número de página y los botones extra.
    Todos son botones persistentes (el número también, desactivado): así discord.py no guarda
    en memoria cada listado enviado.
    """
    items: List[ui.Item] = []
    if pages > 1:
        items += [
            PageButton(kind, owner_id, page - 1, emoji="◀️", disabled=page <= 1),
            PageButton(kind, owner_id, page, label=f"{page} / {pages}", disabled=True),
            PageButton(kind, owner_id, page + 1, emoji="▶️", disabled=page >= pages),
        ]
    return items + list(extra)


def clamp_page(page: Union[int, str], pages: int) -> int:
    return min(max(1, page if isinstance(page, int) else 1), pages)


class PageButton(ui.DynamicItem[ui.Button], template=r"reimbou:pag:(?P<kind>[a-z]+):(?P<owner>\d+):(?P<page>\d+|yo)"):
    """
    Botón de un listado por páginas. Quien pidió el listado mueve su mensaje; si pulsa otra persona,
    le abre su propia copia (que solo ve ella) en esa página, para que nadie le cambie la página a otro.
    """

    def __init__(self, kind: str, owner_id: int, page: Union[int, str], *, emoji=None, label: Optional[str] = None,
                 disabled: bool = False, style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(ui.Button(emoji=emoji, label=label, style=style, disabled=disabled,
                                   custom_id=f"reimbou:pag:{kind}:{owner_id}:{page}"))
        self.kind, self.owner_id, self.page = kind, owner_id, page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        page = match["page"]
        return cls(match["kind"], int(match["owner"]), page if page == "yo" else int(page),
                   emoji=item.emoji, label=item.label, disabled=item.disabled, style=item.style)

    async def callback(self, interaction: discord.Interaction):
        await run_safely(interaction, self._turn_page)

    async def _turn_page(self, interaction: discord.Interaction):
        renderer = _renderers.get(self.kind)
        if renderer is None:
            return await interaction.response.send_message("⌛ Este botón ya no funciona.", ephemeral=True)
        result = await renderer(interaction, interaction.user.id, self.page)
        if isinstance(result, str):
            await interaction.response.send_message(result, ephemeral=True, allowed_mentions=NO_MENTIONS)
        elif interaction.user.id == self.owner_id:
            await update(interaction, result)
        else:
            await interaction.response.send_message(view=result, ephemeral=True, allowed_mentions=NO_MENTIONS)
