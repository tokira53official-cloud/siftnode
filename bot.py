import asyncio
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================
# VPS CLOUD DISCORD BOT
# Docker backend + logical NODE1..NODE20 on one local host.
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ADMIN_ID = 1520903362667876523
VOUCH_CHANNEL_ID = 1557023316038914209
PREFIX = "/"
OS_NAME = "Ubuntu 24.04 LTS"
DOCKER_IMAGE = "ubuntu:24.04"
VPS_EXPIRY_DAYS = 29
MAX_VPS_PER_USER = 3

# All logical nodes live on the same Docker/local host.
NODES = [f"NODE{i}" for i in range(1, 21)]

# /buy plans currently exposed to users. More logical nodes can be added later.
BUY_PLANS = {
    "NODE1-180Gb": {"node": "NODE1", "ram": 180, "cpu": 18, "disk": 180, "invites": 12},
    "NODE2-180Gb": {"node": "NODE2", "ram": 180, "cpu": 18, "disk": 180, "invites": 12},
    "NODE3-180Gb": {"node": "NODE3", "ram": 180, "cpu": 18, "disk": 180, "invites": 12},
    "NODE4-180Gb": {"node": "NODE4", "ram": 180, "cpu": 18, "disk": 180, "invites": 12},
    "NODE5-180Gb": {"node": "NODE5", "ram": 180, "cpu": 18, "disk": 180, "invites": 12},
}

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
VPS_FILE = DATA_DIR / "vps_data.json"
INVITES_FILE = DATA_DIR / "invites.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
CODES_FILE = DATA_DIR / "redeem_codes.json"
RENEW_FILE = DATA_DIR / "renew_rewards.json"
MESSAGE_FILE = DATA_DIR / "message_credits.json"
ADMINS_FILE = DATA_DIR / "admins.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("vps-cloud")


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Could not read %s: %s", path, exc)
    return default


vps_data = load_json(VPS_FILE, {})
invite_data = load_json(INVITES_FILE, {})
settings = load_json(SETTINGS_FILE, {"full_nodes": [], "ticketauto": False})
redeem_codes = load_json(CODES_FILE, {})
renew_rewards = load_json(RENEW_FILE, {})
message_credits = load_json(MESSAGE_FILE, {})
extra_admins = set(str(x) for x in load_json(ADMINS_FILE, []))


def save_all():
    VPS_FILE.write_text(json.dumps(vps_data, indent=2), encoding="utf-8")
    INVITES_FILE.write_text(json.dumps(invite_data, indent=2), encoding="utf-8")
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    CODES_FILE.write_text(json.dumps(redeem_codes, indent=2), encoding="utf-8")
    RENEW_FILE.write_text(json.dumps(renew_rewards, indent=2), encoding="utf-8")
    MESSAGE_FILE.write_text(json.dumps(message_credits, indent=2), encoding="utf-8")
    ADMINS_FILE.write_text(json.dumps(sorted(extra_admins)), encoding="utf-8")


def is_admin_id(user_id: int) -> bool:
    return user_id == ADMIN_ID or str(user_id) in extra_admins


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


def clean_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", code).upper()


def gen_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def short_container(container_id: str) -> str:
    return container_id[:12]


def user_vps(user_id: int):
    return vps_data.get(str(user_id), [])


def find_vps(container_id: str):
    needle = container_id.lower()
    for owner_id, items in vps_data.items():
        for item in items:
            cid = str(item.get("container_id", ""))
            name = str(item.get("container_name", ""))
            if needle in cid.lower() or needle == name.lower():
                return owner_id, item
    return None, None


def vps_expired(vps):
    expiry = parse_dt(vps.get("expires", ""))
    return bool(expiry and expiry <= now_utc())


def days_left(vps):
    expiry = parse_dt(vps.get("expires", ""))
    if not expiry:
        return 0
    seconds = max(0, int((expiry - now_utc()).total_seconds()))
    return seconds // 86400


def node_is_full(node: str) -> bool:
    return node.upper() in {str(x).upper() for x in settings.get("full_nodes", [])}


def embed(title: str, description: str = "", color: int = 0x5865F2):
    e = discord.Embed(title=title, description=description, color=color, timestamp=now_utc())
    return e


async def docker(*args, timeout=180, check=True):
    proc = await asyncio.create_subprocess_exec(
        "docker", *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("Docker command timed out.")
    stdout = out.decode(errors="replace").strip()
    stderr = err.decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise RuntimeError(stderr or stdout or "Docker command failed.")
    return stdout, stderr, proc.returncode


async def docker_exec(container: str, command: str, timeout=120):
    return await docker("exec", container, "bash", "-lc", command, timeout=timeout)


async def ensure_image():
    await docker("pull", DOCKER_IMAGE, timeout=600)


async def create_container(ram_gb: int, cpu: int, disk_gb: int, node: str):
    await ensure_image()
    name = "vps-" + secrets.token_hex(8)
    password = gen_password()

    # Docker resource limits. Storage quota is attempted where the active
    # Docker storage driver supports it; RAM/CPU are always applied.
    args = [
        "run", "-d", "--name", name,
        "--memory", f"{ram_gb}g",
        "--cpus", str(cpu),
        "--restart", "unless-stopped",
        DOCKER_IMAGE,
        "sleep", "infinity",
    ]
    try:
        await docker(*args, timeout=90)
    except RuntimeError as exc:
        # Some Docker storage drivers reject --storage-opt size. We do not
        # include it in the primary run because Docker's default overlay2
        # usually cannot enforce a per-container virtual disk quota.
        raise RuntimeError(f"Could not create Docker VPS: {exc}")

    setup = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq && "
        "apt-get install -y -qq openssh-server curl ca-certificates tmux tmate >/dev/null 2>&1; "
        "mkdir -p /run/sshd; "
        "echo 'root:%s' | chpasswd; "
        "sed -ri 's/^#?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
        "sed -ri 's/^#?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "/usr/sbin/sshd; "
        "curl -sSf https://sshx.io/get | sh -s run >/tmp/sshx_boot.log 2>&1 || true"
    ) % password
    try:
        await docker_exec(name, setup, timeout=300)
    except Exception:
        # Keep container usable even if optional package setup has a transient failure.
        log.exception("Initial package setup failed for %s", name)

    out, _, _ = await docker("inspect", "-f", "{{.Id}}", name, timeout=30)
    cid = out.strip()
    return name, cid, password


async def remove_container(vps):
    name = vps.get("container_name") or vps.get("container_id")
    if not name:
        return
    await docker("rm", "-f", name, timeout=90, check=False)


async def start_container(vps):
    name = vps.get("container_name")
    await docker("start", name, timeout=60)
    try:
        await docker_exec(name, "/usr/sbin/sshd || true", timeout=20)
    except Exception:
        pass
    vps["status"] = "Active"


async def stop_container(vps):
    name = vps.get("container_name")
    await docker("stop", name, timeout=60, check=False)
    vps["status"] = "Stopped"


async def get_tmate_route(vps):
    name = vps.get("container_name")
    try:
        await docker_exec(name, "pkill -f 'tmate -F' || true", timeout=15)
        command = "tmate -F -n /tmp/tmate.sock new-session -d 2>/dev/null; sleep 2; tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'"
        out, err, code = await docker_exec(name, command, timeout=45)
        route = out.strip().splitlines()[-1] if out.strip() else "Failed to extract tmate route"
        if not route.startswith("ssh "):
            route = "Failed to extract tmate route"
        return route
    except Exception:
        return "Failed to extract tmate route"


async def get_sshx_link(vps):
    name = vps.get("container_name")
    try:
        # sshx normally prints its public URL to stdout. Run it in a detached
        # shell and capture a short period of output.
        command = "pkill -f 'sshx' || true; (curl -sSf https://sshx.io/get | sh -s run)"
        out, err, code = await docker_exec(name, command, timeout=35)
        text = (out + "\n" + err).strip()
        match = re.search(r"https://sshx\.io/[A-Za-z0-9_-]+", text)
        return match.group(0) if match else "GENRATED_SSHX_HERE"
    except Exception:
        return "GENRATED_SSHX_HERE"


async def connection_details(vps):
    if str(vps.get("status", "")).lower() != "active":
        return "Failed to extract tmate route", "GENRATED_SSHX_HERE"
    return await get_tmate_route(vps), await get_sshx_link(vps)


def credentials_embed(vps, title="🚀 Your VPS Engine Booted"):
    e = embed(title, color=0x57F287)
    e.add_field(name="📦 Instance ID", value=f"`{short_container(vps['container_id'])}`", inline=False)
    e.add_field(name="🌐 Cluster Node", value=f"`{vps['node']}`", inline=False)
    return e


async def send_credentials(user: discord.User | discord.Member, vps, title="🚀 Your VPS Engine Booted"):
    tmate, sshx = await connection_details(vps)
    e = credentials_embed(vps, title)
    e.add_field(name="🔑 tmate SSH Command", value=f"```bash\n{tmate}\n```", inline=False)
    e.add_field(name="🔗 sshx Web Link", value=f"```text\n{sshx}\n```", inline=False)
    try:
        await user.send(embed=e)
        return True
    except discord.Forbidden:
        return False


# --------------------------- Invite tracking ---------------------------

invite_cache = {}


async def refresh_invites(guild: discord.Guild):
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return
    invite_cache[guild.id] = {i.code: {"uses": i.uses or 0, "inviter": i.inviter.id if i.inviter else None} for i in invites}


async def update_invite_join(member: discord.Member):
    guild = member.guild
    before = invite_cache.get(guild.id, {})
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return
    after = {i.code: {"uses": i.uses or 0, "inviter": i.inviter.id if i.inviter else None} for i in invites}
    invite_cache[guild.id] = after
    used = None
    for code, info in after.items():
        old = before.get(code, {}).get("uses", 0)
        if info["uses"] > old:
            used = info
            break
    if not used or not used.get("inviter"):
        return
    inviter = str(used["inviter"])
    d = invite_data.setdefault(inviter, {"joins": 0, "left": 0, "fake": 0, "rejoins": 0, "net": 0, "invited_users": []})
    d["joins"] = int(d.get("joins", 0)) + 1
    d["net"] = max(0, int(d.get("joins", 0)) - int(d.get("left", 0)) + int(d.get("rejoins", 0)))
    d.setdefault("invited_users", []).append(str(member.id))
    save_all()


# --------------------------- Bot ---------------------------

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        log.exception("Slash command sync failed")
    for guild in bot.guilds:
        await refresh_invites(guild)
    try:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.CustomActivity(name=f"🖥️ {running_count()} VPS Instances Running"),
        )
    except Exception:
        pass
    if not expiry_loop.is_running():
        expiry_loop.start()
    log.info("Logged in as %s", bot.user)


def running_count():
    total = 0
    for items in vps_data.values():
        total += sum(1 for v in items if str(v.get("status", "")).lower() == "active" and not vps_expired(v))
    return total


@tasks.loop(minutes=5)
async def expiry_loop():
    changed = False
    for owner_id, items in list(vps_data.items()):
        for vps in items:
            if vps_expired(vps) and not vps.get("expired_handled"):
                await stop_container(vps)
                vps["expired_handled"] = True
                vps["status"] = "Expired"
                changed = True
    if changed:
        save_all()
    try:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.CustomActivity(name=f"🖥️ {running_count()} VPS Instances Running"),
        )
    except Exception:
        pass


@bot.event
async def on_member_join(member):
    await update_invite_join(member)


@bot.event
async def on_member_remove(member):
    # Best-effort left tracking using the inviter history saved by the bot.
    uid = str(member.id)
    for inviter, d in invite_data.items():
        if uid in d.get("invited_users", []):
            d["left"] = int(d.get("left", 0)) + 1
            d["net"] = max(0, int(d.get("joins", 0)) - int(d.get("left", 0)) + int(d.get("rejoins", 0)))
            save_all()
            break


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Message-credit accounting.
    uid = str(message.author.id)
    message_credits[uid] = int(message_credits.get(uid, 0)) + 1

    # Vouch detector. It only records claims for users who actually have an
    # awaiting /deploy VPS, so random messages cannot unlock anything.
    if message.channel.id == VOUCH_CHANNEL_ID:
        normalized = re.sub(r"\s+", " ", message.content.strip().lower())
        m = re.fullmatch(r"legit got (\d+)gb vps", normalized)
        if m:
            requested_gb = int(m.group(1))
            awaiting = []
            for v in user_vps(message.author.id):
                if v.get("awaiting_vouch") and int(v.get("ram", 0)) == requested_gb:
                    awaiting.append(v)
            if not awaiting:
                await message.channel.send(f"❌ {message.author.mention} has no awaiting `{requested_gb}GB` VPS claim.")
            else:
                for v in awaiting:
                    v["awaiting_vouch"] = False
                    v["vouched"] = True
                    v["vouched_at"] = iso(now_utc())
                save_all()
                await message.channel.send(
                    f"✅ {message.author.mention} vouch recorded for `{len(awaiting)}` VPS instance(s). "
                    f"You can now use `/start <container_id>`."
                )
                for v in awaiting:
                    try:
                        await message.author.send(
                            embed=embed(
                                "🚀 Your VPS Provisioning Complete",
                                f"Your VPS `{short_container(v['container_id'])}` has passed the vouch step.\n\n"
                                f"🌐 Node: `{v['node']}`\n\n"
                                f"Use `/start {short_container(v['container_id'])}` to boot it and receive fresh tmate/sshx credentials."
                            , 0x57F287)
                        )
                    except discord.Forbidden:
                        pass

    save_all()
    await bot.process_commands(message)


# --------------------------- User commands ---------------------------

@bot.tree.command(name="help", description="Show VPS Cloud Management Dashboard commands")
async def help_command(interaction: discord.Interaction):
    e = embed("✨ VPS Cloud Management Dashboard", "Professional command reference and quick actions.")
    e.add_field(name="🚀 User Commands", value=(
        "`/buy` — Choose a VPS plan and deploy after invite eligibility.\n"
        "`/list` — Show your VPS instances with control buttons.\n"
        "`/start <id>` — Power on a container.\n"
        "`/stop <id>` — Stop a running container.\n"
        "`/regen_ssh <id>` — Generate fresh tmate/sshx routes.\n"
        "`/rebuild <id> <os>` — Re-image the VPS and reset expiry to 29 days.\n"
        "`/renew <id> <code>` — Extend expiry with a redeem code.\n"
        "`/renewvps` or `-renew` — Open invite renewal rewards.\n"
        "`/message` / `/m` — Show message credits.\n"
        "`/claimmessage` / `-claimmessage` — Claim a message-credit plan.\n"
        "`/i` — Show invite statistics.\n"
        "`/sharevps <id> <user>` — Transfer VPS ownership.\n"
        "Before a deployed 64GB VPS can start, vouch in the configured vouch channel."
    ), inline=False)
    if is_admin_id(interaction.user.id):
        e.add_field(name="👑 Admin Commands", value=(
            "`/deploy <user> <os> <node>` — Provision 64GB/16C/80GB VPS.\n"
            "`/clearnode <node>` — Remove all VPS containers on a node.\n"
            "`/usercleanvps <user>` — Remove all VPSes owned by a user.\n"
            "`/redeemcreate <days> [code]` — Create a redeem code.\n"
            "`/renewadd <plan> <invites> [days]` — Configure renewal rewards.\n"
            "`/giveaway <time> <reward> <winners>` — Start a giveaway.\n"
            "`/invitesadd <user> <amount>` / `/invitesremove` — Adjust invite score.\n"
            "`/invitereseteveryone` — Reset invite scores.\n"
            "`/messageadd` / `/messageremove` / `/messagereset` — Manage credits.\n"
            "`/planmessageadd <plan> <specs> <messages>` — Configure message plan.\n"
            "`/ticketauto [enabled]` — Toggle ticket auto mode.\n"
            "`/node_full <node>` — Mark a node full or reopen it.\n"
            "`/admin_add <user>` / `/admin_remove <user>` — Manage admins."
        ), inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="buy", description="Choose a VPS plan")
async def buy(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=embed("🛒 VPS Purchase Panel", "Choose a plan from the database below. Each plan checks your custom invite score before deployment."),
        view=BuyPlanView(interaction.user.id), ephemeral=True
    )


class BuyPlanSelect(discord.ui.Select):
    def __init__(self, owner_id):
        self.owner_id = owner_id
        options = [discord.SelectOption(label=name, value=name, description=f"{p['node']} • {p['disk']}GB • {p['invites']} invites") for name, p in BUY_PLANS.items()]
        super().__init__(placeholder="Choose a VPS plan", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This purchase menu belongs to another user.", ephemeral=True)
            return
        plan_name = self.values[0]
        plan = BUY_PLANS[plan_name]
        current = len(user_vps(interaction.user.id))
        if current >= MAX_VPS_PER_USER:
            await interaction.response.send_message(f"❌ You already have the maximum allowed VPS instances (3).", ephemeral=True)
            return
        score = int(invite_data.get(str(interaction.user.id), {}).get("net", 0))
        if score < plan["invites"]:
            e = embed("❌ VPS Purchase Failed", color=0xED4245)
            e.add_field(name="User", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            e.add_field(name="Plan", value=f"`{plan_name}`", inline=True)
            e.add_field(name="Storage", value=f"`{plan['disk']}GB`", inline=True)
            e.add_field(name="Invite Requirement", value=f"`{plan['invites']} invites required`", inline=True)
            e.add_field(name="Your Invites", value=f"`{score}`", inline=True)
            e.add_field(name="Result", value="`Deployment canceled: not eligible`", inline=False)
            await interaction.response.send_message(embed=e, ephemeral=True)
            await interaction.followup.send(f"❌ You need at least {plan['invites']} invites to buy {plan_name}. Deployment was cancelled.", ephemeral=True)
            return
        if node_is_full(plan["node"]):
            await interaction.response.send_message(f"❌ {plan['node']} is full. Please try again later or ask staff for another node.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            name, cid, password = await create_container(plan["ram"], plan["cpu"], plan["disk"], plan["node"])
            expiry = now_utc() + timedelta(days=VPS_EXPIRY_DAYS)
            vps = {
                "container_name": name, "container_id": cid, "ram": plan["ram"], "cpu": plan["cpu"],
                "storage": plan["disk"], "node": plan["node"], "status": "Active",
                "created_at": iso(now_utc()), "expires": iso(expiry), "ssh_password": password,
                "owner_id": str(interaction.user.id), "plan": plan_name, "awaiting_vouch": False,
            }
            vps_data.setdefault(str(interaction.user.id), []).append(vps)
            save_all()
            await start_container(vps)
            save_all()
            await interaction.followup.send(f"✅ {plan_name} purchased successfully and deployed to `{plan['node']}`. A receipt was sent to your DMs.", ephemeral=True)
            await send_credentials(interaction.user, vps, "▶️ Your VPS Engine Booted")
        except Exception as exc:
            log.exception("Buy failed")
            await interaction.followup.send(f"❌ VPS deployment failed: `{str(exc)[:900]}`", ephemeral=True)


class BuyPlanView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=120)
        self.add_item(BuyPlanSelect(owner_id))


@bot.tree.command(name="deploy", description="Admin: provision a 64GB VPS for a user")
@app_commands.describe(user="VPS owner", os="Operating system", node="Logical cluster node")
@app_commands.choices(os=[app_commands.Choice(name=OS_NAME, value="ubuntu-24.04")])
@app_commands.choices(node=[app_commands.Choice(name=n, value=n) for n in NODES])
async def deploy(interaction: discord.Interaction, user: discord.Member, os: app_commands.Choice[str], node: app_commands.Choice[str]):
    if not is_admin_id(interaction.user.id):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return
    if len(user_vps(user.id)) >= MAX_VPS_PER_USER:
        await interaction.response.send_message("❌ This user already has the maximum allowed VPS instances (3).", ephemeral=True)
        return
    node_name = node.value
    if node_is_full(node_name):
        await interaction.response.send_message(f"❌ {node_name} is full. Please try again later or ask staff for another node.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        name, cid, password = await create_container(64, 16, 80, node_name)
        await stop_container({"container_name": name, "status": "Stopped"})
        vps = {
            "container_name": name, "container_id": cid, "ram": 64, "cpu": 16, "storage": 80,
            "node": node_name, "status": "Stopped", "created_at": iso(now_utc()),
            "expires": iso(now_utc() + timedelta(days=VPS_EXPIRY_DAYS)), "ssh_password": password,
            "owner_id": str(user.id), "plan": "Admin Deploy 64GB", "awaiting_vouch": True,
        }
        vps_data.setdefault(str(user.id), []).append(vps)
        save_all()
        await interaction.followup.send(f"✅ 64GB VPS deployed for {user.mention} on `{node_name}` and placed in vouch-pending state.", ephemeral=True)
        try:
            await user.send(embed=embed(
                "🗣️ Vouch Required Before VPS Start",
                f"Your VPS (`{short_container(cid)}`) is ready, but it is stopped until you vouch.\n\n"
                f"Send `Legit got 64gb vps` in <#{VOUCH_CHANNEL_ID}>.\n"
                f"After you vouch, use `/start {short_container(cid)}`.", 0xFEE75C
            ))
        except discord.Forbidden:
            await interaction.followup.send(f"⚠️ I could not DM {user.mention}. Enable DMs to receive VPS details.", ephemeral=True)
    except Exception as exc:
        log.exception("Deploy failed")
        await interaction.followup.send(f"❌ VPS deployment failed: `{str(exc)[:900]}`", ephemeral=True)


@bot.tree.command(name="list", description="Show your VPS instances")
async def list_vps(interaction: discord.Interaction):
    items = user_vps(interaction.user.id)
    if not items:
        await interaction.response.send_message("📦 You do not have any VPS instances.", ephemeral=True)
        return
    e = embed("📦 Your VPS Instances", "Use the buttons below to control your VPS.")
    for v in items:
        status = "Expired" if vps_expired(v) else v.get("status", "Unknown")
        e.add_field(name=f"📦 Container `{short_container(v['container_id'])}`", value=(
            f"🌐 Cluster Node\n`{v['node']}`\n\n"
            f"🛠️ Status\n`{status}`\n\n"
            f"📅 Expiry\n`{v.get('expires', 'Unknown')}` ({days_left(v)}d left)"
        ), inline=False)
    await interaction.response.send_message(embed=e, view=ListView(interaction.user.id, items), ephemeral=True)


class ListView(discord.ui.View):
    def __init__(self, owner_id, items):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        for v in items[:25]:
            self.add_item(VPSButton(owner_id, short_container(v["container_id"]), "start", "Start", discord.ButtonStyle.success))
            self.add_item(VPSButton(owner_id, short_container(v["container_id"]), "stop", "Stop", discord.ButtonStyle.secondary))
            self.add_item(VPSButton(owner_id, short_container(v["container_id"]), "ssh", "SSH", discord.ButtonStyle.primary))
            break  # Discord rows/components have hard limits; command form remains available for all IDs.


class VPSButton(discord.ui.Button):
    def __init__(self, owner_id, cid, action, label, style):
        self.owner_id = owner_id
        self.cid = cid
        self.action_name = action
        super().__init__(label=label, style=style, custom_id=f"vps:{action}:{cid}")

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id and not is_admin_id(interaction.user.id):
            await interaction.response.send_message("❌ This is not your VPS.", ephemeral=True)
            return
        owner, vps = find_vps(self.cid)
        if not vps:
            await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
            return
        if self.action_name == "start":
            if vps_expired(vps):
                await interaction.response.send_message("❌ This VPS has expired.", ephemeral=True)
                return
            if vps.get("awaiting_vouch"):
                await interaction.response.send_message("❌ Vouch first in the configured vouch channel.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            await start_container(vps)
            save_all()
            await send_credentials(interaction.user if owner == str(interaction.user.id) else await bot.fetch_user(int(owner)), vps)
            await interaction.followup.send("▶️ VPS started and credentials were sent to your DMs.", ephemeral=True)
        elif self.action_name == "stop":
            await interaction.response.defer(ephemeral=True)
            await stop_container(vps)
            save_all()
            await interaction.followup.send("⏹️ VPS stopped.", ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True)
            ok = await send_credentials(interaction.user, vps, "🔑 VPS SSH Credentials")
            await interaction.followup.send("✅ Fresh SSH credentials sent to your DMs." if ok else "❌ DMs are disabled.", ephemeral=True)


@bot.tree.command(name="start", description="Power on a VPS")
@app_commands.describe(container_id="Container ID")
async def start(interaction: discord.Interaction, container_id: str):
    owner, vps = find_vps(container_id)
    if not vps or (owner != str(interaction.user.id) and not is_admin_id(interaction.user.id)):
        await interaction.response.send_message("❌ VPS not found or you do not own it.", ephemeral=True)
        return
    if vps_expired(vps):
        await interaction.response.send_message("❌ This VPS has expired.", ephemeral=True)
        return
    if vps.get("awaiting_vouch"):
        await interaction.response.send_message("❌ Vouch first in the configured vouch channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await start_container(vps)
        save_all()
        recipient = interaction.user if owner == str(interaction.user.id) else await bot.fetch_user(int(owner))
        await send_credentials(recipient, vps)
        await interaction.followup.send("▶️ Your VPS Engine Booted. Credentials sent to your DMs.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Start failed: `{str(exc)[:900]}`", ephemeral=True)


@bot.tree.command(name="stop", description="Stop a VPS")
@app_commands.describe(container_id="Container ID")
async def stop(interaction: discord.Interaction, container_id: str):
    owner, vps = find_vps(container_id)
    if not vps or (owner != str(interaction.user.id) and not is_admin_id(interaction.user.id)):
        await interaction.response.send_message("❌ VPS not found or you do not own it.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await stop_container(vps)
    save_all()
    await interaction.followup.send(f"⏹️ VPS `{short_container(vps['container_id'])}` stopped.", ephemeral=True)


@bot.tree.command(name="regen_ssh", description="Rotate tmate and sshx routes")
@app_commands.describe(container_id="Container ID")
async def regen_ssh(interaction: discord.Interaction, container_id: str):
    owner, vps = find_vps(container_id)
    if not vps or (owner != str(interaction.user.id) and not is_admin_id(interaction.user.id)):
        await interaction.response.send_message("❌ VPS not found or you do not own it.", ephemeral=True)
        return
    if str(vps.get("status", "")).lower() != "active":
        await interaction.response.send_message("❌ Start the VPS before regenerating SSH routes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok = await send_credentials(interaction.user if owner == str(interaction.user.id) else await bot.fetch_user(int(owner)), vps, "🔑 New VPS SSH Credentials")
    await interaction.followup.send("✅ New tmate/sshx credentials sent to your DMs." if ok else "❌ DMs are disabled.", ephemeral=True)


@bot.tree.command(name="rebuild", description="Rebuild a VPS with Ubuntu 24.04 LTS")
@app_commands.describe(container_id="Container ID", os="Operating system")
@app_commands.choices(os=[app_commands.Choice(name=OS_NAME, value="ubuntu-24.04")])
async def rebuild(interaction: discord.Interaction, container_id: str, os: app_commands.Choice[str]):
    owner, vps = find_vps(container_id)
    if not vps or owner != str(interaction.user.id):
        await interaction.response.send_message("❌ VPS not found or you do not own it.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    progress = await interaction.followup.send("🔄 VPS Rebuild Progress\n`[█░░░░░░░░░]` 10%", ephemeral=True, wait=True)
    try:
        await remove_container(vps)
        await progress.edit(content="🔄 VPS Rebuild Progress\n`[█████░░░░░]` 50%")
        ram, cpu, disk, node = int(vps["ram"]), int(vps["cpu"]), int(vps["storage"]), vps["node"]
        name, cid, password = await create_container(ram, cpu, disk, node)
        expiry = now_utc() + timedelta(days=VPS_EXPIRY_DAYS)
        vps.update({"container_name": name, "container_id": cid, "ssh_password": password, "status": "Active", "created_at": iso(now_utc()), "expires": iso(expiry), "awaiting_vouch": False, "expired_handled": False})
        save_all()
        await progress.edit(content="🔄 VPS Rebuild Progress\n`[▓▓▓▓▓▓▓▓▓▓]` 100%\n\n🔄 VPS Rebuild Process Finished!\n\n👤 Owner Tag\n" + interaction.user.mention + f"\n\n📦 New Container ID\n`{short_container(cid)}`\n\n🌐 Cluster Node\n`{node}`\n\n🔒 New connection credentials (tmate & sshx) sent directly to your DMs.")
        await send_credentials(interaction.user, vps, "🔄 Your VPS Rebuild Complete")
    except Exception as exc:
        await progress.edit(content=f"❌ VPS Rebuild Failed\n`{str(exc)[:900]}`")


@bot.tree.command(name="sharevps", description="Transfer VPS ownership to another user")
@app_commands.describe(container_id="Container ID", user="New owner")
async def sharevps(interaction: discord.Interaction, container_id: str, user: discord.Member):
    owner, vps = find_vps(container_id)
    if not vps or owner != str(interaction.user.id):
        await interaction.response.send_message("❌ VPS not found or you do not own it.", ephemeral=True)
        return
    if len(user_vps(user.id)) >= MAX_VPS_PER_USER:
        await interaction.response.send_message("❌ The target user already has the maximum allowed VPS instances (3).", ephemeral=True)
        return
    vps_data[owner].remove(vps)
    vps["owner_id"] = str(user.id)
    vps_data.setdefault(str(user.id), []).append(vps)
    save_all()
    e = embed("✅ VPS Shared Successfully", color=0x57F287)
    e.add_field(name="From", value=interaction.user.mention, inline=True)
    e.add_field(name="To", value=user.mention, inline=True)
    e.add_field(name="Container ID", value=f"`{short_container(vps['container_id'])}`", inline=False)
    e.add_field(name="Status", value="`Ownership transferred`", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=False)
    try:
        await user.send(embed=e)
    except discord.Forbidden:
        pass


@bot.tree.command(name="i", description="Show server invite statistics")
async def invite_stats(interaction: discord.Interaction):
    d = invite_data.get(str(interaction.user.id), {"joins": 0, "left": 0, "fake": 0, "rejoins": 0, "net": 0})
    e = embed("📊 Server Invite Statistics")
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.add_field(name="👤 User", value=f"**{interaction.user}**", inline=False)
    e.add_field(name="Net Invites", value=f"**{int(d.get('net', 0))}**", inline=False)
    e.add_field(name="📥 Joins", value=f"`{int(d.get('joins', 0))}`", inline=True)
    e.add_field(name="📤 Left", value=f"`{int(d.get('left', 0))}`", inline=True)
    e.add_field(name="⚠️ Fake", value=f"`{int(d.get('fake', 0))}`", inline=True)
    e.add_field(name="🔄 Rejoins", value=f"`{int(d.get('rejoins', 0))}`", inline=True)
    e.add_field(name="Invited By", value="Tracked from invite events", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="renew", description="Redeem a renewal code")
async def renew(interaction: discord.Interaction, container_id: str, code: str):
    owner, vps = find_vps(container_id)
    if not vps or owner != str(interaction.user.id):
        await interaction.response.send_message("❌ VPS not found or you do not own it.", ephemeral=True)
        return
    key = clean_code(code)
    if key not in redeem_codes:
        await interaction.response.send_message("❌ Invalid or expired redeem code.", ephemeral=True)
        return
    days = int(redeem_codes[key].get("days", 0))
    expiry = parse_dt(vps.get("expires", "")) or now_utc()
    base = max(expiry, now_utc())
    vps["expires"] = iso(base + timedelta(days=days))
    del redeem_codes[key]
    vps["expired_handled"] = False
    if str(vps.get("status")).lower() == "expired":
        vps["status"] = "Stopped"
    save_all()
    await interaction.response.send_message(f"✅ VPS `{short_container(vps['container_id'])}` renewed for `{days}` days.", ephemeral=True)


@bot.tree.command(name="renewvps", description="Show invite renewal rewards")
async def renewvps(interaction: discord.Interaction):
    if not renew_rewards:
        await interaction.response.send_message("📋 No invite renewal rewards are configured yet.", ephemeral=True)
        return
    e = embed("🔄 VPS Renewal Rewards")
    for plan, cfg in renew_rewards.items():
        e.add_field(name=plan, value=f"`{cfg.get('invites', 0)} invites` → `{cfg.get('days', 29)} days`", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="message", description="Show your message credits")
async def message(interaction: discord.Interaction):
    await interaction.response.send_message(f"💬 Your message credits: `{int(message_credits.get(str(interaction.user.id), 0))}`", ephemeral=True)


@bot.tree.command(name="m", description="Show your message credits")
async def m(interaction: discord.Interaction):
    await message.callback(interaction)


@bot.tree.command(name="claimmessage", description="Open message plan claim menu")
async def claimmessage(interaction: discord.Interaction):
    if not renew_rewards:
        await interaction.response.send_message("📋 No message plans are configured yet.", ephemeral=True)
        return
    await interaction.response.send_message("📋 Message plans are currently configured by staff. Use `/message` to see your credits.", ephemeral=True)


# --------------------------- Admin commands ---------------------------


def admin_only():
    async def predicate(interaction: discord.Interaction):
        return is_admin_id(interaction.user.id)
    return app_commands.check(predicate)


@bot.tree.command(name="clearnode", description="Admin: remove all VPS containers on a node")
@admin_only()
@app_commands.describe(node="Node to clear")
@app_commands.choices(node=[app_commands.Choice(name=n, value=n) for n in NODES])
async def clearnode(interaction: discord.Interaction, node: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    removed = 0
    for owner, items in list(vps_data.items()):
        keep = []
        for v in items:
            if v.get("node") == node.value:
                await remove_container(v)
                removed += 1
            else:
                keep.append(v)
        vps_data[owner] = keep
    save_all()
    await interaction.followup.send(f"✅ Cleared `{removed}` VPS instance(s) from `{node.value}`.", ephemeral=True)


@bot.tree.command(name="usercleanvps", description="Admin: remove all VPSes owned by a user")
@admin_only()
async def usercleanvps(interaction: discord.Interaction, user: discord.Member):
    items = user_vps(user.id)
    for v in items:
        await remove_container(v)
    vps_data[str(user.id)] = []
    save_all()
    await interaction.response.send_message(f"✅ Removed `{len(items)}` VPS instance(s) owned by {user.mention}.", ephemeral=True)


@bot.tree.command(name="redeemcreate", description="Admin: create a VPS renewal code")
@admin_only()
async def redeemcreate(interaction: discord.Interaction, days: int, code: str | None = None):
    if days <= 0:
        await interaction.response.send_message("❌ Days must be greater than zero.", ephemeral=True)
        return
    key = clean_code(code) if code else secrets.token_urlsafe(8).replace("-", "").replace("_", "").upper()
    redeem_codes[key] = {"days": days, "created_by": str(interaction.user.id), "created_at": iso(now_utc())}
    save_all()
    await interaction.response.send_message(f"✅ Redeem code created: `{key}` → `{days}` days.", ephemeral=True)


@bot.tree.command(name="renewadd", description="Admin: configure invite renewal reward")
@admin_only()
async def renewadd(interaction: discord.Interaction, plan: str, invites: int, days: int = 29):
    renew_rewards[plan] = {"invites": invites, "days": days}
    save_all()
    await interaction.response.send_message(f"✅ Renewal reward `{plan}` configured: `{invites}` invites → `{days}` days.", ephemeral=True)


@bot.tree.command(name="invitesadd", description="Admin: add invite score")
@admin_only()
async def invitesadd(interaction: discord.Interaction, user: discord.Member, amount: int):
    d = invite_data.setdefault(str(user.id), {"joins": 0, "left": 0, "fake": 0, "rejoins": 0, "net": 0})
    d["net"] = max(0, int(d.get("net", 0)) + amount)
    save_all()
    await interaction.response.send_message(f"✅ Added `{amount}` invite score to {user.mention}. New score: `{d['net']}`.", ephemeral=True)


@bot.tree.command(name="invitesremove", description="Admin: remove invite score")
@admin_only()
async def invitesremove(interaction: discord.Interaction, user: discord.Member, amount: int):
    d = invite_data.setdefault(str(user.id), {"joins": 0, "left": 0, "fake": 0, "rejoins": 0, "net": 0})
    d["net"] = max(0, int(d.get("net", 0)) - amount)
    save_all()
    await interaction.response.send_message(f"✅ Removed `{amount}` invite score from {user.mention}. New score: `{d['net']}`.", ephemeral=True)


@bot.tree.command(name="invitereseteveryone", description="Admin: reset all invite scores")
@admin_only()
async def invitereseteveryone(interaction: discord.Interaction):
    for d in invite_data.values():
        d["net"] = 0
    save_all()
    await interaction.response.send_message("✅ All custom invite scores reset to zero.", ephemeral=True)


@bot.tree.command(name="node_full", description="Admin: mark a node full / reopen it")
@admin_only()
@app_commands.choices(node=[app_commands.Choice(name=n, value=n) for n in NODES])
async def node_full(interaction: discord.Interaction, node: app_commands.Choice[str]):
    node_name = node.value
    full = settings.setdefault("full_nodes", [])
    existing = next((x for x in full if str(x).upper() == node_name), None)
    if existing:
        full.remove(existing)
        action = "reopened"
    else:
        full.append(node_name)
        action = "marked full"
    save_all()
    await interaction.response.send_message(f"✅ `{node_name}` {action}.", ephemeral=True)


@bot.tree.command(name="messageadd", description="Admin: add message credits")
@admin_only()
async def messageadd(interaction: discord.Interaction, user: discord.Member, amount: int):
    key = str(user.id)
    message_credits[key] = int(message_credits.get(key, 0)) + amount
    save_all()
    await interaction.response.send_message(f"✅ Added `{amount}` message credits to {user.mention}.", ephemeral=True)


@bot.tree.command(name="messageremove", description="Admin: remove message credits")
@admin_only()
async def messageremove(interaction: discord.Interaction, user: discord.Member, amount: int):
    key = str(user.id)
    message_credits[key] = max(0, int(message_credits.get(key, 0)) - amount)
    save_all()
    await interaction.response.send_message(f"✅ Removed `{amount}` message credits from {user.mention}.", ephemeral=True)


@bot.tree.command(name="messagereset", description="Admin: reset message credits")
@admin_only()
async def messagereset(interaction: discord.Interaction, user: discord.Member):
    message_credits[str(user.id)] = 0
    save_all()
    await interaction.response.send_message(f"✅ Reset message credits for {user.mention}.", ephemeral=True)


@bot.tree.command(name="planmessageadd", description="Admin: configure a message-credit plan")
@admin_only()
async def planmessageadd(interaction: discord.Interaction, plan: str, specs: str, messages: int):
    settings.setdefault("message_plans", {})[plan] = {"specs": specs, "messages": messages}
    save_all()
    await interaction.response.send_message(f"✅ Message plan `{plan}` configured: `{messages}` credits — {specs}", ephemeral=True)


@bot.tree.command(name="ticketauto", description="Admin: toggle invite-based ticket auto mode")
@admin_only()
async def ticketauto(interaction: discord.Interaction, enabled: bool | None = None):
    if enabled is None:
        enabled = not bool(settings.get("ticketauto", False))
    settings["ticketauto"] = enabled
    save_all()
    await interaction.response.send_message(f"✅ Ticket auto-deployment is now `{ 'enabled' if enabled else 'disabled' }`.", ephemeral=True)


@bot.tree.command(name="admin_add", description="Admin: add a bot admin")
@admin_only()
async def admin_add(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("❌ Only the main admin can manage bot admins.", ephemeral=True)
        return
    extra_admins.add(str(user.id))
    save_all()
    await interaction.response.send_message(f"✅ {user.mention} added as bot admin.", ephemeral=True)


@bot.tree.command(name="admin_remove", description="Admin: remove a bot admin")
@admin_only()
async def admin_remove(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("❌ Only the main admin can manage bot admins.", ephemeral=True)
        return
    extra_admins.discard(str(user.id))
    save_all()
    await interaction.response.send_message(f"✅ {user.mention} removed from bot admins.", ephemeral=True)


@bot.tree.command(name="giveaway", description="Admin: start a VPS giveaway")
@admin_only()
async def giveaway(interaction: discord.Interaction, time_text: str, reward: str, winners: int):
    await interaction.response.send_message(embed=embed("🎉 VPS Giveaway", f"Reward: `{reward}`\nWinners: `{winners}`\nDuration: `{time_text}`"), ephemeral=False)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "❌ You do not have permission to use this command."
    else:
        log.exception("Slash command error", exc_info=error)
        message = f"❌ Something went wrong: `{str(error)[:900]}`"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")
    bot.run(TOKEN)
