"""
Configuración del bot, leída del archivo .env.
También configura el logging, para que funcione desde el primer mensaje del arranque.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"⚠️ {name}={raw!r} no es un número entero, usando {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"⚠️ {name}={raw!r} no es un número, usando {default}")
        return default


# Discord Bot Configuration
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN no encontrado en las variables de entorno. Configura tu archivo .env")

# Canal de logs de administración (su servidor es el único donde se auditan mensajes)
ADMIN_LOG_CHANNEL_ID = _env_int("ADMIN_LOG_CHANNEL_ID", 0)
if ADMIN_LOG_CHANNEL_ID == 0:
    logger.warning("⚠️ ADMIN_LOG_CHANNEL_ID no configurado, los logs de mensajes eliminados no funcionarán")

# Cache Configuration
CACHE_MAX = _env_int("CACHE_MAX", 5000)
if CACHE_MAX < 100:
    logger.warning(f"⚠️ CACHE_MAX muy bajo ({CACHE_MAX}), recomendado al menos 1000")

# Audit Configuration
AUDIT_LOOKBACK_SECONDS = _env_int("AUDIT_LOOKBACK_SECONDS", 10)
AUDIT_WAIT_SECONDS = _env_float("AUDIT_WAIT_SECONDS", 1.2)

# Database Configuration
DB_PATH = Path(__file__).parent / "mensajes.db"

# Music Configuration
MAX_QUEUE_SIZE = _env_int("MAX_QUEUE_SIZE", 100)
DEFAULT_VOLUME = _env_float("DEFAULT_VOLUME", 0.5)
INACTIVITY_TIMEOUT = _env_int("INACTIVITY_TIMEOUT", 300)      # Cola vacía: 5 minutos
EMPTY_CHANNEL_TIMEOUT = _env_int("EMPTY_CHANNEL_TIMEOUT", 60)  # Nadie en el canal de voz: 1 minuto

# Validaciones
if MAX_QUEUE_SIZE < 1:
    logger.warning(f"⚠️ MAX_QUEUE_SIZE ({MAX_QUEUE_SIZE}) inválido, usando 100")
    MAX_QUEUE_SIZE = 100

if DEFAULT_VOLUME < 0 or DEFAULT_VOLUME > 1:
    logger.warning(f"⚠️ DEFAULT_VOLUME ({DEFAULT_VOLUME}) fuera de rango [0-1], usando 0.5")
    DEFAULT_VOLUME = 0.5

if INACTIVITY_TIMEOUT < 60:
    logger.warning(f"⚠️ INACTIVITY_TIMEOUT muy bajo ({INACTIVITY_TIMEOUT}s), recomendado al menos 60s")

logger.info("✅ Configuración cargada correctamente")
