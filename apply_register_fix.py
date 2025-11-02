import re, sys, pathlib

p = pathlib.Path("frontline-pass.py")
src = p.read_text(encoding="utf-8")

changed = False

# 1) Ensure imports for logging/time exist (tolerant)
def ensure_import(name: str):
    global src, changed
    if re.search(rf'^\s*import\s+{re.escape(name)}\b', src, re.M):
        return
    # add after first import line
    m = re.search(r'^\s*import\s+\w[^\n]*$', src, re.M)
    if m:
        pos = m.end()
        src = src[:pos] + f"\nimport {name}" + src[pos:]
    else:
        src = f"import {name}\n" + src
    changed = True

ensure_import("logging")
ensure_import("time")

# 2) Insert _schedule_ephemeral_cleanup helper if missing
if "_schedule_ephemeral_cleanup" not in src:
    helper = '''
# --- helpers ---------------------------------------------------------------
def _schedule_ephemeral_cleanup(interaction, *, message=None, delay: float = 30.0) -> None:
    """
    Best-effort cleanup for ephemeral follow-ups. Safe if perms missing.
    """
    try:
        import asyncio
        async def _cleanup():
            try:
                await asyncio.sleep(delay)
                if message is not None:
                    await message.delete()
            except Exception:
                pass
        asyncio.create_task(_cleanup())
    except Exception:
        pass
'''.lstrip("\n")
    # place helper after imports block (after last from/ import)
    m = list(re.finditer(r'^(?:from\s+\w[^\n]*import[^\n]*|import\s+\w[^\n]*)\s*$', src, re.M))
    insert_at = m[-1].end() if m else 0
    src = src[:insert_at] + "\n" + helper + src[insert_at:]
    changed = True

# 3) Replace register_button method body (find by custom_id)
register_btn_re = re.compile(
    r'(@discord\.ui\.button\([^)]*custom_id\s*=\s*["\']frontline-pass-register["\'][^)]*\)\s*'
    r'async\s+def\s+register_button\s*\([\s\S]*?\):\s*[\s\S]*?)(?=\n\s*@|'
    r'\n\s*class\s|\Z)',
    re.M
)

new_register = r'''
@discord.ui.button(label="Register", style=ButtonStyle.danger, custom_id="frontline-pass-register")
async def register_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
    """Hardened handler with error trapping + UX for PlayerDirectory present."""
    try:
        discord_id = str(interaction.user.id)
        existing = self.database.fetch_player(discord_id)
        if existing:
            stored_name = self.database.fetch_player_name(discord_id)
            display = f"{existing} ({stored_name})" if stored_name else existing
            await interaction.response.send_message(
                f"You're already registered. Your T17 ID `{display}` is linked to your Discord account.",
                ephemeral=True,
            )
            _schedule_ephemeral_cleanup(interaction)
            return

        # If a PlayerDirectory is configured, guide to slash + manual fallback
        if getattr(self.bot, "player_directory", None):
            try:
                command = interaction.client.tree.get_command("register_player")
            except Exception:
                command = None
            command_mention = getattr(command, "mention", "/register_player")
            view = ManualEntryPromptView(self)
            await interaction.response.send_message(
                f"Use {command_mention} to search and select your player.\n"
                "Can't find it? Click the button below to enter your T17 ID manually.",
                view=view, ephemeral=True
            )
            _schedule_ephemeral_cleanup(interaction)
            return

        # No directory: open manual modal
        await interaction.response.send_modal(PlayerIDModal(self))

    except Exception:
        logging.exception("Register button failed")
        try:
            if interaction.response.is_done():
                msg = await interaction.followup.send(
                    "Registration failed due to an internal error. Mods have been notified.",
                    ephemeral=True, wait=True
                )
                _schedule_ephemeral_cleanup(interaction, message=msg)
            else:
                await interaction.response.send_message(
                    "Registration failed due to an internal error. Mods have been notified.",
                    ephemeral=True
                )
                _schedule_ephemeral_cleanup(interaction)
        except Exception:
            pass
'''.strip("\n")

m = register_btn_re.search(src)
if m:
    src = src[:m.start(1)] + new_register + src[m.end(1):]
    changed = True
else:
    print("WARN: could not locate register_button; leaving as-is", file=sys.stderr)

# 4) Wrap refresh_announcement_message and add slash commands in setup_hook
setup_re = re.compile(r'async\s+def\s+setup_hook\s*\(\s*self\s*\)\s*:\s*\n([\s\S]*?)(?=\n\s*async\s+def|\n\s*def|\Z)', re.M)
m = setup_re.search(src)
if m and "frontline_health" not in m.group(1):
    body = m.group(1)

    # Add try/except around refresh_announcement_message()
    body = re.sub(
        r'await\s+self\.refresh_announcement_message\(\s*\)',
        "try:\n            await self.refresh_announcement_message()\n"
        "        except Exception:\n"
        "            logging.exception(\"Failed to refresh existing announcement; use /frontline_reset_announcement\")",
        body
    )

    # Append slash commands
    body = body.rstrip() + "\n        self.tree.add_command(self.frontline_health)\n        self.tree.add_command(self.frontline_reset_announcement)\n"

    src = src[:m.start(1)] + body + src[m.end(1):]
    changed = True

# 5) Ensure slash command implementations exist
if "def frontline_health" not in src:
    health_cmd = r'''
    @app_commands.command(name="frontline_health", description="Report Frontline Pass status and wiring")
    async def frontline_health(self, interaction: discord.Interaction) -> None:
        try:
            view_ok = bool(getattr(self, "persistent_view", None))
            try:
                ann_id = self.database.get_metadata("announcement_message_id")
            except Exception:
                ann_id = None
            db_backend = getattr(self.database, "backend", "json")
            db_path = getattr(self.database, "path", getattr(self.database, "_path", "unknown"))
            await interaction.response.send_message(
                f"View attached: {view_ok}\n"
                f"DB backend: {db_backend} @ {db_path}\n"
                f"VIP hours: {self.vip_duration_hours:g}\n"
                f"Announcement ID: {ann_id}",
                ephemeral=True
            )
        except Exception:
            logging.exception("frontline_health failed")
            if interaction.response.is_done():
                await interaction.followup.send("Health check failed. See logs.", ephemeral=True)
            else:
                await interaction.response.send_message("Health check failed. See logs.", ephemeral=True)
'''.strip("\n")
    # append near end of class FrontlinePassBot (best-effort)
    bot_class = re.search(r'class\s+FrontlinePassBot\([^\)]*\)\s*:\s*\n', src)
    if bot_class:
        # append at end of class body (best-effort: before next class or EOF)
        class_block = re.search(r'(class\s+FrontlinePassBot[^\n]*\n)([\s\S]*?)(?=\nclass\s|\Z)', src)
        if class_block:
            before, body = class_block.group(1), class_block.group(2)
            body = body.rstrip() + "\n" + health_cmd + "\n"
            src = src[:class_block.start()] + before + body + src[class_block.end():]
            changed = True

if "def frontline_reset_announcement" not in src:
    reset_cmd = r'''
    @app_commands.command(name="frontline_reset_announcement", description="Repost control center with fresh buttons")
    async def frontline_reset_announcement(self, interaction: discord.Interaction) -> None:
        def _is_mod(user):
            return getattr(getattr(user, "guild_permissions", None), "manage_guild", False)
        if not _is_mod(interaction.user):
            await interaction.response.send_message("Insufficient permissions.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            try:
                await self.refresh_announcement_message(force_new=False)
                await interaction.followup.send("Announcement refreshed.", ephemeral=True)
            except Exception:
                logging.exception("Failed to edit existing announcement; posting a fresh one.")
                await self.refresh_announcement_message(force_new=True)
                await interaction.followup.send("Announcement re-posted with fresh components.", ephemeral=True)
        except Exception:
            logging.exception("frontline_reset_announcement failed")
            await interaction.followup.send("Reset failed. See logs.", ephemeral=True)
'''.strip("\n")
    class_block = re.search(r'(class\s+FrontlinePassBot[^\n]*\n)([\s\S]*?)(?=\nclass\s|\Z)', src)
    if class_block:
        before, body = class_block.group(1), class_block.group(2)
        body = body.rstrip() + "\n" + reset_cmd + "\n"
        src = src[:class_block.start()] + before + body + src[class_block.end():]
        changed = True

if changed:
    p.write_text(src, encoding="utf-8")
    print("Applied register-button hardening and admin cmds.")
else:
    print("No changes were necessary.")
