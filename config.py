import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TOKEN")
ADMIN_LOG_CHANNEL_ID = int(os.environ.get("ADMIN_LOG_CHANNEL_ID", 0))
CACHE_MAX = int(os.environ.get("CACHE_MAX", 5000))
AUDIT_LOOKBACK_SECONDS = int(os.environ.get("AUDIT_LOOKBACK_SECONDS", 10))

# Ruta de la base de datos
DB_PATH = os.path.join(os.path.dirname(__file__), "mensajes.db")

# Intents del bot
INTENTS = {
    "guilds": True,
    "messages": True,
    "message_content": True,
    "members": True,
}

AUDIT_WAIT_SECONDS = 1.2
