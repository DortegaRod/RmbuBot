"""
Módulo de Gestión Musical para Discord.
Se encarga de la extracción de audio de YouTube, la gestión de colas por servidor,
y el control de reproducción utilizando FFmpeg y discord.py.
"""

import discord
import asyncio
import yt_dlp
import logging
import random
from typing import Optional, Dict, List
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, INACTIVITY_TIMEOUT, DEFAULT_VOLUME

logger = logging.getLogger(__name__)

# Constantes para los estados del modo bucle (Loop)
LOOP_OFF = 0       # Sin repetición
LOOP_CURRENT = 1   # Repetir la canción actual
LOOP_QUEUE = 2     # Repetir toda la cola

# Configuración de yt-dlp para búsquedas rápidas (Extracción plana o Lazy Loading)
YDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'playlistmaxentries': 150,
    'extract_flat': 'in_playlist',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'force_ipv4': True,
}

# Configuración de yt-dlp para la extracción del audio real (Just-In-Time)
YDL_EXTRACT_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'source_address': '0.0.0.0',
    'force_ipv4': True,
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'] # Evasión de bloqueos (HTTP 403) de YouTube
        }
    }
}

# Opciones de FFmpeg para reconexión automática en caso de microcortes de red
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1',
    'options': '-vn' # Desactiva el procesamiento de vídeo para ahorrar recursos
}


@dataclass
class Song:
    """
    Estructura de datos que representa una canción o flujo de audio.
    """
    title: str
    webpage_url: str
    thumbnail: str
    stream_url: Optional[str] = None  # URL directa del archivo de audio (.mp3, .webm)
    requester: Optional[discord.Member] = None
    is_radio: bool = False  # True para emisoras en directo (stream permanente, sin yt-dlp)

    def __str__(self):
        return self.title


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
        self.inactivity_task: Optional[asyncio.Task] = None  # Silencio (cola vacía)
        self.empty_task: Optional[asyncio.Task] = None       # Canal de voz sin humanos

    def add_song(self, song: Song) -> bool:
        """Añade una canción a la cola si no se ha superado el límite configurado."""
        if len(self.queue) >= MAX_QUEUE_SIZE:
            return False
        self.queue.append(song)
        return True

    def get_next(self) -> Optional[Song]:
        """
        Determina cuál es la siguiente canción a reproducir basándose
        en el estado actual de la cola y el modo de repetición activo.
        """
        last_song = self.current

        # Lógica de bucles
        if self.loop_mode == LOOP_CURRENT and last_song:
            return last_song

        if self.loop_mode == LOOP_QUEUE and last_song:
            # Las radios conservan su stream permanente; solo invalidamos el token
            # temporal de audio de YouTube (que caduca y hay que re-extraer con yt-dlp).
            if not last_song.is_radio:
                last_song.stream_url = None
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

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        """Recupera o crea un reproductor asociado a un servidor específico."""
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(guild)
        return self.players[guild.id]

    def remove_player(self, guild_id: int):
        """Elimina el reproductor de un servidor para liberar memoria."""
        if guild_id in self.players:
            del self.players[guild_id]


music_manager = MusicManager()


async def search_youtube(query: str) -> List[Song]:
    """
    Realiza una búsqueda rápida o procesa una playlist devolviendo la metadata básica.
    No extrae flujos de audio pesados para garantizar una respuesta inmediata.
    """
    try:
        loop = asyncio.get_event_loop()

        def extract():
            with yt_dlp.YoutubeDL(YDL_SEARCH_OPTIONS) as ydl:
                return ydl.extract_info(query, download=False)

        info = await loop.run_in_executor(None, extract)
        if not info: return []

        songs = []
        entries = info.get('entries', [info])

        for entry in entries:
            if not entry: continue

            webpage_url = entry.get('webpage_url')
            if not webpage_url:
                url_field = entry.get('url', '')
                if 'youtube.com' in url_field or 'youtu.be' in url_field:
                    webpage_url = url_field
                else:
                    webpage_url = f"https://www.youtube.com/watch?v={entry.get('id')}"

            thumbnail = ''
            if entry.get('thumbnails'):
                thumbnail = entry['thumbnails'][0]['url']
            elif entry.get('thumbnail'):
                thumbnail = entry.get('thumbnail')

            if not webpage_url: continue

            songs.append(Song(
                title=entry.get('title', 'Desconocido'),
                webpage_url=webpage_url,
                thumbnail=thumbnail,
                stream_url=None  # La carga pesada se delega al momento de reproducción
            ))

        return songs
    except Exception as e:
        logger.error(f"Error en búsqueda plana: {e}")
        return []


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """
    Motor de reproducción. Obtiene la siguiente canción, extrae el flujo de audio
    (si es necesario) y la inyecta en el cliente de voz de Discord.
    """
    if not voice_client or not voice_client.is_connected(): return
    if player.inactivity_task:
        player.inactivity_task.cancel()
        player.inactivity_task = None

    song = player.get_next()

    # Si no hay canciones, iniciamos temporizador de inactividad
    if song is None:
        player.current = None
        player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
        return

    player.current = song

    # --- EXTRACCIÓN JUST IN TIME ---
    # Solo extraemos la URL de audio pesado si la canción no lo tiene (ej. YouTube)
    if not song.stream_url:
        try:
            logger.info(f"Extrayendo URL de audio real para: {song.title}")
            loop = asyncio.get_event_loop()

            def extract_single():
                with yt_dlp.YoutubeDL(YDL_EXTRACT_OPTIONS) as ydl:
                    return ydl.extract_info(song.webpage_url, download=False)

            info = await loop.run_in_executor(None, extract_single)

            if info:
                song.stream_url = info.get('url')
                # Búsqueda fallback del formato de mayor calidad de solo audio
                if not song.stream_url and 'formats' in info:
                    f_audio = [f for f in info['formats'] if f.get('vcodec') == 'none' and f.get('url')]
                    if f_audio:
                        song.stream_url = f_audio[-1]['url']

        except Exception as e:
            logger.error(f"Fallo al cargar la canción {song.title}: {e}")
            await play_next(voice_client, player)
            return

    if not song.stream_url:
        logger.warning(f"No se pudo extraer el stream para {song.title}")
        await play_next(voice_client, player)
        return

    # Inyección en FFmpeg
    try:
        raw_source = discord.FFmpegPCMAudio(song.stream_url, **FFMPEG_OPTIONS)
        # Envolvemos en PCMVolumeTransformer para permitir ajuste de volumen en vivo (/volume)
        source = discord.PCMVolumeTransformer(raw_source, volume=player.volume)
        # El callback 'after' crea un bucle infinito que llama a esta misma función al terminar
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(voice_client, player), voice_client.client.loop))
        logger.info(f"▶️ Sonando correctamente: {song.title}")
    except Exception as e:
        logger.error(f"Error audio FFmpeg: {e}")
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Desconecta al bot tras un periodo de inactividad para liberar recursos."""
    await asyncio.sleep(INACTIVITY_TIMEOUT)
    if voice_client.is_connected() and not voice_client.is_playing():
        await voice_client.disconnect()
        music_manager.remove_player(voice_client.guild.id)


async def empty_channel_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer,
                                   timeout: int = 60):
    """
    Desconecta al bot si su canal de voz se queda sin humanos durante `timeout` segundos.
    Se cancela automáticamente si alguien vuelve a entrar antes de que expire.
    """
    try:
        await asyncio.sleep(timeout)
        if voice_client.is_connected():
            humans = [m for m in voice_client.channel.members if not m.bot]
            if not humans:
                logger.info("👋 Canal de voz vacío, desconectando para ahorrar recursos.")
                await voice_client.disconnect()
                music_manager.remove_player(voice_client.guild.id)
    except asyncio.CancelledError:
        pass  # Alguien volvió a entrar; cancelación normal
    finally:
        player.empty_task = None