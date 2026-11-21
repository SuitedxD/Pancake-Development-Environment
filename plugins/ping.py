# --- Your Plugin Starts Here ---
# Complete this section before submitting your plugin.

# PLUGIN TITLE: Ping
# PLUGIN VERSION: 0.1.0
# PLUGIN DEPENDENCIES: N/A
# YOUR DISCORD ID: 559855365597036585
# PLUGIN DESCRIPTION: Basic ping commands for testing.
# PLUGIN INSTALLATION COMMAND: /install plugin:ping
# COVER IMAGE URL (OPTIONAL): N/A
# CATEGORIES: Utility

# PLUGIN COMMANDS AND DESCRIPTIONS:
# command: /ping
# description: Returns with pong.

# command: /ping-ms
# description: Returns bot latency in ms.

# ADDITIONAL IMAGE URLS (OPTIONAL): N/A
# YOUTUBE VIDEO URLS (OPTIONAL): N/A
# ADDITIONAL NOTES (OPTIONAL; MARKDOWN SUPPORTED): N/A

# --- Your Code Starts Here ---

import discord
from discord import app_commands

class PingPlugin:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._ping = app_commands.Command(
            name="ping",
            description="Replies with a Pong message.",
            callback=self.ping,
        )
        self._ping_ms = app_commands.Command(
            name="ping-ms",
            description="Displays the bot's WebSocket latency in milliseconds.",
            callback=self.ping_ms,
        )

    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pong! 🥞")

    async def ping_ms(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"Current bot latency: **{latency} ms**"
        )

    def get_commands(self):
        return [self._ping, self._ping_ms]