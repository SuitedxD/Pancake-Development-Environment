# Example Custom Events consumer for Pancake Development Environment.
#
# Install with /install eventlistener. It receives events from eventsource.py.

import discord


class EventlistenerPlugin:
    def __init__(self, bot):
        self.bot = bot
        self._subscription = None

    def get_commands(self):
        # This plugin demonstrates an event consumer and does not add a slash command.
        return []

    async def on_plugin_load(self, guild: discord.Guild):
        manager = getattr(self.bot, "custom_events", None)
        if manager is None:
            raise RuntimeError("PDE Custom Events manager is not available.")

        self._subscription = await manager.subscribe(
            guild_id=guild.id,
            event_name="demo.message_published",
            plugin_id="eventlistener",
            consumer_id="demo_message_listener",
            schema_version=1,
            callback=self.on_message_published,
        )

    async def on_message_published(self, event):
        channel_id = int(event.data["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        await channel.send(
            "🥞 **Custom Event received**\n"
            f"Message: {discord.utils.escape_markdown(event.data['message'])}\n"
            f"Author ID: `{event.data['author_id']}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )
