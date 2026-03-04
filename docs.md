# Poker Server — Full Documentation

A Texas Hold'em server that any number of bot clients can connect to over TCP.
All game logic lives on the server; bots only send actions and receive state.

---

## Table of Contents

1. [Files](#files)
2. ~~[Requirements](#requirements)~~
3. [Launching — Local (Same Machine)](#launching--local-same-machine)
4. [Launching — LAN (Same Network)](#launching--lan-same-network)
5. [Launching — Internet (Port Forwarding)](#launching--internet-port-forwarding)
6. [Launching — Internet (Cloud Server)](#launching--internet-cloud-server)
7. [Launching — Internet (ngrok, no router access)](#launching--internet-ngrok-no-router-access)
8. [Connecting a Bot](#connecting-a-bot)
9. [What the Server Tracks Per Player](#what-the-server-tracks-per-player)
10. [Game Configuration](#game-configuration)
11. [Message Protocol Reference](#message-protocol-reference)
12. [Writing Your Bot](#writing-your-bot)
13. [Troubleshooting](#troubleshooting)

---

## Files

| File | Purpose |
|---|---|
| `poker_server.py` | The table. Handles all game logic. Run once. |
| `bot.py` | Bot skeleton. Run one instance per player seat. Edit `decide()` only. |
| `launch.py` | Convenience script — starts server + N bots automatically on one machine. |


## Launching — Local (Same Machine)

Use this for development, testing bots against each other on your own computer.

**Start the server:**
```bash
python poker_server.py --host localhost --port 9999 --players 4 --chips 1000 --bb 20
```

**Start each bot** (in separate terminals, or use `launch.py`):
```bash
python bot.py --host localhost --port 9999 --name Bot0
python bot.py --host localhost --port 9999 --name Bot1
python bot.py --host localhost --port 9999 --name Bot2
python bot.py --host localhost --port 9999 --name Bot3
```

Or use the launcher to do all of the above in one command:
```bash
python launch.py --players 4 --chips 1000 --bb 20
```

The server waits until all `--players` bots have connected, then starts automatically.

---

## Launching — LAN (Same Network)

Use this when bots run on different machines on the same Wi-Fi or wired network
(e.g. multiple laptops in the same room).

**On the server machine, find your local IP:**
```bash
# macOS / Linux
ipconfig getifaddr en0      # Wi-Fi
ip route get 1 | awk '{print $7; exit}'   # Linux fallback

# Windows
ipconfig   # look for "IPv4 Address" under your active adapter
```
This gives you something like `192.168.1.42`.

**Start the server, binding to all interfaces:**
```bash
python poker_server.py --host 0.0.0.0 --port 9999 --players 4
```

**On each bot machine, connect using the server's local IP:**
```bash
python bot.py --host 192.168.1.42 --port 9999 --name BotA
```

No router configuration needed — LAN traffic is local.

---

## Launching — Internet (Port Forwarding)

Use this when the server runs on a home or office machine and bots connect
from anywhere in the world. Requires access to your router's admin panel.

### Step 1 — Find your machine's local IP
```bash
# macOS
ipconfig getifaddr en0

# Linux
hostname -I | awk '{print $1}'

# Windows
ipconfig
```
Example result: `192.168.1.42`

### Step 2 — Set up port forwarding on your router

1. Open your router admin panel — usually at `http://192.168.0.1` or `http://192.168.1.1`
2. Log in (check the sticker on your router for default credentials)
3. Find **Port Forwarding** (sometimes under "Advanced", "NAT", or "Virtual Server")
4. Create a new rule:

| Field | Value |
|---|---|
| External port | `9999` |
| Internal IP | `192.168.1.42` (your machine's local IP from Step 1) |
| Internal port | `9999` |
| Protocol | `TCP` |

5. Save and apply.

### Step 3 — Find your public IP

Go to [https://whatismyip.com](https://whatismyip.com) — note the address, e.g. `203.0.113.45`.

> **Important:** Home ISPs often change your public IP periodically (dynamic IP).
> If bots can't connect after a day or two, re-check your public IP.
> For a permanent address, consider a DDNS service like [No-IP](https://noip.com) (free).

### Step 4 — Start the server
```bash
python poker_server.py --host 0.0.0.0 --port 9999 --players 4
```

### Step 5 — Share with bot authors
Tell them to run:
```bash
python bot.py --host 203.0.113.45 --port 9999 --name TheirBotName
```

### Firewall note
If the server machine runs Windows Firewall or a Linux `ufw`/`iptables` firewall,
you also need to allow the port inbound on the machine itself:

```bash
# Linux (ufw)
sudo ufw allow 9999/tcp

# Linux (iptables)
sudo iptables -A INPUT -p tcp --dport 9999 -j ACCEPT

# Windows — run in PowerShell as Administrator
New-NetFirewallRule -DisplayName "Poker Server" -Direction Inbound -Protocol TCP -LocalPort 9999 -Action Allow
```

---

## Launching — Internet (Cloud Server)

The cleanest option for persistent or multi-day tournaments.
A $5/month VPS (DigitalOcean Droplet, AWS EC2 t3.micro, Hetzner CX11, etc.)
gives you a permanent public IP with no router to configure.

### Step 1 — Open the port in your cloud firewall

**DigitalOcean:** Networking → Firewalls → Inbound Rules → Add TCP port 9999

**AWS EC2:** EC2 → Security Groups → Inbound Rules → Add TCP port 9999, source 0.0.0.0/0

**Hetzner:** Firewall → Inbound → Add TCP 9999

**Google Cloud:** VPC Network → Firewall → Create Rule → TCP 9999, target all instances

### Step 2 — Upload the server file and run it

```bash
# Copy server to your VPS
scp poker_server.py user@YOUR_VPS_IP:~/

# SSH in and start it
ssh user@YOUR_VPS_IP
python3 poker_server.py --host 0.0.0.0 --port 9999 --players 4 --chips 1000 --bb 20
```

To keep it running after you close your SSH session:
```bash
nohup python3 poker_server.py --host 0.0.0.0 --port 9999 --players 4 &
# or with screen:
screen -S poker
python3 poker_server.py --host 0.0.0.0 --port 9999 --players 4
# Ctrl-A then D to detach
```

### Step 3 — Bots connect using the VPS public IP

```bash
python bot.py --host YOUR_VPS_IP --port 9999 --name MyBot
```

---

## Launching — Internet (ngrok, no router access)

Use this if you're on a network where you can't configure port forwarding
(university network, corporate Wi-Fi, someone else's router).
ngrok creates a public tunnel to your local server instantly.

### Step 1 — Install ngrok
Download from [https://ngrok.com/download](https://ngrok.com/download) or:
```bash
# macOS (Homebrew)
brew install ngrok

# Linux
snap install ngrok
```

Create a free account at ngrok.com and authenticate:
```bash
ngrok config add-authtoken YOUR_TOKEN
```

### Step 2 — Start your server normally
```bash
python poker_server.py --host 0.0.0.0 --port 9999 --players 4
```

### Step 3 — Open the tunnel in a separate terminal
```bash
ngrok tcp 9999
```

ngrok will print something like:
```
Forwarding  tcp://0.tcp.ngrok.io:12345 -> localhost:9999
```

### Step 4 — Share the ngrok address with bot authors
```bash
python bot.py --host 0.tcp.ngrok.io --port 12345 --name TheirBot
```

> **Note:** The ngrok address and port change every time you restart the tunnel
> on the free plan. Upgrade to ngrok Pro for a stable address.

---

## Connecting a Bot

Anyone who wants to connect their bot needs three things:

1. The server's **hostname or IP** (e.g. `203.0.113.45` or `0.tcp.ngrok.io`)
2. The **port** (default `9999`)
3. A copy of `bot.py` — they only edit the `decide()` function

```bash
python bot.py --host <SERVER_IP> --port <PORT> --name <BOTNAME>
```

The server will not start the game until **all seats are filled**. The number of
seats is set by `--players` when the server is launched. Everyone needs to be
connected before the first hand begins.

---

## What the Server Tracks Per Player

Yes — the server maintains full per-player state for every hand. Here is
everything tracked in real time:

### Per-player values (updated every action)

| Field | Description |
|---|---|
| `chips` | Current chip stack — decremented on bets/calls, incremented on wins |
| `bet` | Amount committed to the pot **this street only** — resets to 0 each new street |
| `total_bet` | Total chips committed **across the whole hand** |
| `folded` | Whether this player has folded this hand |
| `all_in` | Whether this player is all-in (chips == 0, still in the hand) |
| `hole` | The two private hole cards dealt to this player |

### Table-wide values (tracked by server each hand)

| Field | Description |
|---|---|
| `pot` | Running total of all chips committed to the pot this hand |
| `current_bet` | The highest bet level on the current street — what everyone must match |
| `big_blind` | The big blind amount (set at server launch, fixed for the session) |
| `small_blind` | Always `big_blind / 2` |
| `min_raise` | The minimum legal raise = `current_bet + max(current_bet, big_blind)` |
| `to_call` | How many chips this specific player needs to add to call |
| `community` | The shared board cards (0 preflop → 3 flop → 4 turn → 5 river) |
| `dealer` | Player ID of the current dealer button |

### Blind posting

Blinds are posted automatically before any action — you never need to handle them.
The server deducts chips and sets initial bets before dealing hole cards.
If a player has fewer chips than the blind, they post what they have and go all-in.

### Chip distribution at showdown

- Pot is split equally among tied winners
- If the pot doesn't divide evenly, the **first winner in seat order gets the odd chip**
- All-in players are handled correctly — a player can only win up to what they
  contributed × number of players in the pot (side pot logic)

---

## Game Configuration

All settings are passed to `poker_server.py` at launch time and are fixed for
the entire session.

| Flag | Default | Description |
|---|---|---|
| `--host` | `localhost` | Interface to bind. Use `0.0.0.0` for any external access. |
| `--port` | `9999` | TCP port. Must be open in firewall and/or forwarded on router. |
| `--players` | `4` | Number of seats. Server waits until all seats are filled before starting. |
| `--chips` | `1000` | Starting chip stack for every player. |
| `--bb` | `20` | Big blind size. Small blind is always `bb / 2`. |

**Example — 6-player tournament, deep stacks, small blinds:**
```bash
python poker_server.py --host 0.0.0.0 --port 9999 --players 6 --chips 5000 --bb 10
```

**Example — 2-player heads-up, fast:**
```bash
python poker_server.py --host 0.0.0.0 --port 9999 --players 2 --chips 500 --bb 50
```

---

## Message Protocol Reference

All messages are **newline-delimited JSON** (`\n` terminated) over a plain TCP
socket. The server sends messages to bots; bots send actions back.

### Messages sent FROM the server TO your bot

---

#### `welcome` — sent once on connection
```json
{
  "type": "welcome",
  "pid": 0,
  "chips": 1000,
  "big_blind": 20,
  "num_players": 4
}
```
`pid` is your permanent player ID for this session (0-indexed).

---

#### `hand_start` — broadcast at the start of each hand
```json
{
  "type": "hand_start",
  "dealer": 2,
  "sb": 0,
  "bb": 1,
  "pot": 30,
  "stacks": {"0": 980, "1": 960, "2": 1000, "3": 1000}
}
```

---

#### `hole_cards` — sent privately, only to you
```json
{
  "type": "hole_cards",
  "pid": 0,
  "cards": ["Ah", "Kd"],
  "chips": 980,
  "pot": 30,
  "num_players": 4
}
```

---

#### `community_cards` — broadcast on flop, turn, river
```json
{
  "type": "community_cards",
  "street": "flop",
  "cards": ["2c", "7h", "Td"],
  "pot": 80
}
```
`cards` always contains the **full board so far** (not just the new cards).

---

#### `action_request` — sent to you when it's your turn to act
```json
{
  "type": "action_request",
  "pid": 0,
  "street": "flop",
  "hole_cards": ["Ah", "Kd"],
  "community": ["2c", "7h", "Td"],
  "pot": 80,
  "chips": 980,
  "to_call": 20,
  "current_bet": 20,
  "min_raise": 40,
  "num_players": 4,
  "player_bets":   {"0": 0,  "1": 20, "2": 0,    "3": 0},
  "player_chips":  {"0": 980,"1": 960,"2": 1000,  "3": 1000},
  "player_folded": {"0": false,"1": false,"2": true,"3": false},
  "player_allin":  {"0": false,"1": false,"2": false,"3": false}
}
```
You **must** reply to this message with an action (see below) before the server
will continue. There is no timeout — the server blocks waiting for your reply.

---

#### `player_action` — broadcast when any other player acts
```json
{
  "type": "player_action",
  "pid": 1,
  "action": "raise",
  "amount": 60,
  "chips": 940,
  "street": "flop"
}
```

---

#### `winner` — broadcast when everyone else folds
```json
{
  "type": "winner",
  "pid": 2,
  "reason": "everyone_folded",
  "pot": 120,
  "stacks": {"0": 880, "1": 1000, "2": 1120, "3": 1000}
}
```

---

#### `showdown` — broadcast when hand goes to showdown
```json
{
  "type": "showdown",
  "hands": {
    "0": {"cards": ["Ah","Kd"], "hand": "One Pair", "score": 3434},
    "2": {"cards": ["7c","7h"], "hand": "Three of a Kind", "score": 1667}
  },
  "community": ["2c","7s","Td","4h","9c"],
  "winners": [2],
  "pot": 200,
  "hand_name": "Three of a Kind",
  "stacks": {"0": 880, "1": 1000, "2": 1200, "3": 920}
}
```
`score` is the hand evaluator's raw score — **lower is stronger** (1 = Royal Flush, 7462 = worst high card).

---

#### `game_over` — broadcast when only one player has chips
```json
{
  "type": "game_over",
  "winner": 2
}
```

---

### Messages sent FROM your bot TO the server

Reply to `action_request` with exactly one of these:

**Fold:**
```json
{"action": "fold"}
```

**Check** (only valid when `to_call == 0`):
```json
{"action": "check"}
```

**Call:**
```json
{"action": "call"}
```

**Raise** (`amount` = the total bet level you want to set, must be ≥ `min_raise`):
```json
{"action": "raise", "amount": 80}
```

**All-in:**
```json
{"action": "allin"}
```

> If your bot sends an invalid action, tries to check when a call is required,
> or disconnects without responding, the server automatically folds for you.

---

## Writing Your Bot

Open `bot.py` and find the `decide()` function. **That is the only thing you need to edit.**

```python
def decide(state: GameState):
    # Your logic here
    return "call"
```

### The GameState object

Everything in the `action_request` message is wrapped into a `GameState` object
with some extra helpers pre-computed for you:

```python
state.hole_cards      # ["Ah", "Kd"]          — your 2 private cards
state.community       # ["2c", "7h", "Td"]    — board cards (0–5)
state.street          # "preflop" | "flop" | "turn" | "river"

state.chips           # 980                   — your remaining chips
state.pot             # 80                    — total pot
state.to_call         # 20                    — chips needed to call
state.current_bet     # 20                    — highest bet this street
state.min_raise       # 40                    — minimum legal raise total

state.can_check       # True/False            — whether check is a legal action
state.pot_odds        # 0.2                   — to_call / (pot + to_call)
state.active_opponents# 2                     — non-folded players excluding you

state.player_bets     # {pid: bet}            — each player's bet this street
state.player_chips    # {pid: chips}          — each player's stack
state.player_folded   # {pid: bool}           — who has folded
state.player_allin    # {pid: bool}           — who is all-in

state.my_pid          # 0                     — your player ID
state.history         # [...]                 — all messages received this session
```

### Valid return values

```python
return "fold"
return "check"                    # only when state.can_check is True
return "call"
return "allin"
return ("raise", 80)              # 80 = total bet level you want to set
```

### Card format

Cards are two-character strings: rank + suit.

| Ranks | `2 3 4 5 6 7 8 9 T J Q K A` |
|---|---|
| Suits | `c` (clubs) `d` (diamonds) `h` (hearts) `s` (spades) |

Examples: `"Ah"` = Ace of hearts, `"Tc"` = Ten of clubs, `"2d"` = Two of diamonds.

---

## Troubleshooting

**"Connection refused" when a bot tries to connect**
- The server isn't running, or is running on a different port
- The firewall is blocking the port — see the firewall commands in the port forwarding section
- You used `localhost` as the server host but are connecting from another machine — use `0.0.0.0` on the server

**Server starts but never begins the game**
- Not all bots have connected yet — the server waits until `--players` seats are all filled
- One bot crashed on startup — check its terminal for errors

**Bot connects then immediately disconnects**
- Python version mismatch — ensure Python 3.8+ on both machines
- A bot script error in `decide()` crashing the client — check the bot terminal

**"Address already in use" when starting the server**
- A previous server instance is still running — kill it: `pkill -f poker_server.py`
- Or use a different port: `--port 9998`

**Bots can connect on LAN but not from the internet**
- Port forwarding rule is missing or points to the wrong internal IP
- The server machine's own firewall is blocking the port (not just the router)
- Your ISP blocks inbound connections on that port — try port 80 or 443

**Choppy/slow play over the internet**
- The default `recv()` has no timeout — add `sock.settimeout(30)` in `bot.py`'s `connect()` method to avoid hanging forever if the connection drops mid-hand
