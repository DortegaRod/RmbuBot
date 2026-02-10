import discord
import asyncio
import yt_dlp
import shutil
from typing import Optional, Dict
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, DEFAULT_VOLUME, INACTIVITY_TIMEOUT
import logging

logger = logging.getLogger(__name__)

# Configuración de yt-dlp
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'force_ipv4': True,
    'socket_timeout': 10,
    'retries': 3,
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web']
        }
    }
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1',
    'options': '-vn'
}


@dataclass
class Song:
    url: str
    title: str
    duration: Optional[int] = None
    requester: Optional[discord.Member] = None

    def __str__(self): return self.title


class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.volume = DEFAULT_VOLUME
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

    def toggle_loop(self):
        self.loop = not self.loop; return self.loop


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
        opts = YDL_OPTIONS.copy()
        with yt_dlp.YoutubeDL(opts) as ydl:
            if not query.startswith(('http://', 'https://')): query = f"ytsearch:{query}"
            logger.info(f"Buscando: {query}")
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)

            if not info: return None
            if 'entries' in info:
                entries = list(info['entries'])
                if not entries: return None
                info = entries[0]

            url = info.get('url') or (info['formats'][0]['url'] if 'formats' in info else None)
            title = info.get('title', 'Desconocido')

            if not url: return None
            return Song(url=url, title=title)
    except Exception as e:
        logger.error(f"Error búsqueda: {e}")
        return None


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    if not voice_client or not voice_client.is_connected(): return

    if player.inactivity_task:
        player.inactivity_task.cancel()
        player.inactivity_task = None

    # Verificación simple para RPi (ya instalamos ffmpeg con apt)
    if not shutil.which("ffmpeg"):
        logger.critical("❌ FFMPEG NO ENCONTRADO. Ejecuta: sudo apt install ffmpeg")
        return

    song = player.get_next()
    if song is None:
        player.current = None
        player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
        return

    player.current = song
    logger.info(f"Reproduciendo: {song.title}")

    try:
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(song.url, **FFMPEG_OPTIONS),
            volume=player.volume
        )

        def after_playing(error):
            if error: logger.error(f"Error en playback: {error}")
            if voice_client.is_connected():
                fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, player), voice_client.client.loop)
                try:
                    fut.result()
                except:
                    pass

        voice_client.play(source, after=after_playing)

    except Exception as e:
        logger.error(f"❌ Error al iniciar FFmpeg: {e}")
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    await asyncio.sleep(INACTIVITY_TIMEOUT)
    if voice_client.is_connected() and not voice_client.is_playing():
        await voice_client.disconnect()
        music_manager.remove_player(voice_client.guild.id)