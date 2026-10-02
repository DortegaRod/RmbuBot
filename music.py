"""
Módulo de Gestión Musical para Discord.
Se encarga de la extracción de audio de YouTube, la gestión de colas por servidor,
y el control de reproducción utilizando FFmpeg y discord.py.
"""

import asyncio
import logging
import platform
import random
import re
import shlex
import shutil
import struct
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp

from config import MAX_QUEUE_SIZE, INACTIVITY_TIMEOUT, DEFAULT_VOLUME, EMPTY_CHANNEL_TIMEOUT

logger = logging.getLogger(__name__)

# Constantes para los estados del modo bucle (Loop)
LOOP_OFF = 0       # Sin repetición
LOOP_CURRENT = 1   # Repetir la canción actual
LOOP_QUEUE = 2     # Repetir toda la cola
LOOP_LABELS = {LOOP_OFF: "Off", LOOP_CURRENT: "🔂 Canción", LOOP_QUEUE: "🔁 Cola"}

STREAM_URL_TTL = 60 * 60       # Las URLs de audio de YouTube caducan: se re-extraen pasada 1 hora
MAX_CONSECUTIVE_FAILURES = 5   # Canciones seguidas que no cargan antes de dejar de intentarlo
FAST_FAIL_SECONDS = 2.0        # Si una pista "termina" antes de esto, FFmpeg no pudo reproducirla


# ==========================================
# CONFIGURACIÓN DE YT-DLP
# ==========================================

def _find_deno() -> Optional[str]:
    """
    yt-dlp necesita un motor JavaScript (Deno) para resolver los retos de YouTube.
    Primero se busca el paquete oficial de pip (`pip install deno`), que queda fuera del PATH
    si el bot se arranca sin activar el entorno virtual; si no está, se usa el `deno` del sistema.
    """
    try:
        import deno
        return deno.find_deno_bin()
    except Exception:
        return shutil.which("deno")


DENO_PATH = _find_deno()


class _YtdlLogger:
    """Pasa los avisos de yt-dlp al log del bot, sin repetir el mismo aviso una y otra vez."""

    def __init__(self):
        self._seen: Dict[str, float] = {}

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        now = time.monotonic()
        if now - self._seen.get(msg, float("-inf")) < 600:
            return
        if len(self._seen) > 200:
            self._seen.clear()
        self._seen[msg] = now
        logger.warning(f"[yt-dlp] {msg}")

    def error(self, msg):
        logger.debug(f"[yt-dlp] {msg}")  # Los errores se registran con contexto donde se capturan


_BASE_YDL_OPTIONS = {
    'quiet': True,
    'noprogress': True,
    'source_address': '0.0.0.0',  # Fuerza IPv4 (YouTube bloquea más a menudo el IPv6 de los servidores)
    'logger': _YtdlLogger(),
}
if DENO_PATH:
    _BASE_YDL_OPTIONS['js_runtimes'] = {'deno': {'path': DENO_PATH}}

# Configuración de yt-dlp para búsquedas rápidas (Extracción plana o Lazy Loading)
YDL_SEARCH_OPTIONS = {
    **_BASE_YDL_OPTIONS,
    'format': 'bestaudio/best',
    'noplaylist': False,
    'extract_flat': 'in_playlist',
    'default_search': 'ytsearch',
}

# Configuración de yt-dlp para la extracción del audio real (Just-In-Time).
# Sin forzar 'player_client': los clientes por defecto son los que mantienen al día los autores de yt-dlp.
YDL_EXTRACT_OPTIONS = {
    **_BASE_YDL_OPTIONS,
    'format': 'bestaudio/best',
    'noplaylist': True,
}


@dataclass
class Song:
    """
    Estructura de datos que representa una canción o flujo de audio.
    """
    title: str
    webpage_url: str
    thumbnail: str = ""
    stream_url: Optional[str] = None  # URL directa del archivo de audio (.mp3, .webm)
    requester: Optional[discord.Member] = None
    is_radio: bool = False  # True para emisoras en directo (stream permanente, sin yt-dlp)
    duration: Optional[float] = None  # Duración en segundos (None en radios/directos)
    stream_fetched_at: Optional[float] = None  # Cuándo se extrajo stream_url (reloj monotónico)
    user_agent: Optional[str] = None  # User-Agent con el que yt-dlp obtuvo la URL (FFmpeg debe usar el mismo)
    retried: bool = False  # Ya se reintentó una vez tras cortarse nada más empezar

    def __str__(self):
        return self.title

    def has_fresh_stream(self) -> bool:
        """True si hay URL de audio y no ha caducado (las de las radios no caducan)."""
        if not self.stream_url:
            return False
        if self.is_radio or self.stream_fetched_at is None:
            return True
        return time.monotonic() - self.stream_fetched_at < STREAM_URL_TTL


class MusicPlayer:
    """
    Controlador de reproducción individual para cada servidor (Guild).
    Mantiene el estado actual de la reproducción, la cola y el modo de repetición.
    """
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.loop_mode = LOOP_OFF
        self.volume = DEFAULT_VOLUME  # 0.0 - 1.0, ajustable con /volume
        self.skip_requested = False   # /skip avanza aunque el bucle sea de canción
        self.lock = asyncio.Lock()    # Evita que dos llamadas a play_next se pisen
        # Anuncios en el canal de texto (los gestiona controls.py mediante ganchos)
        self.text_channel: Optional[discord.abc.Messageable] = None  # Donde se usó /play o /radio
        self.now_playing_message: Optional[discord.Message] = None   # Tarjeta "Sonando ahora" con botones
        self.announced_song: Optional[Song] = None
        self.inactivity_task: Optional[asyncio.Task] = None  # Silencio (cola vacía)
        self.empty_task: Optional[asyncio.Task] = None       # Canal de voz sin humanos
        # Seguimiento del tiempo de reproducción (para la barra de progreso de /current)
        self.started_monotonic: Optional[float] = None  # Instante del segmento en curso
        self.paused_elapsed: float = 0.0                 # Segundos ya acumulados antes de pausas

    def add_song(self, song: Song) -> bool:
        """Añade una canción a la cola si no se ha superado el límite configurado."""
        if len(self.queue) >= MAX_QUEUE_SIZE:
            return False
        self.queue.append(song)
        return True

    def free_slots(self) -> int:
        """Huecos libres en la cola."""
        return max(0, MAX_QUEUE_SIZE - len(self.queue))

    def get_next(self) -> Optional[Song]:
        """
        Determina cuál es la siguiente canción a reproducir basándose
        en el estado actual de la cola y el modo de repetición activo.
        """
        last_song = self.current
        skip = self.skip_requested
        self.skip_requested = False

        # Lógica de bucles (un /skip siempre avanza, aunque el bucle sea de canción)
        if self.loop_mode == LOOP_CURRENT and last_song and not skip:
            return last_song

        if self.loop_mode == LOOP_QUEUE and last_song:
            # Conserva su URL de audio: se re-extrae solo si ha caducado (ver Song.has_fresh_stream)
            self.queue.append(last_song)

        # Extraer la siguiente pista
        if self.queue:
            return self.queue.popleft()

        return None

    def shuffle_queue(self):
        """Mezcla aleatoriamente los elementos en la cola actual."""
        if len(self.queue) > 0:
            temp_list = list(self.queue)
            random.shuffle(temp_list)
            self.queue = deque(temp_list)

    def clear_queue(self):
        """Vacía la cola de reproducción por completo."""
        self.queue.clear()

    def cleanup(self):
        """Cancela las tareas en segundo plano y vacía el estado (al desconectar)."""
        try:
            running = asyncio.current_task()
        except RuntimeError:
            running = None
        for task in (self.inactivity_task, self.empty_task):
            # No cancelar la tarea que está ejecutando esta limpieza (p. ej. la de inactividad)
            if task and task is not running and not task.done():
                task.cancel()
        self.inactivity_task = None
        self.empty_task = None
        self.queue.clear()
        self.current = None

    def mark_started(self):
        """Reinicia el cronómetro al empezar una canción nueva."""
        self.paused_elapsed = 0.0
        self.started_monotonic = time.monotonic()

    def mark_paused(self):
        """Congela el cronómetro al pausar."""
        if self.started_monotonic is not None:
            self.paused_elapsed += time.monotonic() - self.started_monotonic
            self.started_monotonic = None

    def mark_resumed(self):
        """Reanuda el cronómetro tras una pausa."""
        if self.started_monotonic is None:
            self.started_monotonic = time.monotonic()

    def get_elapsed(self) -> float:
        """Segundos reproducidos de la canción actual (descontando el tiempo en pausa)."""
        elapsed = self.paused_elapsed
        if self.started_monotonic is not None:
            elapsed += time.monotonic() - self.started_monotonic
        return elapsed

    def remove_at(self, index: int) -> Optional[Song]:
        """
        Elimina y devuelve la canción en la posición `index` (base 0) de la cola.

        Returns:
            Song | None: La canción eliminada, o None si el índice es inválido.
        """
        if 0 <= index < len(self.queue):
            temp = list(self.queue)
            song = temp.pop(index)
            self.queue = deque(temp)
            return song
        return None


class MusicManager:
    """
    Gestor global que administra las instancias de MusicPlayer
    para múltiples servidores simultáneamente.
    """
    def __init__(self):
        self.players: Dict[int, MusicPlayer] = {}
        # Ganchos opcionales (los registra bot.py) para anunciar en el canal de texto
        self.on_track_start: Optional[Callable[[MusicPlayer, Song], Awaitable[None]]] = None
        self.on_track_error: Optional[Callable[[MusicPlayer, Optional[Song], str], Awaitable[None]]] = None
        self.on_player_end: Optional[Callable[[MusicPlayer], Awaitable[None]]] = None

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        """Recupera o crea un reproductor asociado a un servidor específico."""
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(guild)
        return self.players[guild.id]

    def peek(self, guild_id: Optional[int]) -> Optional[MusicPlayer]:
        """Recupera el reproductor de un servidor SIN crearlo (para consultas)."""
        return self.players.get(guild_id) if guild_id is not None else None

    def remove_player(self, guild_id: int) -> Optional[MusicPlayer]:
        """Elimina el reproductor de un servidor (cancelando sus tareas) para liberar memoria."""
        player = self.players.pop(guild_id, None)
        if player:
            player.cleanup()
        return player

    async def notify(self, hook_name: str, *args):
        """Ejecuta un gancho sin que un fallo al anunciar afecte a la reproducción."""
        hook = getattr(self, hook_name, None)
        if hook is None:
            return
        try:
            await hook(*args)
        except Exception:
            logger.exception(f"Error en el gancho {hook_name}")


music_manager = MusicManager()


# ==========================================
# BÚSQUEDA Y EXTRACCIÓN
# ==========================================

_UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]"}


class SearchError(Exception):
    """Error de búsqueda con un motivo apto para mostrarlo en Discord."""


def _clean_query(query: str) -> str:
    """
    Los enlaces de "Mix" de YouTube (…&list=RD…) son listas casi infinitas generadas
    automáticamente: quien pega uno quiere ESA canción, no cientos. Se deja solo el vídeo.
    """
    query = query.strip().strip("<>")  # Discord permite <enlace> para ocultar la vista previa
    try:
        parsed = urlparse(query)
    except ValueError:
        return query
    host = (parsed.hostname or "").lower()
    params = parse_qs(parsed.query)
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/")
    elif (host == "youtube.com" or host.endswith(".youtube.com")) and parsed.path == "/watch":
        video_id = (params.get("v") or [""])[0]
    else:
        return query
    playlist_id = (params.get("list") or [""])[0]
    if video_id and playlist_id.startswith("RD"):
        return f"https://www.youtube.com/watch?v={video_id}"
    return query


def _entry_to_song(entry: dict) -> Optional[Song]:
    """Convierte un resultado de yt-dlp (plano o completo) en Song."""
    title = entry.get('title') or 'Desconocido'
    if title in _UNAVAILABLE_TITLES:
        return None  # Vídeos privados/borrados dentro de playlists: fallarían al reproducirse

    video_id = entry.get('id')
    extractor = (entry.get('ie_key') or entry.get('extractor_key') or '').lower()
    if extractor == 'youtube' and video_id:
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    else:
        webpage_url = entry.get('webpage_url') or entry.get('url')
        thumbnails = entry.get('thumbnails') or []
        thumbnail = entry.get('thumbnail') or (thumbnails[-1].get('url') if thumbnails else '')

    if not webpage_url or not str(webpage_url).startswith('http'):
        return None
    return Song(title=title, webpage_url=webpage_url, thumbnail=thumbnail or '', duration=entry.get('duration'))


def _apply_stream_info(song: Song, info: dict):
    """Copia a la canción la URL de audio elegida por yt-dlp (y lo que FFmpeg necesita para usarla)."""
    url = info.get('url')
    headers = info.get('http_headers') or {}
    if not url:
        # Respaldo: el mejor formato de SOLO audio. Ojo: los 'storyboard' (miniaturas de la barra
        # de progreso) también tienen vcodec='none', por eso se exige que tengan códec de audio.
        audio = [f for f in info.get('formats') or []
                 if f.get('url') and f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')]
        if audio:
            best = max(audio, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            url, headers = best['url'], best.get('http_headers') or headers

    song.stream_url = url
    song.stream_fetched_at = time.monotonic() if url else None
    song.user_agent = headers.get('User-Agent')
    if info.get('is_live'):
        song.duration = None  # Directos: sin barra de progreso
    elif not song.duration:
        song.duration = info.get('duration')


async def search_youtube(query: str, limit: int = MAX_QUEUE_SIZE) -> List[Song]:
    """
    Realiza una búsqueda rápida o procesa una playlist devolviendo la metadata básica.
    `limit` corta las playlists en origen (antes se descargaba la lista entera aunque no cupiera).

    Raises:
        SearchError: si YouTube rechaza el enlace (privado, inexistente...), con el motivo.
    """
    query = _clean_query(query)
    options = dict(YDL_SEARCH_OPTIONS, playlist_items=f"1-{max(1, limit)}")

    def extract():
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(query, download=False)

    try:
        info = await asyncio.to_thread(extract)
    except Exception as e:
        logger.error(f"Error en la búsqueda '{query}': {e}")
        raise SearchError(_friendly_error(e)) from e
    if not info:
        return []

    if info.get('_type') == 'playlist' or 'entries' in info:
        entries = [entry for entry in info.get('entries') or [] if entry]
        songs = [song for song in map(_entry_to_song, entries) if song]
        return songs[:limit]

    # Vídeo suelto: yt-dlp ya eligió el audio, así no hay que extraerlo otra vez al reproducirlo
    song = _entry_to_song(info)
    if song is None:
        return []
    _apply_stream_info(song, info)
    return [song]


def _friendly_error(error: Exception) -> str:
    """Traduce los errores habituales de yt-dlp a algo entendible en Discord."""
    text = str(error).replace("ERROR: ", "")
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube está bloqueando al servidor del bot (pide verificar que no es un robot)"
    if "private video" in low:
        return "el vídeo es privado"
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return "el vídeo tiene restricción de edad"
    if "copyright" in low:
        return "el vídeo está bloqueado por derechos de autor"
    if "unviewable" in low:
        return "esa lista de YouTube no se puede abrir (los «Mix» automáticos no son playlists)"
    if "does not exist" in low:
        return "ese enlace no existe o es privado"
    if "unsupported url" in low:
        return "ese enlace no es compatible"
    if "not available" in low or "unavailable" in low:
        return "el vídeo no está disponible"
    return text[:180]


async def _prepare_stream(song: Song) -> Optional[str]:
    """Garantiza que la canción tenga una URL de audio válida. Devuelve el motivo si no se pudo."""
    if song.has_fresh_stream():
        return None
    if song.is_radio:
        return "la emisora no tiene URL"

    # --- EXTRACCIÓN JUST IN TIME ---
    logger.info(f"Extrayendo URL de audio real para: {song.title}")

    def extract():
        with yt_dlp.YoutubeDL(YDL_EXTRACT_OPTIONS) as ydl:
            return ydl.extract_info(song.webpage_url, download=False)

    try:
        info = await asyncio.to_thread(extract)
    except Exception as e:
        return _friendly_error(e)
    if not info:
        return "YouTube no devolvió información"
    _apply_stream_info(song, info)
    return None if song.stream_url else "no encontré ningún formato de audio"


def _ffmpeg_options(song: Song) -> dict:
    """Opciones de FFmpeg según el tipo de audio."""
    before = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
    if song.is_radio and '.m3u8' not in (song.stream_url or ''):
        # Emisoras 24/7: si su servidor corta la conexión, volver a conectar.
        # (En canciones no: al llegar al final del archivo provocaría reintentos y silencios.)
        before += ' -reconnect_at_eof 1'
    if song.user_agent:
        before += f' -user_agent {shlex.quote(song.user_agent)}'
    return {'before_options': before, 'options': '-vn'}  # -vn: sin vídeo, ahorra recursos


# ==========================================
# MOTOR DE REPRODUCCIÓN
# ==========================================

def _is_active(voice_client: discord.VoiceClient, player: MusicPlayer) -> bool:
    """True mientras el bot siga conectado y este reproductor no haya sido retirado (/stop)."""
    return voice_client.is_connected() and music_manager.peek(player.guild.id) is player


class _StderrTail:
    """
    Guarda lo último que FFmpeg escribe en su salida de errores, para poder explicar por qué
    falló una canción. discord.py no lo transmite cuando el audio pasa por PCMVolumeTransformer.
    """

    def __init__(self, limit: int = 4000):
        self._limit = limit
        self._data = bytearray()

    def write(self, chunk: bytes) -> int:  # Lo llama discord.py desde su hilo lector
        self._data += chunk
        del self._data[:-self._limit]
        return len(chunk)

    def text(self) -> str:
        return self._data.decode("utf-8", "replace").strip()


def _explain_playback_failure(ffmpeg_text: str, error, connected: bool) -> str:
    """Motivo legible de que una pista se cortara al empezar (sin mostrar enlaces)."""
    low = ffmpeg_text.lower()
    if "403" in low:
        return "YouTube rechazó la descarga del audio, error 403"
    if "404" in low:
        return "el enlace de audio ya no existe, error 404"
    if any(k in low for k in ("timed out", "connection refused", "network is unreachable", "connection reset",
                              "name or service not known", "temporary failure in name resolution")):
        return "fallo de red al descargar el audio"
    if "unrecognized option" in low or "option not found" in low:
        return "esta versión de FFmpeg no reconoce una opción; actualízala"
    if "invalid data found" in low:
        return "FFmpeg no entiende el audio recibido"
    if "protocol not found" in low:
        return "FFmpeg no tiene soporte para HTTPS"
    if ffmpeg_text:
        last_line = re.sub(r"https?://\S+", "<enlace>", ffmpeg_text.splitlines()[-1])
        return f"FFmpeg dice: {last_line[:150]}"
    if error:
        return f"{type(error).__name__}: {str(error)[:150]}"
    if not connected:
        return "se perdió la conexión de voz"
    return "sin detalles, revisa el log del bot"


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """
    Motor de reproducción. Obtiene la siguiente canción, extrae el flujo de audio
    (si es necesario) y la inyecta en el cliente de voz de Discord.
    Si una canción no carga, se avisa y se pasa a la siguiente (sin repetirla en bucle).
    """
    async with player.lock:
        if player.inactivity_task:
            player.inactivity_task.cancel()
            player.inactivity_task = None

        failures = 0
        while True:
            if not _is_active(voice_client, player):
                return
            if voice_client.is_playing() or voice_client.is_paused():
                return  # Otra llamada ya puso algo a sonar

            song = player.get_next()

            # Si no hay canciones, iniciamos temporizador de inactividad
            if song is None:
                player.current = None
                player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
                await music_manager.notify('on_player_end', player)
                return

            player.current = song
            error = await _prepare_stream(song)

            if error is None:
                if not _is_active(voice_client, player):
                    return  # Pararon la música mientras se extraía el audio
                try:
                    ffmpeg_log = _StderrTail()
                    raw_source = discord.FFmpegPCMAudio(song.stream_url, stderr=ffmpeg_log, **_ffmpeg_options(song))
                    # Envolvemos en PCMVolumeTransformer para permitir ajuste de volumen en vivo (/volume)
                    source = discord.PCMVolumeTransformer(raw_source, volume=player.volume)
                    loop = asyncio.get_running_loop()
                    voice_client.play(source, after=lambda e, s=song, f=ffmpeg_log:
                                      _schedule_track_end(loop, voice_client, player, s, e, f))
                    player.mark_started()  # Arranca el cronómetro para la barra de progreso
                    logger.info(f"▶️ Sonando: {song.title}")
                    await music_manager.notify('on_track_start', player, song)
                    return
                except Exception as e:
                    error = f"error de audio ({e})"

            logger.warning(f"No se pudo reproducir '{song.title}': {error}")
            player.current = None  # Así el bucle no la repite ni la vuelve a encolar
            await music_manager.notify('on_track_error', player, song, error)
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                await music_manager.notify(
                    'on_track_error', player, None,
                    f"{failures} canciones seguidas no se han podido cargar, así que paro aquí. "
                    f"Revisa el log del servidor (o ejecuta diagnose.py)."
                )
                player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
                return


def _schedule_track_end(loop, voice_client, player: MusicPlayer, song: Song, error,
                        ffmpeg_log: Optional[_StderrTail] = None):
    """Callback 'after' de discord.py: se ejecuta en el hilo de audio al terminar una pista."""
    elapsed = player.get_elapsed()
    future = asyncio.run_coroutine_threadsafe(
        _on_track_end(voice_client, player, song, error, elapsed, ffmpeg_log), loop
    )
    future.add_done_callback(_log_future_error)


def _log_future_error(future):
    if not future.cancelled() and future.exception() is not None:
        logger.error("Error en el motor de reproducción", exc_info=future.exception())


async def _on_track_end(voice_client, player: MusicPlayer, song: Song, error, elapsed: float,
                        ffmpeg_log: Optional[_StderrTail] = None):
    """Decide qué hacer al terminar una pista y pone la siguiente."""
    if music_manager.peek(player.guild.id) is not player:
        return  # Se paró la música o se desconectó el bot
    if error:
        logger.error(f"Error durante la reproducción de '{song.title}': {error}")

    very_short_song = bool(song.duration and song.duration <= FAST_FAIL_SECONDS + 1)
    failed_to_start = elapsed < FAST_FAIL_SECONDS and not very_short_song and not player.skip_requested
    if failed_to_start and player.current is song:
        # FFmpeg se cerró nada más empezar. Se espera un instante a que llegue su mensaje de error
        # (lo escribe justo al cerrarse) para dejarlo en el log y poder explicarlo.
        await asyncio.sleep(0.3)
        if music_manager.peek(player.guild.id) is not player:
            return
        ffmpeg_text = ffmpeg_log.text() if ffmpeg_log else ""
        logger.warning(f"'{song.title}' se cortó a los {elapsed:.1f}s. FFmpeg: {ffmpeg_text or '(sin mensajes)'}")
        player.current = None
        if not song.retried:
            # Un reintento con una URL nueva (la anterior pudo caducar)
            song.retried = True
            if not song.is_radio:
                song.stream_url = None
            player.queue.appendleft(song)
        else:
            reason = _explain_playback_failure(ffmpeg_text, error, voice_client.is_connected())
            logger.warning(f"'{song.title}' se cortó nada más empezar dos veces; se descarta ({reason})")
            await music_manager.notify('on_track_error', player, song,
                                       f"la transmisión se cortó nada más empezar ({reason})")
    else:
        song.retried = False

    await play_next(voice_client, player)


async def disconnect_player(guild: discord.Guild) -> bool:
    """
    Para la música, vacía la cola y saca al bot del canal de voz.
    Devuelve True si el bot estaba conectado.
    """
    player = music_manager.remove_player(guild.id)
    voice_client = guild.voice_client
    was_connected = bool(voice_client and voice_client.is_connected())
    if voice_client:
        try:
            await voice_client.disconnect(force=True)
        except Exception as e:
            logger.warning(f"Error al desconectar del canal de voz: {e}")
    if player:
        await music_manager.notify('on_player_end', player)
    return was_connected


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Desconecta al bot tras un periodo de inactividad para liberar recursos."""
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
    except asyncio.CancelledError:
        return
    if _is_active(voice_client, player) and not voice_client.is_playing() and not voice_client.is_paused():
        logger.info("💤 Sin música desde hace rato, desconectando para ahorrar recursos.")
        await disconnect_player(player.guild)


async def empty_channel_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer,
                                   timeout: int = EMPTY_CHANNEL_TIMEOUT):
    """
    Desconecta al bot si su canal de voz se queda sin humanos durante `timeout` segundos.
    Se cancela automáticamente si alguien vuelve a entrar antes de que expire.
    """
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return  # Alguien volvió a entrar; cancelación normal
    player.empty_task = None
    if _is_active(voice_client, player) and not [m for m in voice_client.channel.members if not m.bot]:
        logger.info("👋 Canal de voz vacío, desconectando para ahorrar recursos.")
        await disconnect_player(player.guild)


def log_dependency_status():
    """Avisa al arrancar de las dependencias que hoy exigen Discord (voz) y YouTube."""
    bits = struct.calcsize("P") * 8
    logger.info(f"🖥️ {platform.system()} {platform.machine()} ({bits} bits) · Python {platform.python_version()}")
    logger.info(f"🎵 discord.py {discord.__version__} | yt-dlp {yt_dlp.version.__version__}")
    if discord.version_info < (2, 7):
        logger.error("❌ discord.py es anterior a 2.7: desde marzo de 2026 Discord exige cifrado E2EE (DAVE) "
                     "en voz y el bot NO podrá entrar a los canales. Actualiza: pip install -U \"discord.py[voice]\"")
    try:
        import davey  # noqa: F401  (cifrado de voz DAVE)
    except ImportError:
        logger.error("❌ Falta el paquete 'davey' (cifrado de voz DAVE): pip install -U \"discord.py[voice]\"")
    try:
        import yt_dlp_ejs  # noqa: F401
    except ImportError:
        logger.warning("⚠️ Falta yt-dlp-ejs para YouTube: pip install -U \"yt-dlp[default]\"")
    if DENO_PATH:
        logger.info(f"🦕 Deno para YouTube: {DENO_PATH}")
    elif bits == 32:
        logger.info("ℹ️ Sistema de 32 bits: Deno no existe para él. YouTube funciona sin él por ahora; "
                    "si algún día empieza a fallar, la solución es pasar a un sistema de 64 bits.")
    else:
        logger.warning("⚠️ Deno no encontrado: YouTube puede fallar o dar peor calidad. Instálalo: pip install -U deno")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.error("❌ FFmpeg no encontrado: la música no funcionará (Raspberry Pi OS: sudo apt install ffmpeg)")
    else:
        try:
            version = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10).stdout
            logger.info(f"🎬 {version.splitlines()[0]}")
        except Exception as e:
            logger.warning(f"⚠️ FFmpeg está en {ffmpeg} pero no responde: {e}")
