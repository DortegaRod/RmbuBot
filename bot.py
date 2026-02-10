# ... (mantén tus imports y eventos de logs igual) ...

@bot.tree.command(name="play", description="Reproduce música de YouTube")
async def play(interaction: discord.Interaction, busqueda: str):
    if not check_music_channel(interaction):
        return await interaction.response.send_message(f"❌ Solo en <#{MUSIC_CHANNEL_ID}>", ephemeral=True)
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ Entra a un canal de voz primero.", ephemeral=True)

    await interaction.response.defer()

    # 1. Buscar (ahora devuelve una lista)
    songs = await search_youtube(busqueda)
    if not songs:
        return await interaction.followup.send("❌ No encontré resultados válidos.")

    # 2. Conectar
    guild = interaction.guild
    voice_channel = interaction.user.voice.channel
    player = music_manager.get_player(guild)
    vc = guild.voice_client
    try:
        if not vc:
            vc = await voice_channel.connect(self_deaf=True)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    except:
        return await interaction.followup.send("❌ Error de conexión.")

    # 3. Procesar canciones
    for s in songs: s.requester = interaction.user

    first_song = songs[0]
    for s in songs: player.add_song(s)

    # 4. Reproducir si no hay nada
    is_playing_now = False
    if not vc.is_playing() and not player.current:
        await play_next(vc, player)
        is_playing_now = True

    # 5. Embed de respuesta
    if len(songs) > 1:
        embed = discord.Embed(
            title="📂 Playlist Añadida",
            description=f"Se han añadido **{len(songs)}** canciones a la cola.",
            color=discord.Color.purple()
        )
    else:
        embed = discord.Embed(
            title="🎶 Reproduciendo ahora" if is_playing_now else "📝 Añadido a la cola",
            description=f"**[{first_song.title}]({first_song.webpage_url})**",
            color=discord.Color.green() if is_playing_now else discord.Color.blue()
        )
        if first_song.thumbnail: embed.set_thumbnail(url=first_song.thumbnail)

    embed.set_footer(text=f"Pedido por {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

# ... (mantén el resto de comandos igual) ...