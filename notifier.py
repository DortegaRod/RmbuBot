import discord
from datetime import datetime, timezone

def now_utc():
    return datetime.now(timezone.utc)

async def send_admin_embed(admin_channel: discord.TextChannel, *, author_display: str, executor_display: str, channel_display: str, content: str, message_id: int):
    embed = discord.Embed(
        title="🗑️ Mensaje eliminado",
        description=(
            f"**Autor:** {author_display}\n"
            f"**Eliminado por:** {executor_display}\n"
            f"**Canal:** {channel_display}"
        ),
        color=discord.Color.red(),
        timestamp=now_utc()
    )
    embed.add_field(name="Contenido", value=content if content else "(no disponible en caché)", inline=False)
    embed.set_footer(text=f"ID del mensaje: {message_id}")
    await admin_channel.send(embed=embed)