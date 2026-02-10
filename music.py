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
    'playlistmaxentries': 50,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
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

    def __str__(self): return self.title


class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        # Ahora loop_mode es un número (0, 1, 2) en vez de True/False
        self.loop_mode = LOOP_OFF
        self.inactivity_task: Optional[asyncio.Task] = None

    def add_song(self, song: Song) -> bool:
        if len(self.queue) >= MAX_QUEUE_SIZE: return False
        self.queue.append(song)
        return True

    def get_next(self) -> Optional[Song]:
        # Lógica de bucle
        last_song = self.current

        # 1. Loop Canción Actual: Devolvemos la misma
        if self.loop_mode == LOOP_CURRENT and last_song:
            return last_song

        # 2. Loop Cola: La que acaba de terminar se va al final de la fila
        if self.loop_mode == LOOP_QUEUE and last_song:
            self.queue.append(last_song)

        # Sacamos la siguiente
        if self.queue:
            return self.queue.popleft()

        return None

    def shuffle_queue(self):
        """Mezcla aleatoriamente las canciones en espera."""
        if len(self.queue) > 0:
            # Convertir a lista, mezclar y volver a deque
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
    try:
        loop = asyncio.get_event_loop()

        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                return ydl.extract_info(query, download=False)

        info = await loop.run_in_executor(None, extract)
        if not info: return []

        songs = []
        entries = info.get('entries', [info])

        for entry in entries:
            if not entry: continue
            stream_url = entry.get('url')
            if not stream_url and 'formats' in entry:
                f_audio = [f for f in entry['formats'] if f.get('vcodec') == 'none' and f.get('url')]
                if f_audio: stream_url = f_audio[0]['url']

            if not stream_url: continue

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