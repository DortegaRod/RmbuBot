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

LOOP_OFF = 0
LOOP_CURRENT = 1
LOOP_QUEUE = 2

# 1. OPCIONES PARA BÚSQUEDA RAPIDA (LAZY LOADING)
# 'extract_flat': 'in_playlist' es la magia que hace que las playlists carguen en 1 segundo.
YDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'playlistmaxentries': 150,  # Puedes subirlo sin problema porque ahora es rapidísimo
    'extract_flat': 'in_playlist',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'force_ipv4': True,
}

# 2. OPCIONES PARA EXTRACCIÓN PROFUNDA (Solo 1 canción a la vez)
YDL_EXTRACT_OPTIONS = {
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
    title: str
    webpage_url: str
    thumbnail: str
    stream_url: Optional[str] = None  # Ahora es opcional, lo rellenamos justo antes de sonar
    requester: Optional[discord.Member] = None

    def __str__(self): return self.title


class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.loop_mode = LOOP_OFF
        self.inactivity_task: Optional[asyncio.Task] = None

    def add_song(self, song: Song) -> bool:
        if len(self.queue) >= MAX_QUEUE_SIZE: return False
        self.queue.append(song)
        return True

    def get_next(self) -> Optional[Song]:
        last_song = self.current

        if self.loop_mode == LOOP_CURRENT and last_song:
            return last_song

        if self.loop_mode == LOOP_QUEUE and last_song:
            self.queue.append(last_song)

        if self.queue:
            return self.queue.popleft()

        return None

    def shuffle_queue(self):
        if len(self.queue) > 0:
            temp_list = list(self.queue)
            random.shuffle(temp_list)
            self.queue = deque(temp_list)

    def clear_queue(self):
        self.queue.clear()


class MusicManager:
    def __init__(self):
        self.players: Dict[int, MusicPlayer] = {}

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        if guild.id not in self.players: self.players[guild.id] = MusicPlayer(guild)
        return self.players[guild.id]

    def remove_player(self, guild_id: int):
        if guild_id in self.players: del self.players[guild_id]


music_manager = MusicManager()


async def search_youtube(query: str) -> List[Song]:
    """Carga rápida de canciones (Solo obtiene títulos y links)"""
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

            # Obtener el link del video real
            webpage_url = entry.get('webpage_url')
            if not webpage_url:
                url_field = entry.get('url', '')
                if 'youtube.com' in url_field or 'youtu.be' in url_field:
                    webpage_url = url_field
                else:
                    webpage_url = f"https://www.youtube.com/watch?v={entry.get('id')}"

            # Obtener miniatura (en carga plana viene como lista a veces)
            thumbnail = ''
            if entry.get('thumbnails'):
                thumbnail = entry['thumbnails'][0]['url']
            elif entry.get('thumbnail'):
                thumbnail = entry.get('thumbnail')

            # Si por casualidad se cargó entera (ej. video individual), guardamos el audio ya
            stream_url = None
            if 'formats' in entry:
                f_audio = [f for f in entry['formats'] if f.get('vcodec') == 'none' and f.get('url')]
                if f_audio: stream_url = f_audio[0]['url']

            if not webpage_url: continue

            songs.append(Song(
                title=entry.get('title', 'Desconocido'),
                webpage_url=webpage_url,
                thumbnail=thumbnail,
                stream_url=stream_url
            ))

        return songs
    except Exception as e:
        logger.error(f"Error en búsqueda: {e}")
        return []


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Reproduce y extrae el audio pesado Justo A Tiempo"""
    if not voice_client or not voice_client.is_connected(): return
    if player.inactivity_task:
        player.inactivity_task.cancel()
        player.inactivity_task = None

    song = player.get_next()
    if song is None:
        player.current = None
        player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
        return

    player.current = song

    # --- EXTRACCIÓN JUST IN TIME (LA MAGIA DE CARGAR RÁPIDO) ---
    if not song.stream_url:
        try:
            logger.info(f"Extrayendo audio en tiempo real para: {song.title}")
            loop = asyncio.get_event_loop()

            def extract_single():
                with yt_dlp.YoutubeDL(YDL_EXTRACT_OPTIONS) as ydl:
                    return ydl.extract_info(song.webpage_url, download=False)

            info = await loop.run_in_executor(None, extract_single)
            if info and 'formats' in info:
                f_audio = [f for f in info['formats'] if f.get('vcodec') == 'none' and f.get('url')]
                if f_audio: song.stream_url = f_audio[0]['url']
        except Exception as e:
            logger.error(f"Fallo al cargar la canción {song.title}: {e}")
            await play_next(voice_client, player)  # Saltamos a la siguiente si esta falla
            return

    if not song.stream_url:
        logger.warning(f"No se encontró URL de audio para {song.title}")
        await play_next(voice_client, player)
        return
    # -------------------------------------------------------------

    try:
        source = discord.FFmpegPCMAudio(song.stream_url, **FFMPEG_OPTIONS)
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(voice_client, player),
                                                                                   voice_client.client.loop))
        logger.info(f"▶️ Sonando: {song.title}")
    except Exception as e:
        logger.error(f"Error audio: {e}")
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    await asyncio.sleep(INACTIVITY_TIMEOUT)
    if voice_client.is_connected() and not voice_client.is_playing():
        await voice_client.disconnect()
        music_manager.remove_player(voice_client.guild.id)