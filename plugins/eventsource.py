# Example Custom Events publisher for Pancake Development Environment.
#
# Install with /install eventsource. Use /event-publish to publish a demo event.

import discord
from discord import app_commands


DEMO_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "minLength": 1, "maxLength": 500},
        "channel_id": {"type": "string", "minLength": 1},
        "author_id": {"type": "string", "minLength": 1},
    },
    "required": ["message", "channel_id", "author_id"],
    "additionalProperties": False,
}


class EventsourcePlugin:
    def __init__(self, bot):
        self.bot = bot
        self._commands = [
            app_commands.Command(
                name="event-publish",
                description="Publish a demo Custom Event for the installed listener plugin.",
                callback=self.publish,
            )
        ]

    def get_commands(self):
        return self._commands

    async def on_plugin_load(self, guild: discord.Guild):
        manager = getattr(self.bot, "custom_events", None)
        if manager is None:
            raise RuntimeError("PDE Custom Events manager is not available.")

        await manager.register_emitter(
            guild_id=guild.id,
            event_name="demo.message_published",
            resource_type="demo_command",
            resource_id="event-publish",
            plugin_id="eventsource",
            schema_version=1,
            schema=DEMO_EVENT_SCHEMA,
        )

    async def publish(self, interaction: discord.Interaction, message: str):
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Use this command inside a test server channel.", ephemeral=True
            )
            return

        manager = getattr(self.bot, "custom_events", None)
        if manager is None:
            await interaction.response.send_message(
                "Custom Events is not available in this runtime.", ephemeral=True
            )
            return

        accepted = await manager.emit(
            guild_id=interaction.guild_id,
            event_name="demo.message_published",
            emitter_plugin_id="eventsource",
            resource_type="demo_command",
            resource_id="event-publish",
            schema_version=1,
            data={
                "message": message,
                "channel_id": str(interaction.channel_id),
                "author_id": str(interaction.user.id),
            },
            actor_user_id=interaction.user.id,
        )

        if accepted:
            await interaction.response.send_message(
                "Custom Event accepted and queued for delivery.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "The event was not accepted. Check that both demo plugins are installed "
                "and review the PDE console for details.",
                ephemeral=True,
            )
