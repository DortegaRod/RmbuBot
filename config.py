import os
from pathlib import Path
from dotenv import load_dotenv
import logging

load_dotenv()

logger = logging.getLogger(__name__)

# Discord Bot Configuration
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN no encontrado en las variables de entorno. Configura tu archivo .env")

# Canal de logs de administración
ADMIN_LOG_CHANNEL_ID = int(os.environ.get("ADMIN_LOG_CHANNEL_ID", 0))
if ADMIN_LOG_CHANNEL_ID == 0:
    logger.warning("⚠️ ADMIN_LOG_CHANNEL_ID no configurado, los logs de mensajes eliminados no funcionarán")

# Canal de música (0 = todos los canales permitidos)
MUSIC_CHANNEL_ID = int(os.environ.get("MUSIC_CHANNEL_ID", 0))
if MUSIC_CHANNEL_ID == 0:
    logger.info("ℹ️ MUSIC_CHANNEL_ID no configurado, comandos de música permitidos en todos los canales")

# Cache Configuration
CACHE_MAX = int(os.environ.get("CACHE_MAX", 5000))
if CACHE_MAX < 100:
    logger.warning(f"⚠️ CACHE_MAX muy bajo ({CACHE_MAX}), recomendado al menos 1000")

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
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", 100))  # Aumentado de 50 a 100
DEFAULT_VOLUME = float(os.environ.get("DEFAULT_VOLUME", 0.5))
INACTIVITY_TIMEOUT = int(os.environ.get("INACTIVITY_TIMEOUT", 300))  # 5 minutos

# Validaciones
if DEFAULT_VOLUME < 0 or DEFAULT_VOLUME > 1:
    logger.warning(f"⚠️ DEFAULT_VOLUME ({DEFAULT_VOLUME}) fuera de rango [0-1], usando 0.5")
    DEFAULT_VOLUME = 0.5

if INACTIVITY_TIMEOUT < 60:
    logger.warning(f"⚠️ INACTIVITY_TIMEOUT muy bajo ({INACTIVITY_TIMEOUT}s), recomendado al menos 60s")

logger.info("✅ Configuración cargada correctamente")