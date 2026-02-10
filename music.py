import discord
import asyncio
import yt_dlp
import logging
from typing import Optional, Dict, List
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, INACTIVITY_TIMEOUT

logger = logging.getLogger(__name__)

# Configuración optimizada para RPi y Playlists
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,  # Permitimos playlists
    'playlistmaxentries': 100,  # Límite para no colgar la RPi
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'force_ipv4': True,
    'extract_flat': False,  # Necesitamos la info completa para el flujo de audio
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}


@dataclass
class Song:
    url: str  # URL del flujo (audio real)
    title: str  # Título
    webpage_url: str  # Link de YouTube
    thumbnail: str  # Miniatura
    requester: Optional[discord.Member] = None

    def __str__(self): return self.title


class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.inactivity_task: Optional[asyncio.Task] = None

    def add_song(self, song: Song) -> bool:
        if len(self.queue) >= MAX_QUEUE_SIZE: return False
        self.queue.append(song)
        return True

    def get_next(self) -> Optional[Song]:
        if self.queue: return self.queue.popleft()
        return None

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
    """Busca y devuelve una lista de objetos Song."""
    try:
        loop = asyncio.get_event_loop()

        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                return ydl.extract_info(query, download=False)

        info = await loop.run_in_executor(None, extract)
        if not info: return []

        songs = []
        # Si es una playlist o resultado de búsqueda múltiple
        if 'entries' in info:
            entries = info['entries']
        else:
            entries = [info]

        for entry in entries:
            if not entry: continue

            # Intentar obtener la URL de audio (stream)
            # A veces está en 'url', a veces hay que buscar en 'formats'
            stream_url = entry.get('url')
            if not stream_url and 'formats' in entry:
                # Filtrar formatos que solo son audio y tienen URL
                f_audio = [f for f in entry['formats'] if f.get('vcodec') == 'none' and f.get('url')]
                if f_audio: stream_url = f_audio[0]['url']

            if not stream_url: continue  # Si no hay audio, saltamos

            songs.append(Song(
                url=stream_url,
                title=entry.get('title', 'Desconocido'),
                webpage_url=entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                thumbnail=entry.get('thumbnail', '')
            ))

        return songs

    except Exception as e:
        logger.error(f"Error en búsqueda: {e}")
        return []


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
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
    try:
        source = discord.FFmpegPCMAudio(song.url, **FFMPEG_OPTIONS)
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(voice_client, player),
                                                                                   voice_client.client.loop))
    except Exception as e:
        logger.error(f"Error audio: {e}")
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    await asyncio.sleep(INACTIVITY_TIMEOUT)
    if voice_client.is_connected() and not voice_client.is_playing():
        await voice_client.disconnect()
        music_manager.remove_player(voice_client.guild.id)