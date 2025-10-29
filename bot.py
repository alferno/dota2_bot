# bot.py
"""
Dota2 Tournament Bot (Python)
Features:
 - persistent data in MongoDB (players, queue, tournaments)
 - register players by mention (@user)
 - individual ELO, wins, losses
 - create tournaments with teams (teams are lists of discord user IDs)
 - start tournament: automatic pairing (byes when odd)
 - report match result: winner stays upper, loser to lower; automatic next-match creation when opponents ready
 - text-based bracket display
"""

import os
import math
import random
import asyncio
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
import motor.motor_asyncio
import discord
from discord.ext import commands
from discord.ui import View, Button, button
from discord import app_commands


load_dotenv()

# ---------- ENV ----------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID")) if os.getenv("DISCORD_GUILD_ID") else None
GUILD_ID = int(os.getenv("GUILD_ID"))


MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "dota2_bot")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID") else None

if not DISCORD_BOT_TOKEN or not MONGO_URI:
    raise RuntimeError("Please set DISCORD_BOT_TOKEN and MONGO_URI in .env")

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True        # required to read message content if you use it
intents.members = True                # required to resolve @mentions to Member objects
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---------- MongoDB ----------
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client[MONGO_DB_NAME]
players_col = db["players"]         # player documents
tournaments_col = db["tournaments"] # tournaments
queue_col = db["queue"]             # single document for queue

# ---------- Constants ----------
K_FACTOR = 32

# ---------- Utility helpers ----------

async def ensure_queue_doc():
    doc = await queue_col.find_one({"_id": "main"})
    if not doc:
        await queue_col.insert_one({"_id": "main", "queue": []})
        return {"queue": []}
    return doc

async def get_queue() -> List[int]:
    doc = await ensure_queue_doc()
    return doc.get("queue", [])

async def set_queue(q: List[int]):
    await queue_col.update_one({"_id": "main"}, {"$set": {"queue": q}}, upsert=True)

async def ensure_player_doc(discord_id: int, display_name: Optional[str] = None):
    doc = await players_col.find_one({"discord_id": discord_id})
    if not doc:
        base = {"discord_id": discord_id, "name": display_name or str(discord_id),
                "wins": 0, "losses": 0, "elo": 1200}
        await players_col.insert_one(base)
        return base
    # update name if provided
    if display_name and doc.get("name") != display_name:
        await players_col.update_one({"discord_id": discord_id}, {"$set": {"name": display_name}})
        doc["name"] = display_name
    return doc

async def get_player(discord_id: int) -> Dict[str, Any]:
    return await players_col.find_one({"discord_id": discord_id})

async def set_player(discord_id: int, update: Dict[str, Any]):
    await players_col.update_one({"discord_id": discord_id}, {"$set": update})

async def adjust_elo_for_match(winner_ids: List[int], loser_ids: List[int], k: int = K_FACTOR):
    """
    Update individual elo for each player.
    Use team-average method:
      team_rating = average(player.elo)
      expected_score = 1/(1+10^((opp - team)/400))
      Each player gets rating += K * (score - expected)
    """
    # fetch docs
    players = {}
    for pid in winner_ids + loser_ids:
        players[pid] = await ensure_player_doc(pid)

    win_avg = sum(players[p]["elo"] for p in winner_ids)/len(winner_ids)
    lose_avg = sum(players[p]["elo"] for p in loser_ids)/len(loser_ids)

    expected_win = 1 / (1 + 10 ** ((lose_avg - win_avg)/400))
    expected_lose = 1 - expected_win

    # winners -> score 1 , losers -> 0
    updates = []
    for pid in winner_ids:
        old = players[pid]["elo"]
        new = round(old + k * (1 - expected_win))
        updates.append((pid, new))
    for pid in loser_ids:
        old = players[pid]["elo"]
        new = round(old + k * (0 - expected_lose))
        updates.append((pid, new))

    # persist and increment wins/losses
    for pid, new_elo in updates:
        if pid in winner_ids:
            await players_col.update_one({"discord_id": pid}, {"$inc": {"wins": 1}, "$set": {"elo": new_elo}})
        else:
            await players_col.update_one({"discord_id": pid}, {"$inc": {"losses": 1}, "$set": {"elo": new_elo}})

async def elo_leaderboard(limit: int = 20) -> List[Dict[str, Any]]:
    cursor = players_col.find().sort("elo", -1).limit(limit)
    return [doc async for doc in cursor]

def team_display(team_players: List[int], players_cache: Dict[int, Dict[str, Any]] = None) -> str:
    if not team_players:
        return "No players"
    names = []
    for pid in team_players:
        if players_cache and pid in players_cache:
            names.append(players_cache[pid].get("name") or f"<@{pid}>")
        else:
            names.append(f"<@{pid}>")
    return ", ".join(names)

# ---------- Tournament helpers ----------
def make_match_id() -> str:
    return str(random.getrandbits(64))[:12]

def create_round_matches(team_ids: List[str]) -> List[Dict[str, Any]]:
    """Randomize and pair teams. Return list of match objects.
       If odd, last team gets bye (match with 'bye': True)
    """
    shuffled = team_ids[:]
    random.shuffle(shuffled)
    matches = []
    while len(shuffled) >= 2:
        a = shuffled.pop()
        b = shuffled.pop()
        matches.append({"id": make_match_id(), "teamA": a, "teamB": b, "winner": None, "bracket": "upper"})
    if shuffled:
        bye = shuffled.pop()
        # bye is an auto-advance; represent as a match with teamB = None
        matches.append({"id": make_match_id(), "teamA": bye, "teamB": None, "winner": bye, "bracket": "upper", "bye": True})
    return matches

async def create_tournament_doc(name: str) -> Dict[str, Any]:
    doc = {
        "name": name,
        "teams": {},   # teamName -> [playerIDs]
        "status": "setup",
        "pending_upper": [],  # team ids waiting to be paired for next upper round
        "pending_lower": [],  # team ids waiting to be paired for next lower round
        "matches": [],        # all matches (history + pending), match objects (see create_round_matches)
        "upper_rounds": [],   # list of rounds (each round list of match ids)
        "lower_rounds": [],
        "final": None         # final match id when set
    }
    await tournaments_col.insert_one(doc)
    return doc

async def get_tournament(name: str) -> Optional[Dict[str, Any]]:
    return await tournaments_col.find_one({"name": name})

async def update_tournament(name: str, update: Dict[str, Any]):
    await tournaments_col.update_one({"name": name}, {"$set": update})

async def append_match_to_tourney(name: str, match: Dict[str, Any]):
    await tournaments_col.update_one({"name": name}, {"$push": {"matches": match}})

async def push_round(name: str, bracket: str, round_match_ids: List[str]):
    field = "upper_rounds" if bracket == "upper" else "lower_rounds"
    await tournaments_col.update_one({"name": name}, {"$push": {field: round_match_ids}})

async def find_match(tourney: Dict[str, Any], match_id: str) -> Optional[Dict[str, Any]]:
    for m in tourney.get("matches", []):
        if m["id"] == match_id:
            return m
    return None

# pairing helpers (when winners are available)
async def try_pair_pending(tourney_name: str):
    """Try to pair pending_upper into matches and pending_lower into matches.
       Create matches whenever >=2 pending teams exist. If odd, last one gets bye.
    """
    tourney = await get_tournament(tourney_name)
    if not tourney:
        return

    updated = False

    # UPPER bracket
    pu = tourney.get("pending_upper", [])
    while len(pu) >= 2:
        a = pu.pop(0); b = pu.pop(0)
        m = {"id": make_match_id(), "teamA": a, "teamB": b, "winner": None, "bracket": "upper"}
        await append_match_to_tourney(tourney_name, m)
        # append match id to latest upper_round or create a new round
        await push_round(tourney_name, "upper", [m["id"]])
        updated = True
    if len(pu) == 1:
        # give bye
        bye_team = pu.pop(0)
        m = {"id": make_match_id(), "teamA": bye_team, "teamB": None, "winner": bye_team, "bracket": "upper", "bye": True}
        await append_match_to_tourney(tourney_name, m)
        await push_round(tourney_name, "upper", [m["id"]])
        updated = True

    # LOWER bracket
    pl = tourney.get("pending_lower", [])
    while len(pl) >= 2:
        a = pl.pop(0); b = pl.pop(0)
        m = {"id": make_match_id(), "teamA": a, "teamB": b, "winner": None, "bracket": "lower"}
        await append_match_to_tourney(tourney_name, m)
        await push_round(tourney_name, "lower", [m["id"]])
        updated = True
    if len(pl) == 1:
        # give bye in lower
        bye_team = pl.pop(0)
        m = {"id": make_match_id(), "teamA": bye_team, "teamB": None, "winner": bye_team, "bracket": "lower", "bye": True}
        await append_match_to_tourney(tourney_name, m)
        await push_round(tourney_name, "lower", [m["id"]])
        updated = True

    if updated:
        # store modified pendings
        await tournaments_col.update_one({"name": tourney_name}, {"$set": {"pending_upper": pu, "pending_lower": pl}})

    # After pairing, check for terminal condition: only one team remains overall
    t = await get_tournament(tourney_name)
    # Determine alive teams: any team that has not been eliminated (we'll track teams by existence in pending pools or winners)
    # Simpler approach: check all teams vs losers recorded in match history
    all_team_names = list(t.get("teams", {}).keys())
    eliminated = set()
    for m in t.get("matches", []):
        if m.get("bracket") == "lower" and m.get("winner") is not None and not m.get("bye"):
            # the loser of lower bracket matches is eliminated
            loser = m["teamA"] if m["teamB"] == m["winner"] else m["teamB"]
            # If loser is not None
            if loser:
                eliminated.add(loser)
        elif m.get("bracket") == "upper" and m.get("winner") is not None and m.get("teamB") is None and m.get("bye"):
            # bye auto-advance, no elimination
            pass
    alive = [tm for tm in all_team_names if tm not in eliminated]
    if len(alive) == 1:
        # declare champion
        champ = alive[0]
        await tournaments_col.update_one({"name": tourney_name}, {"$set": {"status": "finished", "champion": champ}})
        # finalization done
        return

# ---------- Commands ----------

# ---- Help
@bot.command(name="helpme")
async def helpme(ctx):
    msg = """
**Dota2 Tournament Bot - Commands**
(Use @mention for players)
**Registration / Profile**
!register @user      — register user (create profile)
!profile @user       — view player's stats (ELO/wins/losses)

**Queue**
!join               — join queue (uses your discord id)
!leave              — leave queue
!showqueue          — show queue

**Leaderboard**
!leaderboard        — show top players by ELO

**Tournament**
!createtourney NAME
!addteam NAME TEAMNAME @p1 @p2 ...   — add team with players (5 players typical)
!showteams NAME
!starttourney NAME                   — start tournament (pairs teams into upper matches; auto-byes)
!showbracket NAME                    — show current bracket/text tree
!matchresult NAME MATCH_ID @winner_team_name_here @loser_team_name_here  — report result using exact team names
!setelo @user VALUE                  — (admin) set player's ELO
!resetelo @user                      — (admin) reset player's ELO to 1200
"""
    await ctx.send(msg)

# ---- Players
@bot.command(name="register")
async def register(ctx, member: discord.Member):
    await ensure_player_doc(member.id, member.display_name)
    await ctx.send(f"Registered {member.display_name} ({member.id})")

@bot.command(name="profile")
async def profile(ctx, member: discord.Member = None):
    member = member or ctx.author
    doc = await ensure_player_doc(member.id, member.display_name)
    await ctx.send(f"**{doc['name']}** — ELO: {doc['elo']} | Wins: {doc['wins']} | Losses: {doc['losses']}")

# ---- Queue

queue = []
current_queue_message = None  # Track the message showing the queue

@bot.tree.command(name="startqueue", description="Start a new queue with join/leave buttons")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def start_queue(interaction: discord.Interaction):
    global queue_message
    queue.clear()
    queue_message = None
    await interaction.response.send_message("Queue started below ⬇️", ephemeral=True)
    await update_queue_message(interaction)


class QueueView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="🎮 Join Queue", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: Button):
        user = interaction.user.display_name
        if user in queue:
            await interaction.response.send_message(f"⚠️ {user}, you're already in the queue.", ephemeral=True)
            return

        queue.append(user)
        await update_queue_message(interaction)
        
        if len(queue) == 10:
            await interaction.channel.send("✅ **10 players have joined! Lobby is ready to start!**")
            queue.clear()
            await asyncio.sleep(1)
            await interaction.channel.send(embed=create_queue_embed(), view=QueueView())

    @button(label="🚪 Leave Queue", style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: Button):
        user = interaction.user.display_name
        if user not in queue:
            await interaction.response.send_message(f"❌ {user}, you're not in the queue.", ephemeral=True)
            return

        queue.remove(user)
        await update_queue_message(interaction)


def create_queue_embed():
    embed = discord.Embed(
        title="🕹️ Dota 2 Lobby Queue",
        description="Click the buttons below to join or leave the current queue.",
        color=discord.Color.blurple()
    )
    if queue:
        embed.add_field(
            name=f"Current Players ({len(queue)}/10)",
            value="\n".join([f"{i+1}. {p}" for i, p in enumerate(queue)]),
            inline=False
        )
    else:
        embed.add_field(name="Current Players (0/10)", value="No one has joined yet.", inline=False)
    return embed


async def update_queue_message(interaction: discord.Interaction):
    """Update the current queue embed after any change."""
    global current_queue_message
    if current_queue_message:
        await current_queue_message.edit(embed=create_queue_embed(), view=QueueView())
    else:
        current_queue_message = await interaction.channel.send(embed=create_queue_embed(), view=QueueView())


@bot.command(name="showqueue")
async def show_queue(ctx):
    """Show the interactive queue embed."""
    global current_queue_message
    queue.clear()  # start fresh each time
    embed = create_queue_embed()
    current_queue_message = await ctx.send(embed=embed, view=QueueView())


# @bot.command(name="join")
# async def join(ctx):
#     q = await get_queue()
#     uid = ctx.author.id
#     if uid in q:
#         await ctx.send("You're already in the queue.")
#         return
#     q.append(uid)
#     await set_queue(q)
#     await ensure_player_doc(uid, ctx.author.display_name)
#     await ctx.send(f"{ctx.author.display_name} joined the queue. ({len(q)} in queue)")

# @bot.command(name="leave")
# async def leave(ctx):
#     q = await get_queue()
#     uid = ctx.author.id
#     if uid not in q:
#         await ctx.send("You're not in the queue.")
#         return
#     q.remove(uid)
#     await set_queue(q)
#     await ctx.send(f"{ctx.author.display_name} left the queue. ({len(q)} left)")

# @bot.command(name="showqueue")
# async def showqueue(ctx):
#     q = await get_queue()
#     if not q:
#         await ctx.send("Queue is empty.")
#         return
#     # resolve names
#     names = []
#     for pid in q:
#         p = await get_player(pid)
#         if p:
#             names.append(f"{p.get('name')} (<@{pid}>)")
#         else:
#             names.append(f"<@{pid}>")
#     await ctx.send("**Queue:**\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)))

# ---- Leaderboard
@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx, limit: int = 10):
    docs = await elo_leaderboard(limit)
    lines = []
    i = 1
    for d in docs:
        lines.append(f"{i}. {d.get('name')} — ELO {d.get('elo')} | W {d.get('wins')} / L {d.get('losses')}")
        i += 1
    if not lines:
        await ctx.send("No players yet.")
    else:
        await ctx.send("**Leaderboard**\n" + "\n".join(lines))

@bot.command(name="setelo")
@commands.has_permissions(administrator=True)
async def setelo(ctx, member: discord.Member, value: int):
    await ensure_player_doc(member.id, member.display_name)
    await players_col.update_one({"discord_id": member.id}, {"$set": {"elo": int(value)}})
    await ctx.send(f"Set ELO for {member.display_name} to {value}")

@bot.command(name="resetelo")
@commands.has_permissions(administrator=True)
async def resetelo(ctx, member: discord.Member):
    await ensure_player_doc(member.id, member.display_name)
    await players_col.update_one({"discord_id": member.id}, {"$set": {"elo": 1200}})
    await ctx.send(f"Reset ELO for {member.display_name} to 1200")

# ---- Tournament commands
@bot.command(name="createtourney")
async def createtourney(ctx, name: str):
    existing = await get_tournament(name)
    if existing:
        await ctx.send("Tournament with this name already exists.")
        return
    await create_tournament_doc(name)
    await ctx.send(f"Tournament **{name}** created. Add teams with !addteam")

@bot.command(name="addteam")
async def addteam(ctx, tourney_name: str, team_name: str, *members: discord.Member):
    tourney = await get_tournament(tourney_name)
    if not tourney:
        await ctx.send("Tournament not found.")
        return
    player_ids = [m.id for m in members]
    # ensure players exist
    for m in members:
        await ensure_player_doc(m.id, m.display_name)
    # update teams map
    await tournaments_col.update_one({"name": tourney_name}, {"$set": {f"teams.{team_name}": player_ids}})
    await ctx.send(f"Added team **{team_name}** with {len(player_ids)} players to {tourney_name}")

@bot.command(name="showteams")
async def showteams(ctx, tourney_name: str):
    tourney = await get_tournament(tourney_name)
    if not tourney:
        await ctx.send("Tournament not found.")
        return
    teams = tourney.get("teams", {})
    if not teams:
        await ctx.send("No teams yet.")
        return
    players_cache = {}
    for pid in {pid for plist in teams.values() for pid in plist}:
        pdoc = await get_player(pid)
        if pdoc:
            players_cache[pid] = pdoc
    lines = []
    for t, pids in teams.items():
        lines.append(f"**{t}** — {team_display(pids, players_cache)}")
    await ctx.send("**Teams:**\n" + "\n".join(lines))

@bot.command(name="starttourney")
async def starttourney(ctx, tourney_name: str):
    tourney = await get_tournament(tourney_name)
    if not tourney:
        await ctx.send("Tournament not found.")
        return
    teams = list(tourney.get("teams", {}).keys())
    if len(teams) < 2:
        await ctx.send("Need at least 2 teams.")
        return
    # initial matches in upper bracket
    matches = create_round_matches(teams)
    # persist matches and initial pending lists
    for m in matches:
        await append_match_to_tourney(tourney_name, m)
    # set pending pools empty (we'll use matches to track winners)
    await tournaments_col.update_one({"name": tourney_name}, {"$set": {"status": "running", "pending_upper": [], "pending_lower": []}})
    # push this round ids into upper_rounds
    round_ids = [m["id"] for m in matches]
    await tournaments_col.update_one({"name": tourney_name}, {"$push": {"upper_rounds": {"$each": [round_ids]}}})
    # reply
    s = []
    for m in matches:
        if m.get("bye"):
            s.append(f"Bye: **{m['teamA']}** (auto-advance)")
        else:
            s.append(f"Match {m['id']}: **{m['teamA']}** vs **{m['teamB']}**")
    await ctx.send("Started tournament. Round 1 (Upper):\n" + "\n".join(s))

@bot.command(name="showbracket")
async def showbracket(ctx, tourney_name: str):
    tourney = await get_tournament(tourney_name)
    if not tourney:
        await ctx.send("Tournament not found.")
        return
    teams = tourney.get("teams", {})
    matches = tourney.get("matches", [])
    # build text tree: show latest upper rounds then lower
    lines = [f"**Tournament: {tourney_name}** — Status: {tourney.get('status','NA')}"]
    # Upper rounds
    lines.append("\n__Upper bracket matches (most recent first)__")
    upper_matches = [m for m in matches if m.get("bracket") == "upper"]
    if not upper_matches:
        lines.append("No upper matches yet.")
    else:
        for m in upper_matches:
            if m.get("bye"):
                lines.append(f"[{m['id']}] Bye → **{m['teamA']}** (auto)")
            else:
                lines.append(f"[{m['id']}] {m['teamA']} vs {m['teamB']} — Winner: {m.get('winner') or 'TBD'}")
    # Lower rounds
    lines.append("\n__Lower bracket matches__")
    lower_matches = [m for m in matches if m.get("bracket") == "lower"]
    if not lower_matches:
        lines.append("No lower matches yet.")
    else:
        for m in lower_matches:
            if m.get("bye"):
                lines.append(f"[{m['id']}] Bye → **{m['teamA']}** (auto)")
            else:
                lines.append(f"[{m['id']}] {m['teamA']} vs {m['teamB']} — Winner: {m.get('winner') or 'TBD'}")
    # final
    if tourney.get("champion"):
        lines.append(f"\n🏆 **Champion: {tourney.get('champion')}**")
    await ctx.send("\n".join(lines))

@bot.command(name="matchresult")
async def matchresult(ctx, tourney_name: str, match_id: str, winner_team: str, loser_team: str):
    tourney = await get_tournament(tourney_name)
    if not tourney:
        await ctx.send("Tournament not found.")
        return
    match = await find_match(tourney, match_id)
    if not match:
        await ctx.send("Match not found.")
        return
    if match.get("winner"):
        await ctx.send("This match already has a reported winner.")
        return
    # validate teams
    teams = tourney.get("teams", {})
    if winner_team not in teams or loser_team not in teams:
        await ctx.send("One or both team names not found in tournament (use exact team name).")
        return
    # set winner
    match["winner"] = winner_team
    # persist match update
    # update in DB: replace matches array item
    await tournaments_col.update_one(
        {"name": tourney_name, "matches.id": match_id},
        {"$set": {"matches.$.winner": winner_team}}
    )
    # move loser to pending_lower
    # get current pendings
    tdoc = await get_tournament(tourney_name)
    pend_lower = tdoc.get("pending_lower", [])
    pend_lower.append(loser_team)
    await tournaments_col.update_one({"name": tourney_name}, {"$set": {"pending_lower": pend_lower}})
    # winner goes to pending_upper (for next round)
    pend_upper = tdoc.get("pending_upper", [])
    pend_upper.append(winner_team)
    await tournaments_col.update_one({"name": tourney_name}, {"$set": {"pending_upper": pend_upper}})
    # adjust ELO and W/L for each player
    winner_pids = teams[winner_team]
    loser_pids = teams[loser_team]
    await adjust_elo_for_match(winner_pids, loser_pids)
    # Attempt to pair pending pools into matches
    await try_pair_pending(tourney_name)
    # re-fetch tourney to check champion
    updated = await get_tournament(tourney_name)
    if updated.get("status") == "finished":
        champ = updated.get("champion")
        # award champion — optionally give leaderboard points or announce in channel
        await ctx.send(f"🏆 Tournament finished! Champion: **{champ}**")
    else:
        await ctx.send(f"Result recorded: Winner **{winner_team}**; loser moved to lower bracket. Next matches may be created automatically.")

# ---------- Event handlers & start ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"🔁 Synced {len(synced)} command(s) with guild {GUILD_ID}")
    except Exception as e:
        print(f"❌ Sync failed: {e}")


# Run
bot.run(DISCORD_BOT_TOKEN)
