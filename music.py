import discord
import asyncio
import yt_dlp
from typing import Optional, Dict
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, DEFAULT_VOLUME, INACTIVITY_TIMEOUT
import logging

logger = logging.getLogger(__name__)

# Configuración de yt-dlp OPTIMIZADA
# Forzamos IPv4 y clientes móviles para evitar throttles/errores 403 y 4006
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # Forzar IPv4
    'force_ipv4': True,  # Forzar IPv4 explícitamente
    'socket_timeout': 10,
    'retries': 3,
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web']
        }
    }
}

# Opciones FFMPEG robustas para evitar cortes
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
        """Activa o desactiva el bucle."""
        self.loop = not self.loop
        return self.loop


class MusicManager:
    def __init__(self):
        self.players: Dict[int, MusicPlayer] = {}

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(guild)
        return self.players[guild.id]

    def remove_player(self, guild_id: int):
        if guild_id in self.players: del self.players[guild_id]


music_manager = MusicManager()


async def search_youtube(query: str) -> Optional[Song]:
    """Busca en YouTube usando yt-dlp."""
    try:
        # Copia de opciones para no modificar la global
        opts = YDL_OPTIONS.copy()

        with yt_dlp.YoutubeDL(opts) as ydl:
            # Detectar si es URL o búsqueda
            if not query.startswith(('http://', 'https://')):
                query = f"ytsearch:{query}"

            logger.info(f"Buscando: {query}")
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)

            if not info: return None

            # Si es una lista de resultados (búsqueda), tomamos el primero
            if 'entries' in info:
                entries = list(info['entries'])
                if not entries: return None
                info = entries[0]

            url = info.get('url')
            title = info.get('title', 'Canción desconocida')

            # Fallback: Si no hay URL directa, buscar en formatos
            if not url and 'formats' in info:
                # Intentar coger el mejor audio
                formats = info['formats']
                # Filtrar solo los que tienen URL
                valid_formats = [f for f in formats if f.get('url')]
                if valid_formats:
                    url = valid_formats[-1]['url']  # El último suele ser mejor calidad en yt-dlp sorted formats

            if not url:
                logger.error("No se encontró URL válida en la info extraída")
                return None

            return Song(url=url, title=title)

    except Exception as e:
        logger.error(f"Error en búsqueda: {e}")
        return None


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Lógica recursiva para reproducir la cola."""
    if not voice_client or not voice_client.is_connected():
        return

    # Cancelar desconexión por inactividad si vamos a reproducir
    if player.inactivity_task:
        player.inactivity_task.cancel()
        player.inactivity_task = None

    song = player.get_next()

    # Si no hay canción, iniciamos temporizador de desconexión
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
            if error:
                logger.error(f"Error en reproducción: {error}")

            # Programar la siguiente canción
            # Usamos el loop del cliente asociado al voice_client
            if voice_client.is_connected():
                coro = play_next(voice_client, player)
                future = asyncio.run_coroutine_threadsafe(coro, voice_client.client.loop)
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Error en callback after_playing: {e}")

        voice_client.play(source, after=after_playing)

    except Exception as e:
        logger.error(f"Error al iniciar audio: {e}")
        # Si falla, intentamos la siguiente
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        if voice_client.is_connected() and not voice_client.is_playing():
            await voice_client.disconnect()
            music_manager.remove_player(voice_client.guild.id)
            logger.info(f"Desconectado por inactividad en {voice_client.guild.name}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Error en desconexión automática: {e}")