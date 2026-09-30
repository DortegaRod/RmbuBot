"""
Palabra del día: el Wordle en español del server.

- Una palabra de 5 letras al día, la misma en todos los servidores; cambia a medianoche (hora de España).
- Cada persona juega en privado con /palabra: 6 intentos, las tildes no cuentan (la ñ sí).
- Al terminar, el bot publica sus cuadritos (sin letras) en el canal del juego.
- A la hora configurada con /setup-palabra, el bot anuncia la nueva palabra y revela la de ayer.

Diccionario: lista de palabras de Letterpress (github.com/lorenbrichter/Words, dominio público CC0).
"""

import asyncio
import logging
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import escape_markdown

import db

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    SPAIN_TZ = ZoneInfo("Europe/Madrid")
except Exception:  # Sistema sin zonas horarias y sin el paquete tzdata
    SPAIN_TZ = timezone.utc

WORD_LENGTH = 5
MAX_ATTEMPTS = 6
DEFAULT_HOUR = 10
DATA_DIR = Path(__file__).parent / "datos"

GREEN, YELLOW, GRAY, EMPTY = "🟩", "🟨", "⬛", "⬜"
NO_MENTIONS = discord.AllowedMentions.none()

VALID_WORDS: set = set()  # Palabras aceptadas como intento (normalizadas: sin tildes)
ANSWERS: List[str] = []   # Posibles palabras del día, escritas con sus tildes

_ACCENTS = str.maketrans("áéíóúüàèìòùâêîôû", "aeiouuaeiouaeiou")


# ==========================================
# PALABRAS
# ==========================================

def normalize(word: str) -> str:
    """Minúsculas y sin tildes (la ñ se mantiene). Devuelve '' si no son 5 letras."""
    word = word.strip().lower().translate(_ACCENTS)
    return word if re.fullmatch(r"[a-zñ]{5}", word) else ""


def _read_word_file(name: str) -> List[str]:
    lines = (DATA_DIR / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def load_words() -> bool:
    """Carga el diccionario y las respuestas. Devuelve False si faltan los archivos."""
    try:
        valid = _read_word_file("palabras_validas.txt")
        answers = [word for word in _read_word_file("palabras_respuestas.txt") if normalize(word)]
    except OSError as e:
        logger.error(f"❌ Palabra del día desactivada: faltan las listas de palabras en {DATA_DIR} ({e})")
        return False

    VALID_WORDS.clear()
    VALID_WORDS.update(filter(None, map(normalize, valid)))
    VALID_WORDS.update(map(normalize, answers))  # Las respuestas siempre se pueden escribir
    ANSWERS[:] = answers
    if SPAIN_TZ is timezone.utc:
        logger.warning("⚠️ Sin zona horaria de España: la palabra cambiará a medianoche UTC (pip install tzdata)")
    logger.info(f"🟩 Palabra del día: {len(ANSWERS)} respuestas posibles y {len(VALID_WORDS)} palabras válidas")
    return bool(ANSWERS)


def score(guess: str, answer: str) -> List[str]:
    """Colores de un intento, como en Wordle (resolviendo bien las letras repetidas)."""
    result = [GRAY] * WORD_LENGTH
    pending = Counter()
    for i, (g, a) in enumerate(zip(guess, answer)):
        if g == a:
            result[i] = GREEN
        else:
            pending[a] += 1
    for i, g in enumerate(guess):
        if result[i] != GREEN and pending[g] > 0:
            result[i] = YELLOW
            pending[g] -= 1
    return result


def today() -> date:
    """Fecha actual en España (el día de la palabra)."""
    return datetime.now(SPAIN_TZ).date()


# ==========================================
# BASE DE DATOS
# ==========================================

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS palabra_dia (
        fecha TEXT PRIMARY KEY,
        numero INTEGER NOT NULL,
        palabra TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS palabra_partidas (
        fecha TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        guild_id INTEGER,
        intentos TEXT NOT NULL DEFAULT '',
        terminada INTEGER NOT NULL DEFAULT 0,
        acertada INTEGER NOT NULL DEFAULT 0,
        actualizada TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (fecha, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS palabra_config (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        hora INTEGER NOT NULL DEFAULT 10,
        ultimo_anuncio TEXT
    )""",
)


def init_db():
    with db.get_db_connection() as conn:
        for sql in _SCHEMA:
            conn.execute(sql)


@dataclass
class Game:
    """Partida de una persona en un día."""
    fecha: str
    user_id: int
    guild_id: Optional[int] = None
    guesses: List[str] = field(default_factory=list)
    finished: bool = False
    won: bool = False
    updated_at: str = ""


@dataclass
class GameConfig:
    guild_id: int
    channel_id: int
    hora: int
    ultimo_anuncio: Optional[str]


def find_word(day: date) -> Optional[Tuple[int, str]]:
    """(número, palabra) de un día, si ya se eligió."""
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT numero, palabra FROM palabra_dia WHERE fecha = ?", (day.isoformat(),)).fetchone()
    return (row["numero"], row["palabra"]) if row else None


def get_word(day: date) -> Tuple[int, str]:
    """(número, palabra) del día. La primera vez se elige al azar, sin repetir las usadas antes."""
    fecha = day.isoformat()
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT numero, palabra FROM palabra_dia WHERE fecha = ?", (fecha,)).fetchone()
        if row:
            return row["numero"], row["palabra"]
        recent = conn.execute("SELECT palabra FROM palabra_dia ORDER BY fecha DESC LIMIT ?", (len(ANSWERS) - 1,))
        used = {normalize(r["palabra"]) for r in recent}
        word = random.choice([w for w in ANSWERS if normalize(w) not in used] or ANSWERS)
        numero = conn.execute("SELECT COALESCE(MAX(numero), 0) + 1 FROM palabra_dia").fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO palabra_dia (fecha, numero, palabra) VALUES (?, ?, ?)",
                     (fecha, numero, word))
        row = conn.execute("SELECT numero, palabra FROM palabra_dia WHERE fecha = ?", (fecha,)).fetchone()
    return row["numero"], row["palabra"]


def _row_to_game(row) -> Game:
    return Game(row["fecha"], row["user_id"], row["guild_id"], [g for g in row["intentos"].split(",") if g],
                bool(row["terminada"]), bool(row["acertada"]), row["actualizada"] or "")


def load_game(day: date, user_id: int) -> Game:
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT * FROM palabra_partidas WHERE fecha = ? AND user_id = ?",
                           (day.isoformat(), user_id)).fetchone()
    return _row_to_game(row) if row else Game(day.isoformat(), user_id)


def save_game(game: Game):
    with db.get_db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO palabra_partidas "
            "(fecha, user_id, guild_id, intentos, terminada, acertada, actualizada) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (game.fecha, game.user_id, game.guild_id, ",".join(game.guesses), int(game.finished), int(game.won))
        )


def finished_games_on(day: date, guild_id: int) -> List[Game]:
    """Partidas terminadas un día en un servidor: primero los aciertos con menos intentos."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM palabra_partidas WHERE fecha = ? AND guild_id = ? AND terminada = 1",
                            (day.isoformat(), guild_id)).fetchall()
    return sorted(map(_row_to_game, rows), key=lambda g: (not g.won, len(g.guesses), g.updated_at))


def user_results(user_id: int) -> Dict[date, Tuple[bool, int]]:
    """fecha -> (acertada, nº de intentos) de todas las partidas terminadas de alguien."""
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT fecha, acertada, intentos FROM palabra_partidas "
                            "WHERE user_id = ? AND terminada = 1", (user_id,)).fetchall()
    return {date.fromisoformat(r["fecha"]): (bool(r["acertada"]), len([g for g in r["intentos"].split(",") if g]))
            for r in rows}


def get_config(guild_id: int) -> Optional[GameConfig]:
    with db.get_db_connection() as conn:
        row = conn.execute("SELECT * FROM palabra_config WHERE guild_id = ?", (guild_id,)).fetchone()
    return GameConfig(row["guild_id"], row["channel_id"], row["hora"], row["ultimo_anuncio"]) if row else None


def all_configs() -> List[GameConfig]:
    with db.get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM palabra_config").fetchall()
    return [GameConfig(r["guild_id"], r["channel_id"], r["hora"], r["ultimo_anuncio"]) for r in rows]


def set_config(guild_id: int, channel_id: int, hora: int):
    with db.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO palabra_config (guild_id, channel_id, hora) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, hora = excluded.hora",
            (guild_id, channel_id, hora)
        )


def clear_config(guild_id: int):
    with db.get_db_connection() as conn:
        conn.execute("DELETE FROM palabra_config WHERE guild_id = ?", (guild_id,))


def mark_announced(guild_id: int, fecha: str):
    with db.get_db_connection() as conn:
        conn.execute("UPDATE palabra_config SET ultimo_anuncio = ? WHERE guild_id = ?", (fecha, guild_id))


# ==========================================
# ESTADÍSTICAS
# ==========================================

@dataclass
class Stats:
    played: int
    wins: int
    current_streak: int
    best_streak: int
    distribution: Counter


def compute_stats(results: Dict[date, Tuple[bool, int]], day: date) -> Stats:
    """Estadísticas y rachas (días seguidos acertando). Una racha sigue viva hasta que acaba el día."""
    day_to_check = day if day in results else day - timedelta(days=1)
    current = 0
    while results.get(day_to_check, (False, 0))[0]:
        current += 1
        day_to_check -= timedelta(days=1)

    best = run = 0
    previous_win: Optional[date] = None
    for d in sorted(results):
        if results[d][0]:
            run = run + 1 if previous_win == d - timedelta(days=1) else 1
            previous_win = d
            best = max(best, run)
        else:
            run, previous_win = 0, None

    wins = [attempts for won, attempts in results.values() if won]
    return Stats(len(results), len(wins), current, best, Counter(wins))


# ==========================================
# MENSAJES
# ==========================================

def _grid(game: Game, answer: str) -> str:
    return "\n".join("".join(score(guess, answer)) for guess in game.guesses)


def _time_to_next_word() -> str:
    now = datetime.now(SPAIN_TZ)
    midnight = datetime.combine(now.date() + timedelta(days=1), time(0), tzinfo=SPAIN_TZ)
    minutes = int((midnight - now).total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min"


def _letters_summary(guesses: List[str], answer: str) -> str:
    """Qué letras están y cuáles están descartadas (lo que en Wordle es el teclado de colores)."""
    rank = {GRAY: 0, YELLOW: 1, GREEN: 2}
    best: Dict[str, str] = {}
    for guess in guesses:
        for letter, color in zip(guess, score(guess, answer)):
            if rank[color] > rank.get(best.get(letter), -1):
                best[letter] = color
    present = " ".join(sorted(letter.upper() for letter, color in best.items() if color != GRAY))
    absent = " ".join(sorted(letter.upper() for letter, color in best.items() if color == GRAY))
    lines = []
    if present:
        lines.append(f"✅ Están: **{present}**")
    if absent:
        lines.append(f"❌ No están: {absent}")
    return "\n".join(lines)


def board_embed(numero: int, answer_display: str, game: Game, notice: Optional[str] = None,
                published_in: Optional[str] = None) -> discord.Embed:
    """Tablero privado de quien juega."""
    answer = normalize(answer_display)
    rows = [f"{''.join(score(guess, answer))}  `{' '.join(guess.upper())}`" for guess in game.guesses]
    if not game.finished:
        rows += [EMPTY * WORD_LENGTH] * (MAX_ATTEMPTS - len(game.guesses))

    parts = []
    if notice:
        parts.append(f"⚠️ {notice}")
    if not game.guesses:
        parts.append(f"Adivina la palabra de **{WORD_LENGTH} letras** en **{MAX_ATTEMPTS} intentos**.\n"
                     f"{GREEN} en su sitio · {YELLOW} está en otro sitio · {GRAY} no está\n"
                     f"Las tildes no cuentan (la Ñ sí).")
    parts.append("\n".join(rows))

    if game.won:
        status = f"🎉 **¡Acertaste en {len(game.guesses)}/{MAX_ATTEMPTS}!**"
    elif game.finished:
        status = f"💀 Se acabaron los intentos. La palabra era **{answer_display.upper()}**."
    else:
        left = MAX_ATTEMPTS - len(game.guesses)
        status = f"Te queda **1** intento." if left == 1 else f"Te quedan **{left}** intentos."
    if game.finished:
        if published_in:
            status += f"\nTu resultado se ha publicado en {published_in}."
        status += f"\n⏳ Nueva palabra en {_time_to_next_word()}."
    parts.append(status)

    color = discord.Color.green() if game.won else discord.Color.red() if game.finished else discord.Color.blurple()
    embed = discord.Embed(title=f"🟩 Palabra del día #{numero}", description="\n\n".join(parts), color=color)
    letters = _letters_summary(game.guesses, answer)
    if letters and not game.finished:
        embed.add_field(name="Letras", value=letters, inline=False)
    return embed


def stats_embed(member: discord.abc.User, stats: Stats) -> discord.Embed:
    embed = discord.Embed(title=f"📊 Palabra del día · {member.display_name}", color=discord.Color.green())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Jugadas", value=str(stats.played))
    embed.add_field(name="Aciertos", value=f"{round(100 * stats.wins / stats.played)} %" if stats.played else "—")
    embed.add_field(name="Racha", value=f"🔥 {stats.current_streak} · mejor: {stats.best_streak}")
    peak = max(stats.distribution.values(), default=0)
    bars = []
    for attempts in range(1, MAX_ATTEMPTS + 1):
        count = stats.distribution.get(attempts, 0)
        bar = "█" * max(1 if count else 0, round(12 * count / peak) if peak else 0)
        bars.append(f"`{attempts}` {bar} {count}")
    embed.add_field(name="Aciertos por número de intentos", value="\n".join(bars), inline=False)
    return embed


def announcement_embed(guild: discord.Guild, day: date) -> discord.Embed:
    """Anuncio diario: la palabra nueva y los resultados de la de ayer en este servidor."""
    numero, _ = get_word(day)
    embed = discord.Embed(
        title=f"🟩 Palabra del día #{numero}",
        description="¡Nueva palabra! Juega con **/palabra** (tus letras solo las ves tú).",
        color=discord.Color.green()
    )
    yesterday = day - timedelta(days=1)
    previous = find_word(yesterday)
    if previous:
        prev_numero, prev_word = previous
        games = finished_games_on(yesterday, guild.id)
        winners = [g for g in games if g.won]
        lines = [f"Era **{prev_word.upper()}**"]
        if games:
            lines.append(f"👥 {len(games)} {'jugó' if len(games) == 1 else 'jugaron'} · "
                         f"✅ {len(winners)} la {'sacó' if len(winners) == 1 else 'sacaron'}")
            podium = [f"{medal} <@{g.user_id}> {len(g.guesses)}/{MAX_ATTEMPTS}" for medal, g in zip("🥇🥈🥉", winners)]
            if podium:
                lines.append(" · ".join(podium))
        else:
            lines.append("Nadie la jugó 😢")
        embed.add_field(name=f"Ayer (#{prev_numero})", value="\n".join(lines), inline=False)
    embed.set_footer(text="Nueva palabra cada día a medianoche (hora de España)")
    return embed


# ==========================================
# INTERACCIÓN: BOTÓN, VENTANA Y PUBLICACIÓN
# ==========================================

_locks: Dict[int, asyncio.Lock] = {}


def _results_channel(interaction: discord.Interaction) -> Optional[discord.abc.Messageable]:
    """Canal del juego; si no hay, el canal de comandos de /setup; si no, donde se jugó."""
    guild = interaction.guild
    candidates = []
    config = get_config(guild.id)
    if config:
        candidates.append(guild.get_channel(config.channel_id))
    command_channel = db.get_music_channel(guild.id)
    if command_channel:
        candidates.append(guild.get_channel(command_channel))
    candidates.append(interaction.channel)
    for channel in candidates:
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for and permissions_for(guild.me).send_messages:
            return channel
    return None


async def _publish_result(interaction: discord.Interaction, channel, numero: int, answer: str, game: Game):
    streak = compute_stats(user_results(game.user_id), today()).current_streak
    if game.won:
        head = f"{interaction.user.mention} ha resuelto la palabra **#{numero}** en **{len(game.guesses)}/{MAX_ATTEMPTS}**"
    else:
        head = f"{interaction.user.mention} no ha sacado la palabra **#{numero}** (X/{MAX_ATTEMPTS}) 💀"
    if streak >= 2:
        head += f"  🔥 racha {streak}"
    try:
        await channel.send(f"{head}\n{_grid(game, answer)}", allowed_mentions=NO_MENTIONS)
    except discord.HTTPException as e:
        logger.warning(f"No pude publicar el resultado de la palabra del día: {e}")


def board_view(fecha: str, finished: bool) -> Optional[discord.ui.View]:
    if finished:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(GuessButton(fecha))
    return view


async def submit_guess(interaction: discord.Interaction, fecha: str, raw: str):
    """Procesa un intento escrito en la ventana y actualiza el tablero privado."""
    day = today()
    if fecha != day.isoformat():
        return await interaction.response.send_message(
            "⏰ Ha cambiado el día mientras jugabas. Usa **/palabra** para la palabra nueva.", ephemeral=True
        )

    async with _locks.setdefault(interaction.user.id, asyncio.Lock()):
        numero, answer_display = get_word(day)
        answer = normalize(answer_display)
        game = load_game(day, interaction.user.id)
        notice = None
        just_finished = False
        if game.finished:
            notice = "Ya has terminado la palabra de hoy."
        else:
            guess = normalize(raw)
            if not guess:
                notice = "Escribe una palabra de 5 letras (sin números ni símbolos)."
            elif guess != answer and guess not in VALID_WORDS:
                notice = f"**{escape_markdown(raw.strip().upper())}** no está en el diccionario."
            elif guess in game.guesses:
                notice = f"Ya has probado **{guess.upper()}**."
            else:
                game.guesses.append(guess)
                game.guild_id = interaction.guild_id
                game.won = guess == answer
                game.finished = game.won or len(game.guesses) >= MAX_ATTEMPTS
                just_finished = game.finished
                save_game(game)

        channel = _results_channel(interaction) if just_finished else None
        await interaction.response.edit_message(
            embed=board_embed(numero, answer_display, game, notice, channel.mention if channel else None),
            view=board_view(fecha, game.finished)
        )
        if channel is not None:
            await _publish_result(interaction, channel, numero, answer, game)


class GuessModal(discord.ui.Modal, title="🟩 Palabra del día"):
    guess = discord.ui.TextInput(label="Tu palabra de 5 letras", min_length=5, max_length=5,
                                 placeholder="Ej.: perro (las tildes no cuentan)")

    def __init__(self, fecha: str):
        super().__init__()
        self.fecha = fecha

    async def on_submit(self, interaction: discord.Interaction):
        await submit_guess(interaction, self.fecha, self.guess.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("Error en la palabra del día", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Algo ha fallado. Prueba otra vez con /palabra.", ephemeral=True)


class GuessButton(discord.ui.DynamicItem[discord.ui.Button], template=r"reimbou:palabra:(?P<fecha>\d{4}-\d{2}-\d{2})"):
    """Botón que abre la ventana para escribir. Funciona aunque el bot se haya reiniciado."""

    def __init__(self, fecha: str):
        super().__init__(discord.ui.Button(label="Escribir palabra", emoji="✏️", style=discord.ButtonStyle.success,
                                           custom_id=f"reimbou:palabra:{fecha}"))
        self.fecha = fecha

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["fecha"])

    async def callback(self, interaction: discord.Interaction):
        day = today()
        if self.fecha != day.isoformat():
            return await interaction.response.send_message(
                "⏰ Esa palabra ya no está en juego. Usa **/palabra** para la de hoy.", ephemeral=True
            )
        game = load_game(day, interaction.user.id)
        if game.finished:
            numero, answer_display = get_word(day)
            return await interaction.response.edit_message(embed=board_embed(numero, answer_display, game), view=None)
        await interaction.response.send_modal(GuessModal(self.fecha))


# ==========================================
# COMANDOS Y ANUNCIO DIARIO
# ==========================================

class PalabraCog(commands.Cog):
    """Comandos /palabra, /palabra-stats y /setup-palabra, y el anuncio diario."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        init_db()
        load_words()
        self.bot.add_dynamic_items(GuessButton)
        self.announcer.start()

    async def cog_unload(self):
        self.announcer.cancel()

    @app_commands.command(name="palabra", description="Juega a la palabra del día (Wordle en español)")
    @app_commands.guild_only()
    async def palabra(self, interaction: discord.Interaction):
        if not ANSWERS:
            return await interaction.response.send_message(
                "⚠️ La palabra del día no está disponible: faltan las listas de palabras (carpeta datos/).",
                ephemeral=True
            )
        day = today()
        numero, answer_display = get_word(day)
        game = load_game(day, interaction.user.id)
        view = board_view(day.isoformat(), game.finished)
        await interaction.response.send_message(embed=board_embed(numero, answer_display, game), ephemeral=True,
                                                **({"view": view} if view else {}))

    @app_commands.command(name="palabra-stats", description="Estadísticas y rachas de la palabra del día")
    @app_commands.guild_only()
    @app_commands.describe(usuario="De quién ver las estadísticas (por defecto, las tuyas)")
    async def palabra_stats(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        member = usuario or interaction.user
        stats = compute_stats(user_results(member.id), today())
        if stats.played == 0:
            who = "Aún no has" if member == interaction.user else f"{member.display_name} aún no ha"
            return await interaction.response.send_message(
                f"📊 {who} terminado ninguna palabra del día. ¡Prueba con **/palabra**!", ephemeral=True
            )
        await interaction.response.send_message(embed=stats_embed(member, stats))

    @app_commands.command(name="setup-palabra", description="Canal y hora del anuncio diario de la palabra (solo admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        canal="Canal del juego: anuncio diario y resultados (vacío = ver la configuración)",
        hora="Hora del anuncio diario, de 0 a 23 (hora de España). Por defecto, las 10",
        quitar="Desactiva el anuncio diario (/palabra sigue funcionando)",
    )
    async def setup_palabra(
            self,
            interaction: discord.Interaction,
            canal: Optional[Union[discord.TextChannel, discord.VoiceChannel]] = None,
            hora: Optional[app_commands.Range[int, 0, 23]] = None,
            quitar: bool = False
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ Necesitas el permiso **Gestionar servidor** para usar `/setup-palabra`.", ephemeral=True
            )
        if quitar:
            clear_config(interaction.guild_id)
            return await interaction.response.send_message(
                "✅ Anuncio diario desactivado. **/palabra** sigue funcionando."
            )

        config = get_config(interaction.guild_id)
        if canal is None and hora is None:
            status = (f"📌 La palabra del día se anuncia a las **{config.hora:02d}:00** en <#{config.channel_id}>."
                      if config else "📌 No hay anuncio diario configurado.")
            return await interaction.response.send_message(
                f"{status}\nUsa `/setup-palabra canal:#canal hora:10` para cambiarlo o `quitar:True` para desactivarlo.",
                ephemeral=True
            )

        channel = canal or (interaction.guild.get_channel(config.channel_id) if config else None)
        if channel is None:
            return await interaction.response.send_message(
                "❌ Indica el canal del juego: `/setup-palabra canal:#canal`.", ephemeral=True
            )
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"⚠️ No puedo escribir bien en {channel.mention}: necesito **Ver canal**, **Enviar mensajes** "
                f"e **Insertar enlaces** ahí. Dame esos permisos y repite `/setup-palabra`.", ephemeral=True
            )

        new_hour = hora if hora is not None else (config.hora if config else DEFAULT_HOUR)
        set_config(interaction.guild_id, channel.id, new_hour)
        await interaction.response.send_message(
            f"✅ La palabra del día se anunciará cada día a las **{new_hour:02d}:00** en {channel.mention}, "
            f"y los resultados de cada uno se publicarán ahí."
        )

    @tasks.loop(minutes=1)
    async def announcer(self):
        """Publica el anuncio diario en cada servidor configurado (una vez al día, a partir de su hora)."""
        if not ANSWERS:
            return
        try:
            now = datetime.now(SPAIN_TZ)
            fecha = now.date().isoformat()
            for config in all_configs():
                if config.ultimo_anuncio == fecha or now.hour < config.hora:
                    continue
                mark_announced(config.guild_id, fecha)  # Antes de enviar: nunca se publica dos veces
                guild = self.bot.get_guild(config.guild_id)
                channel = guild.get_channel(config.channel_id) if guild else None
                if channel is None:
                    logger.warning(f"Palabra del día: no encuentro el canal {config.channel_id} del servidor {config.guild_id}")
                    continue
                try:
                    await channel.send(embed=announcement_embed(guild, now.date()), allowed_mentions=NO_MENTIONS)
                except discord.HTTPException as e:
                    logger.warning(f"No pude publicar el anuncio de la palabra del día en #{channel}: {e}")
        except Exception:
            logger.exception("Error en el anuncio diario de la palabra del día")  # El bucle sigue funcionando

    @announcer.before_loop
    async def before_announcer(self):
        await self.bot.wait_until_ready()
