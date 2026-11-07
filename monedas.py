"""
Monedas: cobre, plata y oro, al estilo de un RPG.

- Se guarda un solo número, en cobre; se enseña desglosado: 1 oro = 100 de plata = 10.000 de cobre.
  Así nunca hay que cambiar monedas a mano (99 de cobre más 1 son 1 de plata).
- Se ganan subiendo de nivel: llegar al nivel N da N de plata. Se guarda hasta qué nivel se le ha pagado
  a cada persona, así que ningún nivel se paga dos veces (aunque un admin le baje y le vuelva a subir el
  nivel, o se vinculen cuentas). Los niveles que ya tenía cada uno se pagan al arrancar el bot (niveles.py).
- Todo va por persona: las cuentas vinculadas (cuentas.py) suman sus monedas.
"""

from typing import Iterable, List, Tuple

import db

PLATA = 100          # Cobre que vale una de plata
ORO = 100 * PLATA    # Cobre que vale una de oro
LEVEL_REWARD = PLATA  # Llegar al nivel N da N veces esto (N de plata)

# Emojis de la aplicación (Developer Portal → el bot → Emojis): el bot los puede usar en cualquier servidor.
# Si se vuelven a subir, cambian de ID: hay que copiar aquí los nuevos.
EMOJI_ORO = "<:gold:1568622574965887048>"
EMOJI_PLATA = "<:silver:1568622565012803594>"
EMOJI_COBRE = "<:copper:1568622542980255835>"
COINS = (("oro", EMOJI_ORO, ORO), ("plata", EMOJI_PLATA, PLATA), ("cobre", EMOJI_COBRE, 1))


def fmt_number(number: int) -> str:
    """12345 -> '12.345'."""
    return f"{number:,}".replace(",", ".")


def format_money(cobre: int) -> str:
    """12345 -> '(oro) **1** oro · (plata) **23** plata · (cobre) **45** cobre' (sin las monedas que estén a 0)."""
    parts = []
    for name, emoji, value in COINS:
        amount, cobre = divmod(cobre, value)
        if amount:
            parts.append(f"{emoji} **{fmt_number(amount)}** {name}")
    return " · ".join(parts) or f"{EMOJI_COBRE} **0** cobre"


def level_rewards(from_level: int, to_level: int) -> int:
    """Cobre que dan los niveles de from_level + 1 a to_level (el nivel N da N de plata)."""
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


def _pay(conn, guild_id: int, group: List[int], level: int) -> int:
    """
    Paga los niveles de la persona que aún no tenía pagados y apunta hasta cuál se le ha pagado.
    Lo cobra su cuenta principal (group[0]). Devuelve lo pagado, en cobre.
    """
    placeholders = ",".join("?" * len(group))
    paid = conn.execute(f"SELECT COALESCE(MAX(nivel_pagado), 0) FROM monedas WHERE guild_id = ? "
                        f"AND user_id IN ({placeholders})", (guild_id, *group)).fetchone()[0]
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


def person_money(guild_id: int, group: List[int]) -> int:
    """Monedas de una persona (suma de sus cuentas), en cobre."""
    placeholders = ",".join("?" * len(group))
    with db.get_db_connection() as conn:
        row = conn.execute(f"SELECT COALESCE(SUM(cobre), 0) FROM monedas WHERE guild_id = ? "
                           f"AND user_id IN ({placeholders})", (guild_id, *group)).fetchone()
    return row[0]
