#!/usr/bin/env python3
"""
Script de diagnóstico para problemas de voz, YouTube y la palabra del día.
Ejecútalo en la Raspberry Pi con el entorno virtual del bot activado:  python diagnose.py
"""

import importlib
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path

problems = []
BITS = struct.calcsize("P") * 8


def check(title):
    print(f"\n{title}")


def read_value(path, prefix=""):
    """Primera línea de un archivo del sistema que empiece por `prefix` (sin el prefijo)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(prefix):
                    return line[len(prefix):].strip().strip('"').strip("\x00")
    except OSError:
        pass
    return None


print("=" * 60)
print("🔍 DIAGNÓSTICO - ReimbouBOT")
print("=" * 60)

# --- Sistema ---
check("🖥️  Sistema")
model = read_value("/proc/device-tree/model")
if model:
    print(f"   {model}")
print(f"   {read_value('/etc/os-release', 'PRETTY_NAME=') or platform.platform()}")
print(f"   {platform.machine()} · Python de {BITS} bits")

# --- Python ---
check("🐍 Python")
print(f"   {sys.version.split()[0]} ({sys.executable})")
if sys.version_info < (3, 10):
    print("   ❌ Se necesita Python 3.10 o superior (yt-dlp y deno ya no admiten versiones anteriores)")
    problems.append("Actualiza Python: Raspberry Pi OS Bookworm (12) o posterior trae 3.11+. En Bullseye (11) "
                    "actualiza el sistema o instala un Python más nuevo (p. ej. con: uv python install 3.12)")
else:
    print("   ✅ Versión compatible")
if sys.prefix == sys.base_prefix:
    print("   ⚠️  No estás en un entorno virtual: ¿has activado el del bot? (source .venv/bin/activate)")

# --- discord.py + DAVE ---
check("📦 discord.py y cifrado de voz (DAVE)")
try:
    import discord
    print(f"   discord.py {discord.__version__}")
    if discord.version_info < (2, 7):
        print("   ❌ Anterior a 2.7: desde marzo de 2026 Discord exige cifrado E2EE en voz y el bot no podrá entrar")
        problems.append('pip install -U "discord.py[voice]"')
    else:
        print("   ✅ Soporta DAVE")
except ImportError as e:
    print(f"   ❌ discord.py NO instalado: {e}")
    problems.append('pip install -U "discord.py[voice]"')

for module in ("davey", "nacl"):
    try:
        mod = importlib.import_module(module)
        print(f"   ✅ {module} {getattr(mod, '__version__', '')}")
    except ImportError:
        print(f"   ❌ Falta '{module}' (necesario para la voz)")
        problems.append('pip install -U "discord.py[voice]"')

# --- Opus (codifica el audio que se envía a Discord) ---
try:
    import discord
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
        except Exception:
            pass
    if discord.opus.is_loaded():
        print("   ✅ Opus cargado")
    else:
        print("   ❌ No encuentro la librería Opus (necesaria para enviar audio)")
        problems.append("sudo apt install libopus0")
except Exception as e:
    print(f"   ⚠️  No se pudo verificar Opus: {e}")

# --- YouTube: yt-dlp, EJS y Deno ---
check("🎵 YouTube (yt-dlp + Deno)")
try:
    import yt_dlp
    print(f"   yt-dlp {yt_dlp.version.__version__}  (actualízalo a menudo: pip install -U \"yt-dlp[default]\")")
except ImportError as e:
    print(f"   ❌ yt-dlp NO instalado: {e}")
    problems.append('pip install -U "yt-dlp[default]"')

try:
    import yt_dlp_ejs  # noqa: F401
    print("   ✅ yt-dlp-ejs instalado")
except ImportError:
    print("   ❌ Falta yt-dlp-ejs")
    problems.append('pip install -U "yt-dlp[default]"')

deno_path = None
try:
    import deno
    deno_path = deno.find_deno_bin()
except Exception:
    deno_path = shutil.which("deno")
if deno_path:
    try:
        out = subprocess.run([deno_path, "--version"], capture_output=True, text=True, timeout=30)
        print(f"   ✅ Deno: {out.stdout.splitlines()[0] if out.stdout else deno_path}")
    except Exception as e:
        print(f"   ⚠️  Deno encontrado en {deno_path} pero no se pudo ejecutar: {e}")
elif BITS == 32:
    print("   ℹ️  Deno no existe para sistemas de 32 bits. No es obligatorio: YouTube funciona sin él por ahora")
else:
    print("   ❌ Deno no encontrado (YouTube irá peor o fallará)")
    problems.append("pip install -U deno")

# --- FFmpeg ---
check("🎬 FFmpeg")
try:
    result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=10)
    print(f"   ✅ {result.stdout.splitlines()[0]}" if result.returncode == 0 else "   ⚠️  FFmpeg responde con error")
except FileNotFoundError:
    print("   ❌ FFmpeg NO encontrado")
    problems.append("sudo apt install ffmpeg")
except Exception as e:
    print(f"   ⚠️  Error al verificar FFmpeg: {e}")

# --- Palabra del día ---
check("🟩 Palabra del día")
data_dir = Path(__file__).parent / "datos"
for name in ("palabras_validas.txt", "palabras_respuestas.txt"):
    exists = (data_dir / name).is_file()
    print(f"   {'✅' if exists else '❌'} datos/{name}")
    if not exists:
        problems.append("Copia la carpeta datos/ del repositorio (git pull)")
try:
    from zoneinfo import ZoneInfo
    ZoneInfo("Europe/Madrid")
    print("   ✅ Zona horaria de España")
except Exception:
    print("   ❌ Sin zona horaria de España (la palabra cambiaría a medianoche UTC)")
    problems.append("pip install -U tzdata")

# --- Configuración ---
check("⚙️  Configuración (.env)")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    problems.append("pip install -U python-dotenv")
print(f"   {'✅' if os.environ.get('TOKEN') else '❌'} TOKEN")
print(f"   {'✅' if os.environ.get('ADMIN_LOG_CHANNEL_ID') else '⚠️ '} ADMIN_LOG_CHANNEL_ID")
if not os.environ.get('TOKEN'):
    problems.append("Configura TOKEN en el archivo .env (copia .env.example)")

# --- Resumen ---
print("\n" + "=" * 60)
print("📊 RESUMEN")
print("=" * 60)
if not problems:
    print("✅ ¡Todo parece estar OK!")
else:
    print("⚠️  Se encontraron problemas. Soluciones (los pip, con el entorno virtual activado):\n")
    for fix in dict.fromkeys(problems):  # sin repetidos, en orden
        print(f"   • {fix}")
print("=" * 60)
