import discord
import asyncio
import yt_dlp
import logging
from typing import Optional, Dict
from dataclasses import dataclass
from collections import deque
from config import MAX_QUEUE_SIZE, INACTIVITY_TIMEOUT

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN IDÉNTICA A TU VERSIÓN ANTIGUA (MEJORADA) ---

# Opciones de yt-dlp equilibradas:
# - 'format': 'bestaudio' (Igual que tu versión antigua)
# - 'noplaylist': True (Para evitar descargar listas enteras)
# - 'default_search': 'ytsearch' (Ayuda a encontrar mejor la canción)
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',  # Esto mejora la precisión de la búsqueda
    'source_address': '0.0.0.0',  # Único ajuste de red necesario para RPi
}

# Opciones de FFmpeg EXACTAS a tu versión antigua + reconexión
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}


@dataclass
class Song:
    url: str
    title: str
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
    """
    Busca la canción y extrae la URL directa.
    Usa la lógica de tu bot antiguo pero gestionando mejor los errores.
    """
    try:
        # Usamos run_in_executor para no bloquear el bot mientras busca
        loop = asyncio.get_event_loop()

        # Función auxiliar para ejecutar yt-dlp
        def extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                # yt-dlp maneja automáticamente si es URL o búsqueda gracias a las opciones
                return ydl.extract_info(query, download=False)

        info = await loop.run_in_executor(None, extract)

        if not info: return None

        # Si devuelve una lista de resultados, cogemos el primero (el más relevante)
        if 'entries' in info:
            entries = list(info['entries'])
            if entries:
                info = entries[0]
            else:
                return None

        # Extraemos la URL y el título
        url = info.get('url')
        title = info.get('title', 'Canción desconocida')

        if not url: return None

        return Song(url=url, title=title)

    except Exception as e:
        logger.error(f"Error al buscar en YouTube: {e}")
        return None


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """
    Sistema de reproducción recursivo.
    Usa la simplicidad de tu bot antiguo para el audio.
    """
    if not voice_client or not voice_client.is_connected():
        return

    # Cancelar desconexión si hay música
    if player.inactivity_task:
        player.inactivity_task.cancel()
        player.inactivity_task = None

    song = player.get_next()

    # Si no hay canciones, esperar y desconectar
    if song is None:
        player.current = None
        player.inactivity_task = asyncio.create_task(inactivity_disconnect(voice_client, player))
        return

    player.current = song
    logger.info(f"Reproduciendo: {song.title}")

    try:
        # --- AQUÍ ESTÁ LA CLAVE DEL ARREGLO ---
        # Usamos FFmpegPCMAudio DIRECTAMENTE, sin VolumeTransformer.
        # Esto elimina una capa de complejidad que suele fallar en Raspberry.
        # Es exactamente lo que hacía tu bot antiguo: source = discord.FFmpegPCMAudio(url2, **FFMPEG_OPTIONS)

        source = discord.FFmpegPCMAudio(song.url, **FFMPEG_OPTIONS)

        def after_playing(error):
            if error: logger.error(f"Error de reproducción: {error}")
            # Llamada recursiva segura
            if voice_client.is_connected():
                fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, player), voice_client.client.loop)
                try:
                    fut.result()
                except:
                    pass

        voice_client.play(source, after=after_playing)

    except Exception as e:
        logger.error(f"Error crítico al iniciar audio: {e}")
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Espera un tiempo y desconecta si no hay música."""
    await asyncio.sleep(INACTIVITY_TIMEOUT)
    if voice_client.is_connected() and not voice_client.is_playing():
        await voice_client.disconnect()
        music_manager.remove_player(voice_client.guild.id)
        logger.info("Desconectado por inactividad.")