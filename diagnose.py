#!/usr/bin/env python3
"""
Script de diagnóstico para problemas de voz y de YouTube en el bot de Discord.
Ejecuta esto en la consola de SparkedHost (python diagnose.py) para ver qué falta.
"""

import importlib
import os
import shutil
import subprocess
import sys

problems = []


def check(title):
    print(f"\n{title}")


print("=" * 60)
print("🔍 DIAGNÓSTICO - ReimbouBOT")
print("=" * 60)

# --- Python ---
check("🐍 Python")
print(f"   {sys.version.split()[0]} ({sys.executable})")
if sys.version_info < (3, 10):
    print("   ❌ Se necesita Python 3.10 o superior (yt-dlp y deno ya no admiten versiones anteriores)")
    problems.append("Cambia la versión de Python del servidor a 3.11 o 3.12 (en SparkedHost: pestaña Startup)")
else:
    print("   ✅ Versión compatible")

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

for module, fix in (("davey", 'pip install -U "discord.py[voice]"'),
                    ("nacl", 'pip install -U "discord.py[voice]"')):
    try:
        mod = importlib.import_module(module)
        print(f"   ✅ {module} {getattr(mod, '__version__', '')}")
    except ImportError:
        print(f"   ❌ Falta '{module}' (necesario para la voz)")
        problems.append(fix)

# --- Opus ---
try:
    import discord
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
        except Exception:
            pass
    print(f"   {'✅' if discord.opus.is_loaded() else '⚠️ '} Opus {'cargado' if discord.opus.is_loaded() else 'no cargado (puede ser normal hasta conectar a voz)'}")
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
        out = subprocess.run([deno_path, "--version"], capture_output=True, text=True, timeout=20)
        print(f"   ✅ Deno: {out.stdout.splitlines()[0] if out.stdout else deno_path}")
    except Exception as e:
        print(f"   ⚠️  Deno encontrado en {deno_path} pero no se pudo ejecutar: {e}")
else:
    print("   ❌ Deno no encontrado (YouTube irá peor o fallará)")
    problems.append("pip install -U deno")

# --- FFmpeg ---
check("🎬 FFmpeg")
try:
    result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
    print(f"   ✅ {result.stdout.splitlines()[0]}" if result.returncode == 0 else "   ⚠️  FFmpeg responde con error")
except FileNotFoundError:
    print("   ❌ FFmpeg NO encontrado")
    problems.append("Instalar FFmpeg (en SparkedHost: pide soporte o usa una imagen de Python que lo incluya)")
except Exception as e:
    print(f"   ⚠️  Error al verificar FFmpeg: {e}")

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
    print("⚠️  Se encontraron problemas. Soluciones (en SparkedHost añade --prefix .local a pip):\n")
    for fix in dict.fromkeys(problems):  # sin repetidos, en orden
        print(f"   • {fix}")
print("=" * 60)
