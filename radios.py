"""
Catálogo de emisoras de radio y lectura del título que suena en directo (metadatos ICY).

Todas las URLs se verificaron con OpenSSL, que es el TLS que usa FFmpeg en Linux
(algunas emisoras solo funcionan por http:// porque su HTTPS usa cifrados antiguos).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# Icono 📻 de Twemoji (CDN estable). El de Imgur que se usaba antes fue eliminado.
RADIO_ICON = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/1f4fb.png"

_STREAMTHEWORLD = "https://playerservices.streamtheworld.com/api/livestream-redirect/"
_ATRESMEDIA = "https://radio-atres-live.ondacero.es/api/livestream-redirect/"
_COPE = "http://flucast09-h-cloud.flumotion.com/cope/"  # Su HTTPS falla con OpenSSL 3: usar http://


@dataclass(frozen=True)
class Radio:
    key: str
    name: str
    emoji: str
    url: str


# El orden es el que se ve en el menú de /radio (Discord permite 25 como máximo)
RADIOS: List[Radio] = [
    # --- Música ---
    Radio("los40", "Los 40 Principales", "📻", _STREAMTHEWORLD + "Los40.mp3"),
    Radio("los40urban", "Los 40 Urban", "📻", _STREAMTHEWORLD + "LOS40_URBAN.mp3"),
    Radio("cadenadial", "Cadena Dial", "🎤", _STREAMTHEWORLD + "CADENADIAL.mp3"),
    Radio("cadena100", "Cadena 100", "💯", _COPE + "cadena100.mp3"),
    Radio("europafm", "Europa FM", "🌍", _ATRESMEDIA + "EFMAAC.aac"),
    Radio("kissfm", "Kiss FM", "💋", "https://kissfm.kissfmradio.cires21.com/kissfm.mp3"),
    Radio("hitfm", "Hit FM", "🔥", "https://kissfm.kissfmradio.cires21.com/hitfm.mp3"),
    Radio("megastar", "MegaStar FM", "🌟", _COPE + "megastar.mp3"),
    Radio("flaixfm", "Flaix FM", "⚡", "https://stream.flaixfm.cat/icecast"),
    Radio("rockfm", "Rock FM", "🎸", _COPE + "rockfm.mp3"),
    Radio("melodiafm", "Melodía FM", "🎼", _ATRESMEDIA + "MELODIA_FMAAC.aac"),
    Radio("radiola", "Radiolé", "💃", _STREAMTHEWORLD + "RADIOLE.mp3"),
    Radio("rac105", "RAC 105", "🎶", _STREAMTHEWORLD + "RAC105.mp3"),
    Radio("locafm", "Loca FM", "🤪", "https://s3.we4stream.com:2020/stream/locafm"),
    Radio("ibizaglobal", "Ibiza Global Radio", "🏝️", "http://ibizaglobalradio.streaming-pro.com:8024"),
    Radio("radio3", "RNE Radio 3", "🎧", "https://rtvelivestream.rtve.es/rtvesec/rne/rne_r3_main.m3u8"),
    # --- Habladas y deportes ---
    Radio("cadenaser", "Cadena SER", "🗣️", _STREAMTHEWORLD + "CADENASER.mp3"),
    Radio("cope", "COPE", "🗣️", _COPE + "net1.mp3"),
    Radio("ondacero", "Onda Cero", "🗣️", _ATRESMEDIA + "OCAAC.aac"),
    Radio("radiomarca", "Radio Marca", "⚽", _STREAMTHEWORLD + "RADIOMARCA_NACIONAL.mp3"),
]

RADIOS_BY_KEY: Dict[str, Radio] = {r.key: r for r in RADIOS}

# --- Título en directo (ICY) ---
# Muchas emisoras intercalan "StreamTitle='Artista - Canción';" en el audio si se pide
# con la cabecera Icy-MetaData. Solo leemos el primer bloque y cerramos la conexión.
_TITLE_CACHE_SECONDS = 20
_title_cache: Dict[str, Tuple[float, Optional[str]]] = {}


async def fetch_now_playing(radio_url: str, station_name: str = "", timeout: float = 6.0) -> Optional[str]:
    """Devuelve 'Artista - Canción' si la emisora lo publica, o None."""
    cached = _title_cache.get(radio_url)
    if cached and time.monotonic() - cached[0] < _TITLE_CACHE_SECONDS:
        return cached[1]

    title = None
    try:
        title = await asyncio.wait_for(_read_icy_title(radio_url), timeout)
    except Exception as e:
        logger.debug(f"Sin título ICY para {radio_url}: {e!r}")

    if title:
        title = title.strip(" -")
        # Algunas emisoras solo publican su propio nombre: eso no es una canción
        if not title or title.lower() == station_name.lower():
            title = None

    _title_cache[radio_url] = (time.monotonic(), title)
    return title


async def _read_icy_title(url: str) -> Optional[str]:
    if ".m3u8" in url:
        return None  # HLS no lleva metadatos ICY
    headers = {"Icy-MetaData": "1", "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            metaint = int(resp.headers.get("icy-metaint") or 0)
            if metaint <= 0:
                return None
            await resp.content.readexactly(metaint)  # audio que precede al primer bloque
            length = (await resp.content.readexactly(1))[0] * 16
            if length == 0:
                return None
            raw = (await resp.content.readexactly(length)).rstrip(b"\0")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    match = re.search(r"StreamTitle='(.*?)';", text, re.S)
    return match.group(1) if match else None
