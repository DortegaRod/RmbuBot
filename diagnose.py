#!/usr/bin/env python3
"""
Script de diagnóstico para problemas de voz en Discord bot
Ejecuta esto en la consola de SparkedHost para ver qué falta
"""

import sys
import os

print("=" * 60)
print("🔍 DIAGNÓSTICO DE VOZ - Discord Bot")
print("=" * 60)
print()

# Python version
print(f"Python: {sys.version}")
print(f"Prefix: {sys.prefix}")
print()

# Verificar discord.py
print("📦 Verificando discord.py...")
try:
    import discord

    print(f"✅ discord.py instalado: {discord.__version__}")

    # Verificar si tiene soporte de voz
    try:
        from discord import opus

        print(f"✅ discord.opus importable")
    except ImportError as e:
        print(f"❌ discord.opus no disponible: {e}")

except ImportError as e:
    print(f"❌ discord.py NO instalado: {e}")
    print("   Ejecuta: pip3 install discord.py[voice] --prefix .local")
print()

# Verificar PyNaCl (CRÍTICO para voz)
print("🔐 Verificando PyNaCl (encriptación de voz)...")
try:
    import nacl

    print(f"✅ PyNaCl instalado: {nacl.__version__}")
    print(f"   Ubicación: {nacl.__file__}")

    # Verificar que puede importar componentes necesarios
    try:
        from nacl import secret, utils

        print(f"✅ PyNaCl completamente funcional")
    except ImportError as e:
        print(f"⚠️  PyNaCl parcial: {e}")

except ImportError as e:
    print(f"❌ PyNaCl NO instalado: {e}")
    print("   ⚠️  ESTE ES PROBABLEMENTE TU PROBLEMA")
    print("   Ejecuta: pip3 install PyNaCl==1.5.0 --prefix .local --force-reinstall")
print()

# Verificar libsodium (sistema)
print("🧪 Verificando libsodium (librería del sistema)...")
try:
    import ctypes.util

    lib = ctypes.util.find_library('sodium')
    if lib:
        print(f"✅ libsodium encontrado: {lib}")
    else:
        print(f"❌ libsodium NO encontrado en el sistema")
        print("   Esto puede causar problemas con PyNaCl")
        print("   Contacta a SparkedHost para que lo instalen")
except Exception as e:
    print(f"⚠️  No se pudo verificar: {e}")
print()

# Verificar yt-dlp
print("🎵 Verificando yt-dlp...")
try:
    import yt_dlp

    print(f"✅ yt-dlp instalado")
    print(f"   Ubicación: {yt_dlp.__file__}")
except ImportError as e:
    print(f"❌ yt-dlp NO instalado: {e}")
    print("   Ejecuta: pip3 install yt-dlp --prefix .local")
print()

# Verificar FFmpeg
print("🎬 Verificando FFmpeg...")
import subprocess

try:
    result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        version_line = result.stdout.split('\n')[0]
        print(f"✅ FFmpeg instalado: {version_line}")
    else:
        print(f"⚠️  FFmpeg responde pero con error")
except FileNotFoundError:
    print(f"❌ FFmpeg NO encontrado")
    print("   Contacta a SparkedHost para que lo instalen")
except Exception as e:
    print(f"⚠️  Error al verificar FFmpeg: {e}")
print()

# Verificar Opus (codec de audio)
print("🎤 Verificando Opus...")
try:
    import discord

    if discord.opus.is_loaded():
        print(f"✅ Opus cargado correctamente")
    else:
        print(f"⚠️  Opus no cargado")
        try:
            discord.opus.load_opus('opus')
            print(f"✅ Opus cargado manualmente")
        except:
            print(f"⚠️  No se pudo cargar Opus (puede ser normal)")
except Exception as e:
    print(f"⚠️  No se pudo verificar Opus: {e}")
print()

# Variables de entorno
print("⚙️  Verificando configuración...")
token_set = bool(os.environ.get('TOKEN'))
channel_set = bool(os.environ.get('ADMIN_LOG_CHANNEL_ID'))

if token_set:
    print(f"✅ TOKEN configurado")
else:
    print(f"❌ TOKEN no configurado")

if channel_set:
    print(f"✅ ADMIN_LOG_CHANNEL_ID configurado")
else:
    print(f"⚠️  ADMIN_LOG_CHANNEL_ID no configurado")
print()

# Test de importación completo
print("🧪 Test de importación completo...")
try:
    from discord.ext import commands
    from discord import app_commands
    import asyncio

    print(f"✅ Todas las importaciones básicas OK")
except ImportError as e:
    print(f"❌ Error en importaciones: {e}")
print()

# Resumen
print("=" * 60)
print("📊 RESUMEN")
print("=" * 60)

issues = []

# Verificar PyNaCl
try:
    import nacl
except ImportError:
    issues.append("❌ CRÍTICO: PyNaCl no instalado (causa error de voz)")

# Verificar discord.py
try:
    import discord

    if discord.__version__ < "2.0":
        issues.append("⚠️  discord.py version antigua")
except ImportError:
    issues.append("❌ CRÍTICO: discord.py no instalado")

# Verificar yt-dlp
try:
    import yt_dlp
except ImportError:
    issues.append("⚠️  yt-dlp no instalado (música no funcionará)")

if not issues:
    print("✅ ¡Todo parece estar OK!")
    print()
    print("Si aún tienes errores de voz, prueba:")
    print("1. Reinstalar PyNaCl: pip3 install PyNaCl --prefix .local --force-reinstall")
    print("2. Contactar a SparkedHost sobre libsodium")
else:
    print("⚠️  Se encontraron problemas:")
    print()
    for issue in issues:
        print(f"  {issue}")
    print()
    print("🔧 SOLUCIONES:")
    print()
    if any("PyNaCl" in i for i in issues):
        print("Para PyNaCl:")
        print("  pip3 install PyNaCl==1.5.0 --prefix .local --force-reinstall --no-cache-dir")
        print()
    if any("discord.py" in i for i in issues):
        print("Para discord.py:")
        print("  pip3 install discord.py[voice]==2.4.0 --prefix .local --force-reinstall")
        print()
    if any("yt-dlp" in i for i in issues):
        print("Para yt-dlp:")
        print("  pip3 install yt-dlp --prefix .local")
        print()

print("=" * 60)
print("Para más ayuda, consulta FIX_VOICE_ERROR.md")
print("=" * 60)