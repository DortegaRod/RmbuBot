import discord
import asyncio
import yt_dlp
import logging
import random
from typing import Optional, Dict, List
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, INACTIVITY_TIMEOUT

logger = logging.getLogger(__name__)

# Constantes para el modo de bucle
LOOP_OFF = 0
LOOP_CURRENT = 1
LOOP_QUEUE = 2

# Configuración optimizada
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'extract_flat': 'in_playlist',  # Extrae solo metadata de playlists (RÁPIDO)
    'playlistend': 100,  # Aumentado de 50 a 100
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'force_ipv4': True,
    'ignoreerrors': True,  # Continúa si alguna canción falla
}

# Opciones para extraer una canción individual (completa)
YDL_OPTIONS_SINGLE = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'source_address': '0.0.0.0',
    'force_ipv4': True,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}


@dataclass
class Song:
    url: str
    title: str
    webpage_url: str
    thumbnail: str
    requester: Optional[discord.Member] = None
    # Nuevo: para playlists, guardamos la URL original para extraer el stream después
    needs_extraction: bool = False
    original_url: Optional[str] = None

    def __str__(self): return self.title


class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.loop_mode = LOOP_OFF
        self.inactivity_task: Optional[asyncio.Task] = None
        # Nuevo: tarea de carga en segundo plano
        self.loading_task: Optional[asyncio.Task] = None

    def add_song(self, song: Song) -> bool:
        if len(self.queue) >= MAX_QUEUE_SIZE:
            logger.warning(f"Cola llena ({MAX_QUEUE_SIZE}), rechazando canción: {song.title}")
            return False
        self.queue.append(song)
        return True

    def get_next(self) -> Optional[Song]:
        last_song = self.current

        # Loop Canción Actual
        if self.loop_mode == LOOP_CURRENT and last_song:
            return last_song

        # Loop Cola: la que terminó va al final
        if self.loop_mode == LOOP_QUEUE and last_song:
            self.queue.append(last_song)

        # Sacar la siguiente
        if self.queue:
            return self.queue.popleft()

        return None

    def shuffle_queue(self):
        """Mezcla aleatoriamente las canciones en espera."""
        if len(self.queue) > 0:
            temp_list = list(self.queue)
            random.shuffle(temp_list)
            self.queue = deque(temp_list)

    def clear_queue(self):
        self.queue.clear()
        # Cancelar carga en segundo plano si existe
        if self.loading_task and not self.loading_task.done():
            self.loading_task.cancel()
            self.loading_task = None


class MusicManager:
    def __init__(self):
        self.players: Dict[int, MusicPlayer] = {}

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(guild)
        return self.players[guild.id]

    def remove_player(self, guild_id: int):
        if guild_id in self.players:
            player = self.players[guild_id]
            # Cancelar tarea de carga si existe
            if player.loading_task and not player.loading_task.done():
                player.loading_task.cancel()
            del self.players[guild_id]


music_manager = MusicManager()


async def extract_song_url(song: Song) -> Optional[str]:
    """
    Extrae la URL de stream de una canción que solo tiene metadata.
    Se usa cuando needs_extraction=True.
    """
    try:
        loop = asyncio.get_event_loop()

        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS_SINGLE) as ydl:
                return ydl.extract_info(song.original_url or song.webpage_url, download=False)

        info = await loop.run_in_executor(None, extract)
        if not info:
            return None

        # Obtener URL de stream
        stream_url = info.get('url')
        if not stream_url and 'formats' in info:
            f_audio = [f for f in info['formats'] if f.get('vcodec') == 'none' and f.get('url')]
            if f_audio:
                stream_url = f_audio[0]['url']

        return stream_url
    except Exception as e:
        logger.error(f"Error extrayendo URL para {song.title}: {e}")
        return None


async def search_youtube(query: str) -> List[Song]:
    """
    Busca en YouTube. Para playlists, devuelve rápidamente solo metadata
    (la URL de stream se extrae después cuando se vaya a reproducir).
    """
    try:
        loop = asyncio.get_event_loop()

        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                return ydl.extract_info(query, download=False)

        info = await loop.run_in_executor(None, extract)
        if not info:
            logger.warning("yt-dlp no devolvió información")
            return []

        songs = []
        entries = info.get('entries', [info])

        # Detectar si es playlist
        is_playlist = 'entries' in info and len(entries) > 1

        # Límite de seguridad para playlists muy largas
        MAX_PROCESS = 150
        if is_playlist and len(entries) > MAX_PROCESS:
            logger.warning(f"Playlist muy larga ({len(entries)} canciones), procesando solo las primeras {MAX_PROCESS}")
            entries = entries[:MAX_PROCESS]

        for idx, entry in enumerate(entries):
            if not entry:
                logger.debug(f"Entrada {idx} es None, saltando")
                continue

            try:
                # Si es playlist y la entrada solo tiene metadata básica
                if is_playlist and entry.get('ie_key') == 'Youtube' and not entry.get('url'):
                    # Solo tenemos metadata, marcar para extracción posterior
                    video_id = entry.get('id')
                    if not video_id:
                        logger.debug(f"Entrada {idx} sin video_id, saltando")
                        continue

                    # Extraer thumbnail de forma segura
                    thumbnail = entry.get('thumbnail', '')
                    if not thumbnail:
                        thumbnails = entry.get('thumbnails', [])
                        if thumbnails and len(thumbnails) > 0:
                            thumbnail = thumbnails[0].get('url', '')

                    songs.append(Song(
                        url='',  # Se llenará después
                        title=entry.get('title', 'Desconocido'),
                        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                        thumbnail=thumbnail,
                        needs_extraction=True,
                        original_url=f"https://www.youtube.com/watch?v={video_id}"
                    ))
                else:
                    # Es una sola canción o ya tenemos toda la info
                    stream_url = entry.get('url')
                    if not stream_url and 'formats' in entry:
                        f_audio = [f for f in entry['formats'] if f.get('vcodec') == 'none' and f.get('url')]
                        if f_audio:
                            stream_url = f_audio[0]['url']

                    if not stream_url:
                        # Marcar para extracción
                        video_id = entry.get('id')
                        if video_id:
                            # Extraer thumbnail de forma segura
                            thumbnail = entry.get('thumbnail', '')
                            if not thumbnail:
                                thumbnails = entry.get('thumbnails', [])
                                if thumbnails and len(thumbnails) > 0:
                                    thumbnail = thumbnails[0].get('url', '')

                            songs.append(Song(
                                url='',
                                title=entry.get('title', 'Desconocido'),
                                webpage_url=entry.get('webpage_url') or f"https://www.youtube.com/watch?v={video_id}",
                                thumbnail=thumbnail,
                                needs_extraction=True,
                                original_url=entry.get('webpage_url') or f"https://www.youtube.com/watch?v={video_id}"
                            ))
                        continue

                    # Extraer thumbnail de forma segura
                    thumbnail = entry.get('thumbnail', '')
                    if not thumbnail:
                        thumbnails = entry.get('thumbnails', [])
                        if thumbnails and len(thumbnails) > 0:
                            thumbnail = thumbnails[0].get('url', '')

                    songs.append(Song(
                        url=stream_url,
                        title=entry.get('title', 'Desconocido'),
                        webpage_url=entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        thumbnail=thumbnail
                    ))
            except Exception as e:
                logger.error(f"Error procesando entrada {idx}: {e}")
                continue  # Continuar con la siguiente canción

        logger.info(f"Búsqueda completada: {len(songs)} canciones de {len(entries)} entradas procesadas")
        return songs
    except Exception as e:
        logger.error(f"Error general en búsqueda: {e}", exc_info=True)
        return []


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Reproduce la siguiente canción en la cola."""
    if not voice_client or not voice_client.is_connected():
        return

    if player.inactivity_task:
        player.inactivity_task.cancel()
        player.inactivity_task = None

    song = player.get_next()
    if song is None:
        player.current = None
        player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
        return

    player.current = song

    try:
        # Si la canción necesita extracción, hacerla ahora
        if song.needs_extraction or not song.url:
            logger.info(f"Extrayendo URL de stream para: {song.title}")
            stream_url = await extract_song_url(song)
            if not stream_url:
                logger.error(f"No se pudo extraer URL para {song.title}, saltando...")
                # Saltar a la siguiente
                await play_next(voice_client, player)
                return
            song.url = stream_url
            song.needs_extraction = False

        source = discord.FFmpegPCMAudio(song.url, **FFMPEG_OPTIONS)
        voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                play_next(voice_client, player),
                voice_client.client.loop
            ) if not e else logger.error(f"Error en reproducción: {e}")
        )
        logger.info(f"Reproduciendo: {song.title}")
    except Exception as e:
        logger.error(f"Error al reproducir {song.title}: {e}")
        # Intentar con la siguiente canción
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Desconecta el bot después de inactividad."""
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        if voice_client.is_connected() and not voice_client.is_playing():
            logger.info(f"Desconectando por inactividad en {voice_client.guild.name}")
            await voice_client.disconnect()
            music_manager.remove_player(voice_client.guild.id)
    except asyncio.CancelledError:
        # Tarea cancelada, normal
        pass
    except Exception as e:
        logger.error(f"Error en desconexión por inactividad: {e}")