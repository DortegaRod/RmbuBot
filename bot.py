import discord
from discord.ext import commands
import asyncio
from config import TOKEN, ADMIN_LOG_CHANNEL_ID, INTENTS
import db
from notifier import send_admin_embed
from audit import find_audit_entry_for_channel

# Configurar intents
intents = discord.Intents.default()
intents.guilds = INTENTS.get("guilds", True)
intents.messages = INTENTS.get("messages", True)
intents.message_content = INTENTS.get("message_content", True)
intents.members = INTENTS.get("members", True)

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[READY] Conectado como {bot.user} (ID {bot.user.id})")
    db.init_db()
    print("[INFO] Base de datos inicializada")

@bot.event
async def on_message(message: discord.Message):
    # Solo mensajes en servidores y que no sean de bots
    if message.guild and not message.author.bot:
        try:
            content = message.content or ""
            author_id = message.author.id
            channel_id = message.channel.id
            # Guardar siempre en BD
            db.save_message(message.id, author_id, content, channel_id)
            print(f"[SAVE] Mensaje almacenado ID {message.id} Autor {author_id}")
        except Exception as e:
            print(f"[ERROR] No se pudo guardar mensaje {message.id}: {e}")
    await bot.process_commands(message)

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    # Esta función puede notificar borrados con datos de DB
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    channel = guild.get_channel(payload.channel_id) if guild and payload.channel_id else None
    if not guild or not channel:
        print("[INFO] Guild o canal no disponibles para on_raw_message_delete.")
        return

    rec = db.get_message(payload.message_id)
    if not rec:
        print("[INFO] Mensaje no encontrado en base de datos.")
        return

    # Esperar para consultar audit logs
    await asyncio.sleep(1.2)
    entry = await find_audit_entry_for_channel(guild, channel.id)

    admin_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
    if not admin_channel:
        print(f"[ERROR] Canal admin no encontrado con ID {ADMIN_LOG_CHANNEL_ID}")
        return

    author_id = rec['author_id']
    content = rec['content']
    author_display = f"<@{author_id}>" if author_id else "Desconocido"

    if entry and getattr(entry, 'user', None):
        executor = entry.user
        executor_display = executor.mention if hasattr(executor, 'mention') else str(executor)

        if author_id and getattr(executor, 'id', None) == author_id:
            # Auto borrado, no notificar
            return

        channel_display = channel.mention
        try:
            await send_admin_embed(admin_channel,
                                   author_display=author_display,
                                   executor_display=executor_display,
                                   channel_display=channel_display,
                                   content=content,
                                   message_id=payload.message_id)
            print(f"[SEND] Notificación enviada para mensaje {payload.message_id}")
        except Exception as e:
            print(f"[ERROR] Error enviando notificación: {e}")


@bot.event
async def on_ready():
    db.init_db()

if __name__ == '__main__':
    print("[INIT] Iniciando bot...")
    bot.run(TOKEN)
