import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Discord Bot Configuration
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("TOKEN no encontrado en las variables de entorno")

ADMIN_LOG_CHANNEL_ID = int(os.environ.get("ADMIN_LOG_CHANNEL_ID", 0))

# --- NUEVO: Canal de Música ---
# Si no está en el .env, ponemos 0 (así fallará seguro si no se configura)
MUSIC_CHANNEL_ID = int(os.environ.get("MUSIC_CHANNEL_ID", 0))

# Cache Configuration
CACHE_MAX = int(os.environ.get("CACHE_MAX", 5000))

# Audit Configuration
AUDIT_LOOKBACK_SECONDS = int(os.environ.get("AUDIT_LOOKBACK_SECONDS", 10))
AUDIT_WAIT_SECONDS = float(os.environ.get("AUDIT_WAIT_SECONDS", 1.2))

# Database Configuration
DB_PATH = Path(__file__).parent / "mensajes.db"

# Bot Intents
INTENTS = {
    "guilds": True,
    "messages": True,
    "message_content": True,
    "members": True,
    "voice_states": True,
}

# Music Configuration
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", 50))
DEFAULT_VOLUME = float(os.environ.get("DEFAULT_VOLUME", 0.5))
INACTIVITY_TIMEOUT = int(os.environ.get("INACTIVITY_TIMEOUT", 300))