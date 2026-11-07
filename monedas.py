"""
Monedas: cobre, plata y oro, al estilo de un RPG.

- Cada moneda se guarda aparte. Lo que se gana es siempre cobre: la plata y el oro se craftean en la
  forja (forja.py), 100 de cobre por 1 de plata y 100 de plata por 1 de oro, y se pueden volver a fundir.
- Se ganan subiendo de nivel: llegar al nivel N da N × 100 de cobre. Se guarda hasta qué nivel se le ha
  pagado a cada persona, así que ningún nivel se paga dos veces (aunque un admin le baje y le vuelva a
  subir el nivel, o se vinculen cuentas). Los niveles que ya tenía cada uno se pagan al arrancar el bot
  (niveles.py).
- Todo va por persona: las cuentas vinculadas (cuentas.py) suman sus monedas.
"""

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import db
import tarjetas

RATE = 100            # Monedas de un tipo que hacen falta para craftear una del siguiente
PLATA = RATE          # Lo que vale una de plata, en cobre
ORO = RATE * PLATA    # Lo que vale una de oro, en cobre
LEVEL_REWARD = PLATA  # Llegar al nivel N da N veces esto, en cobre
COIN_NAMES = ("oro", "plata", "cobre")  # De más a menos valor
LOWER = {"plata": "cobre", "oro": "plata"}  # Con qué se craftea cada moneda (y en qué se funde)

# Emojis propios de las monedas (se buscan por nombre al conectarse; si faltan, puntos de colores)
tarjetas.register_emoji("oro", ("gold", "oro"), "🟡")
tarjetas.register_emoji("plata", ("silver", "plata"), "⚪")
tarjetas.register_emoji("cobre", ("copper", "cobre"), "🟤")
tarjetas.register_emoji("bolsa", ("pouch", "bolsa"), "🎒")  # Icono de la bolsa (emoji propio; si falta, 🎒)


@dataclass
class Purse:
    """Lo que lleva una persona en la bolsa, moneda a moneda."""
    cobre: int = 0
    plata: int = 0
    oro: int = 0

    @property
    def value(self) -> int:
        """Lo que vale todo, en cobre."""
        return self.cobre + self.plata * PLATA + self.oro * ORO

    def get(self, coin: str) -> int:
        return getattr(self, coin)


def fmt_number(number: int) -> str:
    """12345 -> '12.345'."""
    return f"{number:,}".replace(",", ".")


def coin(name: str, amount: int) -> str:
    """'(cobre) **1.300**'. Con los emojis propios no hace falta el nombre; con los de respaldo, sí."""
    return f"{tarjetas.emoji(name)} **{fmt_number(amount)}**" + ("" if tarjetas.has_emoji(name) else f" {name}")


def format_purse(purse: Purse) -> str:
    """'(oro) **1** · (plata) **23** · (cobre) **45**', sin las monedas que estén a 0."""
    parts = [coin(name, purse.get(name)) for name in COIN_NAMES if purse.get(name)]
    return tarjetas.ITEM_SEP.join(parts) or coin("cobre", 0)


def level_rewards(from_level: int, to_level: int) -> int:
    """Cobre que dan los niveles de from_level + 1 a to_level (el nivel N da N × 100)."""
    def total(level: int) -> int:
        return LEVEL_REWARD * level * (level + 1) // 2
    return max(0, total(to_level) - total(from_level))


# ==========================================
# BASE DE DATOS
# ==========================================

def init_db():
    with db.get_db_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS monedas (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            cobre INTEGER NOT NULL DEFAULT 0,
            nivel_pagado INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )""")
        # Antes solo había un número, el valor en cobre: al añadir las columnas, todo lo que tenía cada uno
        # se queda en cobre (y la plata y el oro, a 0), que es como tiene que empezar todo el mundo
        columns = {row[1] for row in conn.execute("PRAGMA table_info(monedas)")}
        for name in ("plata", "oro"):
            if name not in columns:
                conn.execute(f"ALTER TABLE monedas ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")


def _placeholders(group: List[int]) -> str:
    return ",".join("?" * len(group))


def _pay(conn, guild_id: int, group: List[int], level: int) -> int:
    """
    Paga en cobre los niveles de la persona que aún no tenía pagados y apunta hasta cuál se le ha pagado.
    Lo cobra su cuenta principal (group[0]). Devuelve lo pagado.
    """
    paid = conn.execute(f"SELECT COALESCE(MAX(nivel_pagado), 0) FROM monedas WHERE guild_id = ? "
                        f"AND user_id IN ({_placeholders(group)})", (guild_id, *group)).fetchone()[0]
    amount = level_rewards(paid, level)
    if amount:
        conn.execute(
            "INSERT INTO monedas (guild_id, user_id, cobre, nivel_pagado) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET cobre = cobre + excluded.cobre, "
            "nivel_pagado = excluded.nivel_pagado",
            (guild_id, group[0], amount, level)
        )
    return amount


def pay_levels(guild_id: int, group: List[int], level: int) -> int:
    """Paga a una persona los niveles hasta `level` que no tuviera pagados. Devuelve lo pagado, en cobre."""
    with db.get_db_connection() as conn:
        # Bloquea la base de datos desde la lectura: dos subidas a la vez no pagan el mismo nivel dos veces
        conn.execute("BEGIN IMMEDIATE")
        return _pay(conn, guild_id, group, level)


def pay_levels_many(guild_id: int, people: Iterable[Tuple[List[int], int]]) -> Tuple[int, int]:
    """Lo mismo para muchas personas en una sola transacción. Devuelve (personas pagadas, cobre en total)."""
    paid_people = total = 0
    with db.get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for group, level in people:
            amount = _pay(conn, guild_id, group, level)
            if amount:
                paid_people += 1
                total += amount
    return paid_people, total


def _purse(conn, guild_id: int, group: List[int]) -> Purse:
    row = conn.execute(f"SELECT COALESCE(SUM(cobre), 0), COALESCE(SUM(plata), 0), COALESCE(SUM(oro), 0) "
                       f"FROM monedas WHERE guild_id = ? AND user_id IN ({_placeholders(group)})",
                       (guild_id, *group)).fetchone()
    return Purse(*row)


def person_purse(guild_id: int, group: List[int]) -> Purse:
    """Las monedas de una persona (suma de sus cuentas)."""
    with db.get_db_connection() as conn:
        return _purse(conn, guild_id, group)


def _exchange(guild_id: int, group: List[int], source: str, target: str, cost: int, gain: int,
              count: Optional[int]) -> Tuple[Purse, int]:
    """
    Cambia monedas `source` por `target`: cada vez gasta `cost` y da `gain`, `count` veces (None = todas
    las que se pueda). Las monedas de la persona pasan a su cuenta principal. Devuelve (bolsa, veces hechas).
    """
    with db.get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")  # Leer y escribir sin que se cuele otro cambio en medio
        purse = _purse(conn, guild_id, group)
        possible = purse.get(source) // cost
        done = possible if count is None else min(count, possible)
        if done:
            setattr(purse, source, purse.get(source) - done * cost)
            setattr(purse, target, purse.get(target) + done * gain)
            if len(group) > 1:
                conn.execute(f"UPDATE monedas SET cobre = 0, plata = 0, oro = 0 WHERE guild_id = ? "
                             f"AND user_id IN ({_placeholders(group[1:])})", (guild_id, *group[1:]))
            conn.execute(
                "INSERT INTO monedas (guild_id, user_id, cobre, plata, oro) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET cobre = excluded.cobre, plata = excluded.plata, "
                "oro = excluded.oro",
                (guild_id, group[0], purse.cobre, purse.plata, purse.oro)
            )
    return purse, done


def craft(guild_id: int, group: List[int], target: str, count: Optional[int]) -> Tuple[Purse, int]:
    """Craftea `count` monedas de `target` (plata u oro; None = todas las que pueda). Devuelve (bolsa, hechas)."""
    return _exchange(guild_id, group, LOWER[target], target, RATE, 1, count)


def melt(guild_id: int, group: List[int], source: str, count: Optional[int]) -> Tuple[Purse, int]:
    """Funde `count` monedas de `source` (plata u oro) en 100 de la anterior. Devuelve (bolsa, fundidas)."""
    return _exchange(guild_id, group, source, LOWER[source], 1, RATE, count)
