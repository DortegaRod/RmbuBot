"""
Enlaces de Spotify en /play: se leen sus canciones (título, artistas, duración y carátula) y cada una
se busca en YouTube justo antes de sonar (music.py, _resolve_search).

Por qué se leen de la página que Spotify ofrece para insertar canciones en otras webs:
- El audio de Spotify está cifrado y sus condiciones no permiten reproducirlo fuera de su app.
- Su API oficial exige desde febrero de 2026 que el dueño de la app pague Premium, y ya no deja leer
  las canciones de las playlists de otras personas.
- La página para insertar da canciones, álbumes y playlists (de estas, las 100 primeras canciones)
  sin credenciales. No es una API oficial: si Spotify la cambia, /play avisa de que no pudo leer el enlace.
"""

import asyncio
import json
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from music import SearchError, Song

logger = logging.getLogger(__name__)

EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
KINDS = ("track", "album", "playlist")  # Lo que se puede poner (no artistas, podcasts ni episodios)
SHORT_HOSTS = {"spotify.link", "spoti.fi"}  # Enlaces cortos que comparte la app del móvil
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = aiohttp.ClientTimeout(total=10)

_URI = re.compile(r"spotify:(\w+):(\w+)")
_PATH = re.compile(r"/(?:intl-[\w-]+/)?(\w+)/(\w+)")  # /track/ID, o /intl-es/track/ID
_FULL_LINK = re.compile(r"https://open\.spotify\.com/(?:intl-[\w-]+/)?(?:track|album|playlist)/\w+")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_spotify(query: str) -> bool:
    """True si lo que se ha escrito en /play es un enlace de Spotify."""
    query = query.strip().strip("<>")
    host = _host(query)
    return query.startswith("spotify:") or host == "spotify.com" or host.endswith(".spotify.com") or host in SHORT_HOSTS


def parse_link(url: str) -> Optional[Tuple[str, str]]:
    """(tipo, id) de un enlace o URI de Spotify: («track», «4cOd...»), («playlist», ...), («artist», ...)..."""
    match = _URI.fullmatch(url)
    if match is None and _host(url).endswith("spotify.com"):
        match = _PATH.match(urlparse(url).path)
    return (match.group(1), match.group(2)) if match else None


async def _resolve_short_link(session: aiohttp.ClientSession, url: str) -> str:
    """Los enlaces cortos redirigen al largo; si en vez de redirigir devuelven una página, se busca en ella."""
    async with session.get(url) as resp:
        if parse_link(str(resp.url)):
            return str(resp.url)
        match = _FULL_LINK.search(await resp.text())
    return match.group(0) if match else url


def _read_entity(html: str) -> dict:
    """Los datos de la canción, álbum o playlist que la página lleva dentro (para dibujarse)."""
    match = _NEXT_DATA.search(html)
    try:
        props = json.loads(match.group(1))["props"]["pageProps"] if match else {}
    except (ValueError, KeyError, TypeError):
        props = {}
    if props.get("status") == 404:
        raise SearchError("ese enlace de Spotify no existe o es privado")
    entity = ((props.get("state") or {}).get("data") or {}).get("entity")
    if not entity:
        logger.warning("Spotify: no encuentro las canciones en su página para insertar; puede que la hayan cambiado")
        raise SearchError("no pude leer ese enlace de Spotify (puede que Spotify haya cambiado su web)")
    return entity


def _cover(entity: dict) -> str:
    """La carátula más grande."""
    images = (entity.get("visualIdentity") or {}).get("image") or []
    best = max(images, key=lambda image: image.get("maxWidth") or 0, default=None)
    return (best or {}).get("url") or ""


def _song(title: str, artists: str, duration_ms: Optional[int], uri: str, thumbnail: str) -> Optional[Song]:
    """Una canción de Spotify, pendiente de buscar en YouTube como «Artistas - Título»."""
    if not title or not uri.startswith("spotify:track:"):
        return None  # Archivos locales y episodios de podcast: no hay nada que buscar
    artists = re.sub(r"\s*,\s*", ", ", artists.replace("\xa0", " ")).strip()
    full_title = f"{artists} - {title}" if artists else title
    return Song(title=full_title, webpage_url=f"https://open.spotify.com/track/{uri.rsplit(':', 1)[1]}",
                thumbnail=thumbnail, duration=duration_ms / 1000 if duration_ms else None,
                search_query=full_title)


async def fetch_songs(query: str, limit: int) -> List[Song]:
    """
    Canciones de un enlace de Spotify (canción, álbum o playlist), como mucho `limit`.

    Raises:
        SearchError: si el enlace no es de algo que se pueda poner o no se pudo leer, con el motivo.
    """
    url = query.strip().strip("<>")
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers=HEADERS) as session:
            if _host(url) in SHORT_HOSTS:
                url = await _resolve_short_link(session, url)
            link = parse_link(url)
            if link is None or link[0] not in KINDS:
                raise SearchError("de Spotify solo puedo poner canciones, álbumes y playlists")
            kind, item_id = link
            async with session.get(EMBED_URL.format(kind=kind, id=item_id)) as resp:
                html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Spotify: no pude abrir {url}: {e!r}")
        raise SearchError("no pude conectar con Spotify") from e

    entity = _read_entity(html)
    cover = _cover(entity)
    if kind == "track":
        artists = ", ".join(artist.get("name", "") for artist in entity.get("artists") or [])
        songs = [_song(entity.get("title") or entity.get("name"), artists, entity.get("duration"),
                       entity.get("uri") or f"spotify:track:{item_id}", cover)]
    else:
        songs = [_song(track.get("title"), track.get("subtitle") or "", track.get("duration"),
                       track.get("uri") or "", cover)
                 for track in entity.get("trackList") or []]
    songs = [song for song in songs if song]
    logger.info(f"Spotify: {len(songs)} canciones de {kind} {item_id}")
    return songs[:limit]
