"""
/help: una tarjeta con un menú para elegir sección (Música, Palabra del día, Gremio y Administración).
La sección de administración solo la ven quienes tienen «Gestionar servidor».
Los comandos se pueden pulsar desde la ayuda para escribirlos (cuando se conocen sus IDs).
"""

from typing import Callable, Dict, Optional, Tuple

import discord
from discord import ui

import tarjetas
from tarjetas import cmd


def _music() -> str:
    return (
        "## 🎵 Música\n"
        f"{cmd('play')} — Pon una canción o playlist de YouTube o Spotify (búsqueda o enlace)\n"
        f"{cmd('radio')} — Una radio española en directo\n"
        f"{cmd('current')} — Lo que suena, con su progreso (en radios, la canción que emiten)\n"
        f"{cmd('queue')} — La cola, por páginas\n"
        f"{cmd('skip')} — Pasa a la siguiente\n"
        f"{cmd('pause')} · {cmd('resume')} — Pausa y sigue\n"
        f"{cmd('loop')} — Repite la canción o toda la cola\n"
        f"{cmd('shuffle')} — Mezcla la cola\n"
        f"{cmd('remove')} — Quita una canción de la cola por su número\n"
        f"{cmd('clear')} — Vacía la cola (la que suena sigue)\n"
        f"{cmd('volume')} — Volumen de 0 a 100\n"
        f"{cmd('stop')} — Para la música y saca al bot\n"
        "-# Cada canción sale en una tarjeta con botones para pausar, saltar, parar y poner bucle.\n"
        "-# De Spotify se buscan sus canciones en YouTube (de las playlists, las 100 primeras).\n"
        "-# Para controlar la música tienes que estar en su canal de voz (los moderadores, desde cualquier sitio)."
    )


def _palabra() -> str:
    return (
        "## 📊 Palabra del día\n"
        f"{cmd('palabra')} — Adivina la palabra de hoy en 6 intentos (tus letras solo las ves tú)\n"
        f"{cmd('palabra-stats')} — Tus estadísticas y rachas, o las de otra persona\n"
        "-# Hay palabra nueva cada día a medianoche (hora de España). Las tildes no cuentan; la Ñ sí."
    )


def _gremio() -> str:
    return (
        "## 📜 Gremio\n"
        f"{cmd('perfil')} — Tu título, nivel, puesto y antigüedad, con tu inventario y tu forja (o el perfil de otra persona)\n"
        f"{cmd('craftear')} — Tu forja: craftea plata y oro con tus monedas, o fúndelas\n"
        f"{cmd('ranking')} — La clasificación del gremio\n"
        f"{cmd('vincular')} — Une otra cuenta tuya: vuestra experiencia se suma\n"
        f"{cmd('desvincular')} — Separa una cuenta vinculada\n"
        "-# Se gana experiencia escribiendo y estando en voz. Llegar al nivel N da N × 100 de cobre.\n"
        f"-# En la forja, {tarjetas.emoji('cobre')} 100 de cobre hacen {tarjetas.emoji('plata')} 1 de plata y "
        f"{tarjetas.emoji('plata')} 100 de plata, {tarjetas.emoji('oro')} 1 de oro."
    )


def _admin() -> str:
    return (
        "## ⚙️ Administración\n"
        f"{cmd('setup')} — Canal donde se usan los comandos del bot\n"
        f"{cmd('setup-palabra')} — Canal, hora y rol del anuncio diario de la palabra\n"
        f"{cmd('setup-niveles')} — Activa los niveles y elige dónde se anuncian las subidas\n"
        f"{cmd('niveles-recompensa')} — Roles por llegar a un nivel\n"
        f"{cmd('niveles-multiplicador')} — Más experiencia por escribir en un canal\n"
        f"{cmd('niveles-ajustar')} — Pon a alguien una experiencia o un nivel exactos\n"
        f"{cmd('niveles-importar')} — Importa la clasificación de Amari\n"
        f"{cmd('vincular-cuentas')} — Vincula cuentas de una persona sin pedirle confirmación\n"
        f"{cmd('evento-voz')} — Evento de Discord automático cuando alguien con un rol entra en un canal de voz\n"
        f"{cmd('setup-eventos')} — Canal donde se avisa con el enlace cuando empieza un evento\n"
        f"{cmd('titulos')} — Qué roles salen como título en el perfil\n"
        "-# Necesitan el permiso «Gestionar servidor»."
    )


# Sección -> (emoji, nombre, descripción en el menú, texto)
SECTIONS: Dict[str, Tuple[str, str, str, Callable[[], str]]] = {
    "musica": ("🎵", "Música", "YouTube, Spotify y radios, con botones en cada canción", _music),
    "palabra": ("📊", "Palabra del día", "El Wordle en español, con rachas", _palabra),
    "niveles": ("📜", "Gremio", "Tu perfil, nivel, monedas y el ranking del gremio", _gremio),
    "admin": ("⚙️", "Administración", "Configuración del bot (solo admins)", _admin),
}


def _is_admin(member: discord.abc.User) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


def _overview(admin: bool) -> str:
    lines = ["## 🎧 ReimbouBOT"]
    for key, (emoji, name, description, _) in SECTIONS.items():
        if key != "admin" or admin:
            lines.append(f"{emoji} **{name}** — {description.replace(' (solo admins)', '')}")
    lines.append("-# Elige una sección en el menú.")
    return "\n".join(lines)


def help_card(member: discord.abc.User, section: Optional[str] = None) -> tarjetas.Card:
    admin = _is_admin(member)
    if section == "admin" and not admin:
        section = None
    card = tarjetas.Card(tarjetas.INFO)
    card.text(SECTIONS[section][3]() if section in SECTIONS else _overview(admin))
    card.separator()
    card.buttons(HelpMenu(admin, section))
    return card


class HelpMenu(ui.DynamicItem[ui.Select], template=r"reimbou:ayuda"):
    """Menú de secciones de /help. Es persistente: sigue funcionando aunque el bot se reinicie."""

    def __init__(self, admin: bool, current: Optional[str] = None):
        options = [discord.SelectOption(label=name, value=key, emoji=emoji, description=description,
                                        default=key == current)
                   for key, (emoji, name, description, _) in SECTIONS.items() if key != "admin" or admin]
        super().__init__(ui.Select(custom_id="reimbou:ayuda", placeholder="Elige una sección", options=options))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Select, match):
        return cls(_is_admin(interaction.user))

    async def callback(self, interaction: discord.Interaction):
        await tarjetas.run_safely(interaction, self._show)

    async def _show(self, interaction: discord.Interaction):
        section = self.item.values[0] if self.item.values else None
        await tarjetas.update(interaction, help_card(interaction.user, section))
