"""Discord signup panel for Healer and Starrk roster slots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("signup_bot")

ROLE_CAPACITIES = {"Healer": 3, "Starrk": 9}
STATE_FILE = Path(__file__).with_name("roster.json")
PANEL_VIEW_TIMEOUT = None
EPHEMERAL_VIEW_TIMEOUT = 300


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "panel_channel_id": None,
        "panel_message_id": None,
        "roster": {role: {} for role in ROLE_CAPACITIES},
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return empty_state()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("unsupported roster file version")

        roster = data["roster"]
        if not isinstance(roster, dict):
            raise ValueError("roster must be an object")

        normalized_roster: dict[str, dict[str, dict[str, Any]]] = {}
        for role, capacity in ROLE_CAPACITIES.items():
            members = roster.get(role, {})
            if not isinstance(members, dict) or len(members) > capacity:
                raise ValueError(f"invalid member list for {role}")

            normalized_members: dict[str, dict[str, Any]] = {}
            for user_id, record in members.items():
                if not isinstance(record, dict):
                    raise ValueError(f"invalid member record for {role}")
                original_nickname = record.get("original_nickname")
                nickname_managed = record.get("nickname_managed", False)
                if original_nickname is not None and not isinstance(original_nickname, str):
                    raise ValueError(f"invalid original nickname for {role}")
                if not isinstance(nickname_managed, bool):
                    raise ValueError(f"invalid nickname status for {role}")
                normalized_members[str(user_id)] = {
                    "ign": str(record["ign"]),
                    "roblox_username": str(record["roblox_username"]),
                    "discord_name": str(record.get("discord_name", "Discord user")),
                    "original_nickname": original_nickname,
                    # Older roster entries predate nickname management.
                    "nickname_managed": nickname_managed,
                }
            normalized_roster[role] = normalized_members

        panel_channel_id = data.get("panel_channel_id")
        panel_message_id = data.get("panel_message_id")
        return {
            "version": 1,
            "panel_channel_id": int(panel_channel_id) if panel_channel_id else None,
            "panel_message_id": int(panel_message_id) if panel_message_id else None,
            "roster": normalized_roster,
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Could not load {STATE_FILE.name}: {exc}. "
            "Fix or remove the damaged roster file before restarting the bot."
        ) from exc


state = load_state()
state_lock = asyncio.Lock()


def save_state() -> None:
    """Write the roster atomically so an interrupted write cannot corrupt it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=STATE_FILE.parent,
            prefix=f".{STATE_FILE.name}.",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(state, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, STATE_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def safe_embed_text(value: str, limit: int = 80) -> str:
    """Keep user-entered text on one line and prevent Discord mentions."""
    cleaned = " ".join(value.replace("\x00", "").split())
    cleaned = cleaned.replace("@", "@\u200b")
    return cleaned[:limit] or "—"


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Roster Sign-Ups",
        description=(
            "Choose **Sign Up** to submit your game details and select a role. "
            "Use **Leave Roster** if you need to give up your spot."
        ),
        color=discord.Color.blurple(),
    )

    roster = state["roster"]
    for role, capacity in ROLE_CAPACITIES.items():
        members = list(roster[role].items())
        lines = []
        for slot_number in range(1, capacity + 1):
            if slot_number <= len(members):
                user_id, member = members[slot_number - 1]
                ign = safe_embed_text(member["ign"], 32)
                roblox_username = safe_embed_text(member["roblox_username"], 32)
                discord_name = safe_embed_text(member["discord_name"], 48)
                lines.append(
                    f"**{slot_number}.** {ign} — Roblox: `{roblox_username}` "
                    f"· Discord: {discord_name} (<@{user_id}>)"
                )
            else:
                lines.append(f"**{slot_number}.** Open")

        embed.add_field(
            name=f"{role} — {len(members)}/{capacity}",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text="Use !setup in this channel to create or refresh this panel.")
    return embed


async def refresh_panel() -> None:
    channel_id = state.get("panel_channel_id")
    message_id = state.get("panel_message_id")
    if not channel_id or not message_id:
        return

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        await message.edit(
            embed=build_panel_embed(),
            view=SignupPanelView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.NotFound:
        logger.warning("The saved signup panel message was deleted.")
    except discord.Forbidden:
        logger.warning("The bot cannot access or edit the saved signup panel.")
    except discord.HTTPException:
        logger.exception("Could not refresh the signup panel.")


async def register_member(
    interaction: discord.Interaction,
    role: str,
    ign: str,
    roblox_username: str,
) -> str:
    """Return 'added', 'already_registered', 'full', or 'nickname_failed'."""
    member = interaction.user
    if not isinstance(member, discord.Member):
        return "nickname_failed"

    clean_ign = ign.strip()
    clean_roblox_username = roblox_username.strip()
    if not clean_ign or not clean_roblox_username:
        return "invalid_details"

    user_id = str(member.id)
    async with state_lock:
        if any(user_id in members for members in state["roster"].values()):
            return "already_registered"

        members = state["roster"][role]
        if len(members) >= ROLE_CAPACITIES[role]:
            return "full"

        members[user_id] = {
            "ign": clean_ign,
            "roblox_username": clean_roblox_username,
            "discord_name": safe_embed_text(member.display_name, 48),
            "original_nickname": member.nick,
            "nickname_managed": True,
        }
        save_state()
        try:
            await member.edit(nick=clean_ign, reason="Roster signup")
        except discord.HTTPException:
            logger.exception("Could not change the nickname for roster member %s.", user_id)
            del members[user_id]
            save_state()
            return "nickname_failed"

    await refresh_panel()
    return "added"


class SignupModal(discord.ui.Modal, title="Join the Roster"):
    ign = discord.ui.TextInput(
        label="In-Game Name (IGN)",
        placeholder="Enter your in-game name",
        min_length=1,
        max_length=32,
        required=True,
    )
    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter your Roblox username",
        min_length=1,
        max_length=32,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.ign.value.strip() or not self.roblox_username.value.strip():
            await interaction.response.send_message(
                "Please enter both an in-game name and Roblox username.",
                ephemeral=True,
            )
            return

        user_id = str(interaction.user.id)
        if any(user_id in members for members in state["roster"].values()):
            await interaction.response.send_message(
                "You are already on the roster. Use **Leave Roster** before signing up again.",
                ephemeral=True,
            )
            return

        open_roles = [
            role
            for role, capacity in ROLE_CAPACITIES.items()
            if len(state["roster"][role]) < capacity
        ]
        if not open_roles:
            await interaction.response.send_message(
                "All roster slots are currently full.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose your role:",
            view=RoleSelectionView(
                ign=self.ign.value.strip(),
                roblox_username=self.roblox_username.value.strip(),
                available_roles=open_roles,
            ),
            ephemeral=True,
        )


class RoleSelect(discord.ui.Select):
    def __init__(self, available_roles: list[str]) -> None:
        options = [
            discord.SelectOption(label=role, value=role)
            for role in available_roles
        ]
        super().__init__(
            placeholder="Select Healer or Starrk",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="signup_panel:role_select:v1",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RoleSelectionView):
            await interaction.response.send_message(
                "This signup has expired. Please start again from the panel.",
                ephemeral=True,
            )
            return

        role = self.values[0]
        result = await register_member(
            interaction,
            role,
            view.ign,
            view.roblox_username,
        )

        if result == "added":
            await interaction.response.edit_message(
                content=f"You are signed up as **{role}**, and your nickname is now your IGN.",
                view=None,
            )
        elif result == "full":
            open_roles = [
                available_role
                for available_role, capacity in ROLE_CAPACITIES.items()
                if len(state["roster"][available_role]) < capacity
            ]
            if open_roles:
                await interaction.response.edit_message(
                    content=f"All **{role}** slots just filled. Choose another role:",
                    view=RoleSelectionView(
                        ign=view.ign,
                        roblox_username=view.roblox_username,
                        available_roles=open_roles,
                    ),
                )
            else:
                await interaction.response.edit_message(
                    content="All roster slots are currently full.",
                    view=None,
                )
        else:
            if result == "nickname_failed":
                message = (
                    "I couldn't change your server nickname, so you were not added. "
                    "Ask a server manager to give the bot **Manage Nicknames** and "
                    "place its role above your role, then try again."
                )
            elif result == "invalid_details":
                message = "Enter both names before choosing a role."
            else:
                message = (
                    "You are already on the roster. Use **Leave Roster** "
                    "before signing up again."
                )
            await interaction.response.edit_message(content=message, view=None)


class RoleSelectionView(discord.ui.View):
    def __init__(
        self,
        ign: str,
        roblox_username: str,
        available_roles: list[str],
    ) -> None:
        super().__init__(timeout=EPHEMERAL_VIEW_TIMEOUT)
        self.ign = ign
        self.roblox_username = roblox_username
        self.add_item(RoleSelect(available_roles))


class SignupPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=PANEL_VIEW_TIMEOUT)

    @discord.ui.button(
        label="Sign Up",
        style=discord.ButtonStyle.primary,
        custom_id="signup_panel:signup:v1",
    )
    async def sign_up(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[SignupPanelView],
    ) -> None:
        user_id = str(interaction.user.id)
        if any(user_id in members for members in state["roster"].values()):
            await interaction.response.send_message(
                "You are already on the roster. Use **Leave Roster** to remove your spot.",
                ephemeral=True,
            )
            return
        if not any(
            len(state["roster"][role]) < capacity
            for role, capacity in ROLE_CAPACITIES.items()
        ):
            await interaction.response.send_message(
                "All roster slots are currently full.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SignupModal())

    @discord.ui.button(
        label="Leave Roster",
        style=discord.ButtonStyle.danger,
        custom_id="signup_panel:leave:v1",
    )
    async def leave_roster(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[SignupPanelView],
    ) -> None:
        user_id = str(interaction.user.id)
        removed_role: str | None = None
        nickname_restore_failed = False
        async with state_lock:
            for role, members in state["roster"].items():
                if user_id in members:
                    record = members[user_id]
                    if record.get("nickname_managed"):
                        if not isinstance(interaction.user, discord.Member):
                            nickname_restore_failed = True
                            break
                        try:
                            await interaction.user.edit(
                                nick=record.get("original_nickname"),
                                reason="Leaving the signup roster",
                            )
                        except discord.HTTPException:
                            logger.exception(
                                "Could not restore the nickname for roster member %s.",
                                user_id,
                            )
                            nickname_restore_failed = True
                            break

                    del members[user_id]
                    removed_role = role
                    save_state()
                    break

        if nickname_restore_failed:
            await interaction.response.send_message(
                "I couldn't restore your nickname, so your signup was kept. "
                "Ask a server manager to check the bot's **Manage Nicknames** "
                "permission and role position, then try again.",
                ephemeral=True,
            )
            return

        if removed_role is None:
            await interaction.response.send_message(
                "You are not currently on the roster.",
                ephemeral=True,
            )
            return

        await refresh_panel()
        await interaction.response.send_message(
            f"You have left the **{removed_role}** roster, and your previous nickname was restored.",
            ephemeral=True,
        )


class SignupBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Register persistent button callbacks so existing panels keep working
        # after a bot restart.
        self.add_view(SignupPanelView())


intents = discord.Intents.default()
intents.message_content = True  # Required to receive the !setup prefix command.
bot = SignupBot(command_prefix="!", intents=intents)


@bot.command(name="setup")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def setup_panel_command(ctx: commands.Context[SignupBot]) -> None:
    """Create the signup panel, or refresh it if one already exists here."""
    if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
        await ctx.send("Run `!setup` in a server text channel.")
        return

    message: discord.Message | None = None
    saved_channel_id = state.get("panel_channel_id")
    saved_message_id = state.get("panel_message_id")
    if saved_channel_id == ctx.channel.id and saved_message_id:
        try:
            message = await ctx.channel.fetch_message(saved_message_id)
        except discord.NotFound:
            message = None
        except discord.HTTPException:
            logger.exception("Could not fetch the saved signup panel.")

    if message is None:
        message = await ctx.send(
            embed=build_panel_embed(),
            view=SignupPanelView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await message.edit(
            embed=build_panel_embed(),
            view=SignupPanelView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async with state_lock:
        state["panel_channel_id"] = ctx.channel.id
        state["panel_message_id"] = message.id
        save_state()

    await ctx.send("Signup panel is ready.", delete_after=8)


@bot.command(name="reset")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def reset_roster_command(ctx: commands.Context[SignupBot]) -> None:
    """Restore saved nicknames and clear the active roster."""
    if ctx.guild is None:
        await ctx.send("Run `!reset` in a server text channel.")
        return

    restored_nicknames = 0
    cleared_signups = 0
    failed_restores = 0

    async with state_lock:
        for members in state["roster"].values():
            for user_id, record in list(members.items()):
                if record.get("nickname_managed"):
                    try:
                        member = ctx.guild.get_member(int(user_id))
                        if member is None:
                            member = await ctx.guild.fetch_member(int(user_id))
                        await member.edit(
                            nick=record.get("original_nickname"),
                            reason="Roster reset",
                        )
                        restored_nicknames += 1
                    except discord.NotFound:
                        # The member left the server; there is no nickname to restore.
                        pass
                    except discord.HTTPException:
                        logger.exception(
                            "Could not restore nickname for roster member %s.",
                            user_id,
                        )
                        failed_restores += 1
                        continue

                del members[user_id]
                cleared_signups += 1

        save_state()

    await refresh_panel()
    if failed_restores:
        await ctx.send(
            f"Reset partially completed: restored {restored_nicknames} nickname(s) "
            f"and cleared {cleared_signups} signup(s). {failed_restores} signup(s) "
            "remain so you can retry. Check the bot's **Manage Nicknames** permission "
            "and make sure its role is above the affected members, then run `!reset` again."
        )
    else:
        await ctx.send(
            f"Roster cleared. Restored {restored_nicknames} nickname(s) and "
            f"removed {cleared_signups} signup(s)."
        )


@bot.event
async def on_command_error(
    ctx: commands.Context[SignupBot],
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            "You need the **Manage Server** permission to use `!setup` or `!reset`."
        )
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("Use `!setup` or `!reset` in a server text channel.")
        return

    logger.error("Command failed: %s", error)
    await ctx.send("The signup panel could not be set up. Check the bot logs.")


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("Connected to Discord as %s (ID: %s)", bot.user, bot.user.id)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing DISCORD_BOT_TOKEN. Add your Discord bot token to Replit Secrets."
        )

    try:
        bot.run(token)
    except discord.LoginFailure as exc:
        raise SystemExit(
            "Discord rejected DISCORD_BOT_TOKEN. Check the token in Replit Secrets."
        ) from exc
    except discord.PrivilegedIntentsRequired as exc:
        raise SystemExit(
            "Discord requires Message Content Intent for the !setup command. "
            "Enable it in the Discord Developer Portal under your application's "
            "Bot settings, then restart the bot."
        ) from exc


if __name__ == "__main__":
    main()
