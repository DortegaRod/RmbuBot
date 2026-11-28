"""
Moderación: reglas que un admin pone a un canal con /moderacion (una por canal):

- «Solo vídeos»: solo se pueden mandar vídeos (un archivo de vídeo o un enlace que Discord enseña como vídeo,
  también reenviados; los GIF no, ver videos.py). Lo demás se borra sin avisar, y sin pasar por el log de
  borrados. Vale para un canal de texto o de voz y para un foro (sus publicaciones y las respuestas).
  - Quien tiene «Gestionar mensajes» en el canal puede escribir lo que quiera. Los bots, también.
  - En un foro, el primer mensaje de cada publicación no se toca.
  - Si el mensaje lleva un enlace sin vista previa todavía (Discord la añade unos segundos después), se
    espera un poco antes de decidir.
- «Solo audios»: igual, pero lo que vale es un archivo de audio o una nota de voz (los enlaces, no).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import cache
import db
import tarjetas
import videos
from tarjetas import NO_MENTIONS

logger = logging.getLogger(__name__)

PREVIEW_WAIT = 8.0  # Segundos que se espera la vista previa de un enlace antes de decidir si es un vídeo
ONLY_VIDEOS = "solo-videos"
ONLY_AUDIOS = "solo-audios"
RULES = {ONLY_VIDEOS: "🎬 Solo vídeos", ONLY_AUDIOS: "🎵 Solo audios"}
RULE_HELP = {ONLY_VIDEOS: "solo vídeos (archivo o enlace a un vídeo; los GIF no)",
             ONLY_AUDIOS: "solo audios (archivo de audio o nota de voz)"}

Moderated = Union[discord.TextChannel, discord.ForumChannel, discord.VoiceChannel]


@dataclass
class ChannelRule:
    guild_id: int
    rule: str


def init_db():
    with db.get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS moderacion_canales (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            regla TEXT NOT NULL
        )""")


def load_rules() -> Dict[int, ChannelRule]:
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT channel_id, guild_id, regla FROM moderacion_canales").fetchall()
    return {channel_id: ChannelRule(guild_id, rule) for channel_id, guild_id, rule in rows}


def save_rule(channel_id: int, rule: Optional[ChannelRule]):
    """Guarda la regla de un canal; con None se la quita."""
    with db.get_db_connection() as conn:
        if rule is None:
            conn.execute("DELETE FROM moderacion_canales WHERE channel_id = ?", (channel_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO moderacion_canales (channel_id, guild_id, regla) VALUES (?, ?, ?)",
                         (channel_id, rule.guild_id, rule.rule))


class ModeracionCog(commands.Cog):
    """/moderacion y las reglas de cada canal."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rules: Dict[int, ChannelRule] = {}

    async def cog_load(self):
        init_db()
        self.rules = await asyncio.to_thread(load_rules)

    def rule_for(self, channel) -> Optional[str]:
        """La regla del canal; en una publicación de un foro (o un hilo), la de su canal."""
        rule = self.rules.get(channel.id) or self.rules.get(getattr(channel, "parent_id", None))
        return rule.rule if rule else None

    # ---------- Solo vídeos / solo audios ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.is_system():
            return
        channel = message.channel
        rule = self.rule_for(channel)
        if rule not in (ONLY_VIDEOS, ONLY_AUDIOS):
            return
        if isinstance(channel, discord.Thread) and message.id == channel.id:
            return  # El primer mensaje de una publicación de un foro no se toca
        if channel.permissions_for(message.author).manage_messages:
            return
        allowed = videos.has_video if rule == ONLY_VIDEOS else videos.has_audio
        if allowed(message):
            return
        if rule == ONLY_VIDEOS and videos.waiting_preview(message):
            # La vista previa del enlace llega unos segundos después: se espera y se vuelve a mirar
            await asyncio.sleep(PREVIEW_WAIT)
            try:
                message = await channel.fetch_message(message.id)
            except discord.NotFound:
                return  # Ya lo habían borrado
            except discord.HTTPException as e:
                logger.warning(f"🛡️ Moderación: no pude volver a leer un mensaje en #{channel}: {e}")
                return
            if allowed(message):
                return
        cache.mark_moderated(message.id)
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            cache.was_moderated(message.id)  # No se borró: que no se quede apuntado
            logger.warning(f"🛡️ Moderación: no pude borrar un mensaje en #{channel} ({RULES[rule]}): {e}")

    # ---------- /moderacion (admins) ----------

    def _list_card(self, guild: discord.Guild) -> tarjetas.Card:
        lines = [f"<#{channel_id}> · {RULES.get(rule.rule, rule.rule)}"
                 for channel_id, rule in self.rules.items() if rule.guild_id == guild.id]
        card = tarjetas.Card(tarjetas.INFO)
        card.text("### 🛡️ Moderación\n" + ("\n".join(lines) or "Ningún canal tiene reglas todavía."))
        card.note(f"Pon una regla con {tarjetas.cmd('moderacion')} canal:#canal. Quien tiene «Gestionar mensajes» "
                  "en el canal se la salta.")
        return card

    @app_commands.command(name="moderacion", description="Reglas de un canal: solo vídeos o solo audios (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal o foro (vacío = ver las reglas que hay)", regla="La regla (por defecto, solo vídeos)",
                           quitar="Quitar la regla de ese canal")
    @app_commands.choices(regla=[app_commands.Choice(name=name, value=key) for key, name in RULES.items()])
    async def moderacion(self, interaction: discord.Interaction, canal: Optional[Moderated] = None,
                         regla: Optional[app_commands.Choice[str]] = None, quitar: bool = False):
        guild = interaction.guild
        if canal is None:
            return await interaction.response.send_message(view=self._list_card(guild), ephemeral=True,
                                                           allowed_mentions=NO_MENTIONS)
        if quitar:
            self.rules.pop(canal.id, None)
            await asyncio.to_thread(save_rule, canal.id, None)
            return await interaction.response.send_message(f"✅ {canal.mention} ya no tiene reglas.", ephemeral=True)
        perms = canal.permissions_for(guild.me)
        if not (perms.view_channel and perms.manage_messages and perms.read_message_history):
            return await interaction.response.send_message(
                f"❌ En {canal.mention} necesito **Ver canal**, **Gestionar mensajes** y **Leer el historial**.",
                ephemeral=True
            )
        rule = ChannelRule(guild.id, regla.value if regla else ONLY_VIDEOS)
        self.rules[canal.id] = rule
        await asyncio.to_thread(save_rule, canal.id, rule)
        forum = (" En cada publicación, el primer mensaje no se toca; las respuestas, sí."
                 if isinstance(canal, discord.ForumChannel) else "")
        await interaction.response.send_message(
            f"✅ En {canal.mention} ahora se puede mandar {RULE_HELP[rule.rule]}. Lo demás se borra sin avisar; "
            f"quien tiene «Gestionar mensajes» ahí puede escribir igual.{forum}",
            ephemeral=True, allowed_mentions=NO_MENTIONS
        )
