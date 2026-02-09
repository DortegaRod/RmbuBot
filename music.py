import discord
import asyncio
import yt_dlp
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
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'socket_timeout': 30,
    'retries': 3,
    # Opciones adicionales para mayor estabilidad
    'age_limit': None,
    'geo_bypass': True,
    'prefer_insecure': False,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -loglevel panic'
}


@dataclass
class Song:
    """Representa una canción en la cola."""
    url: str
    title: str
    duration: Optional[int] = None
    requester: Optional[discord.Member] = None

    def __str__(self):
        return self.title


class MusicPlayer:
    """Gestor de música para un servidor."""

    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.volume = DEFAULT_VOLUME
        self.loop = False
        self.inactivity_task: Optional[asyncio.Task] = None

    def add_song(self, song: Song) -> bool:
        """Añade una canción a la cola."""
        if len(self.queue) >= MAX_QUEUE_SIZE:
            logger.warning(f"Cola llena en {self.guild.name}")
            return False
        self.queue.append(song)
        logger.info(f"Canción añadida a la cola: {song.title}")
        return True

    def skip(self) -> Optional[Song]:
        """Salta a la siguiente canción."""
        skipped = self.current
        self.current = None
        return skipped

    def clear_queue(self) -> int:
        """Limpia toda la cola."""
        count = len(self.queue)
        self.queue.clear()
        logger.info(f"Cola limpiada: {count} canciones eliminadas")
        return count

    def get_next(self) -> Optional[Song]:
        """Obtiene la siguiente canción de la cola."""
        if self.loop and self.current:
            return self.current
        if self.queue:
            return self.queue.popleft()
        return None

    def toggle_loop(self) -> bool:
        """Activa/desactiva el modo loop."""
        self.loop = not self.loop
        return self.loop


class MusicManager:
    """Gestor global de reproductores de música."""

    def __init__(self):
        self.players: Dict[int, MusicPlayer] = {}

    def get_player(self, guild: discord.Guild) -> MusicPlayer:
        """Obtiene o crea un reproductor para un servidor."""
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(guild)
        return self.players[guild.id]

    def remove_player(self, guild_id: int):
        """Elimina un reproductor."""
        if guild_id in self.players:
            del self.players[guild_id]
            logger.info(f"Reproductor eliminado para guild {guild_id}")


# Instancia global del gestor de música
music_manager = MusicManager()


async def search_youtube(query: str) -> Optional[Song]:
    """
    Busca una canción en YouTube.

    Args:
        query: Búsqueda o URL de YouTube

    Returns:
        Song si se encuentra, None en caso contrario
    """
    logger.info(f"Buscando: {query}")

    try:
        # Configuración específica para esta búsqueda
        ydl_opts = YDL_OPTIONS.copy()

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Si no es una URL, buscar en YouTube
            search_query = query if query.startswith('http') else f"ytsearch1:{query}"

            logger.info(f"Ejecutando búsqueda: {search_query}")

            # Extraer info
            info = await asyncio.to_thread(ydl.extract_info, search_query, download=False)

            # Debug: ver qué devolvió yt-dlp
            if info is None:
                logger.error("yt-dlp devolvió None")
                return None

            logger.debug(f"Claves en info: {info.keys() if isinstance(info, dict) else 'No es dict'}")

            # Manejar búsquedas (entries)
            if 'entries' in info:
                logger.debug(f"Encontradas {len(info.get('entries', []))} entradas")

                entries = info['entries']
                if not entries or len(entries) == 0:
                    logger.warning(f"No se encontraron resultados para: {query}")
                    return None

                # Tomar el primer resultado válido
                video_info = None
                for entry in entries:
                    if entry is not None:
                        video_info = entry
                        break

                if video_info is None:
                    logger.error("Todas las entradas son None")
                    return None

                info = video_info

            # En este punto, info debería ser la información del video
            if not isinstance(info, dict):
                logger.error(f"info no es un diccionario: {type(info)}")
                return None

            # Obtener título
            title = info.get('title', 'Desconocido')
            duration = info.get('duration')

            logger.info(f"Video encontrado: {title}")

            # Obtener la URL del stream - probar varios métodos
            url = None

            # Método 1: URL directa
            if 'url' in info:
                url = info['url']
                logger.debug("URL obtenida directamente")

            # Método 2: Buscar en formats
            elif 'formats' in info and info['formats']:
                # Buscar el mejor formato de audio
                formats = info['formats']

                # Preferir formatos de audio solamente
                audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('url')]

                if audio_formats:
                    # Ordenar por calidad de audio (abr = audio bitrate)
                    audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True)
                    url = audio_formats[0]['url']
                    logger.debug(f"URL obtenida de audio formats (abr: {audio_formats[0].get('abr')})")
                else:
                    # Si no hay formatos de solo audio, tomar cualquier formato con URL
                    for fmt in formats:
                        if fmt.get('url'):
                            url = fmt['url']
                            logger.debug("URL obtenida de formats genéricos")
                            break

            # Método 3: Requested formats
            elif 'requested_formats' in info and info['requested_formats']:
                for fmt in info['requested_formats']:
                    if fmt.get('url'):
                        url = fmt['url']
                        logger.debug("URL obtenida de requested_formats")
                        break

            if not url:
                logger.error(f"No se pudo obtener URL. Claves disponibles: {list(info.keys())}")
                return None

            logger.info(f"URL obtenida exitosamente para: {title}")
            return Song(url=url, title=title, duration=duration)

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Error de descarga de yt-dlp: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado al buscar en YouTube: {e}", exc_info=True)
        return None


async def play_next(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Reproduce la siguiente canción en la cola."""
    if voice_client is None or not voice_client.is_connected():
        return

    song = player.get_next()
    if song is None:
        player.current = None
        # Iniciar temporizador de inactividad
        if player.inactivity_task:
            player.inactivity_task.cancel()
        player.inactivity_task = asyncio.create_task(
            inactivity_disconnect(voice_client, player)
        )
        return

    player.current = song

    try:
        # Cancelar temporizador de inactividad si existe
        if player.inactivity_task:
            player.inactivity_task.cancel()
            player.inactivity_task = None

        # Crear source de audio
        source = discord.FFmpegPCMAudio(song.url, **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=player.volume)

        # Reproducir
        voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                play_next(voice_client, player),
                voice_client.guild._state.loop
            )
        )

        logger.info(f"Reproduciendo: {song.title}")

    except Exception as e:
        logger.error(f"Error al reproducir canción: {e}")
        # Intentar con la siguiente canción
        await play_next(voice_client, player)


async def inactivity_disconnect(voice_client: discord.VoiceClient, player: MusicPlayer):
    """Desconecta el bot después de un período de inactividad."""
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect()
            logger.info(f"Desconectado por inactividad en {voice_client.guild.name}")
            music_manager.remove_player(voice_client.guild.id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Error en desconexión por inactividad: {e}")