import discord
import asyncio
import yt_dlp
import logging
from typing import Optional, Dict
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, INACTIVITY_TIMEOUT

logger = logging.getLogger(__name__)

# Configuración crítica para Raspberry Pi
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'playlistmaxentries': 100,
    'extract_flat': 'in_playlist',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'force_ipv4': True,
    'socket_timeout': 10
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}


@dataclass
class Song:
    url: str  # URL del stream de audio (interno)
    title: str  # Título
    webpage_url: str  # URL original de YouTube (para el click)
    thumbnail: str  # URL de la imagen/carátula
    requester: Optional[discord.Member] = None

    def __str__(self): return self.title


class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.loop = False
        self.inactivity_task: Optional[asyncio.Task] = None

    def add_song(self, song: Song) -> bool:
        if len(self.queue) >= MAX_QUEUE_SIZE: return False
        self.queue.append(song)
        return True

    def get_next(self) -> Optional[Song]:
        if self.loop and self.current: return self.current
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


async def search_youtube(query: str) -> Optional[Song]:
    try:
        loop = asyncio.get_event_loop()

        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                return ydl.extract_info(query, download=False)

        logger.info(f"Iniciando búsqueda en YT: {query}")
        info = await loop.run_in_executor(None, extract)

        if not info: return None
        if 'entries' in info: info = info['entries'][0]

        return Song(
            url=info.get('url'),
            title=info.get('title', 'Canción desconocida'),
            webpage_url=info.get('webpage_url', ''),  # Guardamos el link original
            thumbnail=info.get('thumbnail', '')  # Guardamos la foto
        )

    except Exception as e:
        logger.error(f"Error al buscar en YouTube: {e}")
        return None


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
    logger.info(f"Reproduciendo: {song.title}")

    try:
        source = discord.FFmpegPCMAudio(song.url, **FFMPEG_OPTIONS)

        def after_playing(error):
            if error: logger.error(f"Error de reproducción: {error}")
            if voice_client.is_connected():
                fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, player), voice_client.client.loop)
                try:
                    fut.result()
                except:
                    pass

        voice_client.play(source, after=after_playing)

    except Exception as e:
        logger.error(f"Error crítico audio: {e}")
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    await asyncio.sleep(INACTIVITY_TIMEOUT)
    if voice_client.is_connected() and not voice_client.is_playing():
        await voice_client.disconnect()
        music_manager.remove_player(voice_client.guild.id)