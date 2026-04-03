"""
poker_server.py — Texas Hold'em Poker Table Server

N bot clients connect via TCP. The server handles all game logic:
dealing cards, blinds, betting rounds, side pots, showdown, etc.

Usage:
    python poker_server.py [--host HOST] [--port PORT] [--players N] [--chips CHIPS] [--bb BIG_BLIND]

Defaults: host=localhost, port=9999, players=4, chips=1000, bb=20
"""

import functools

# Force all print statements to flush immediately for remote terminal stability
print = functools.partial(print, flush=True)

import socket
import threading
import json
import random
import argparse
import math
import time
import sys
import os
import csv
import datetime
from collections import defaultdict
from itertools import combinations
from poker_db import PokerDB

# ─────────────────────────────────────────────
# CARD ENCODING
# ─────────────────────────────────────────────
# Cards are represented as strings like "Ah", "Td", "2c".
# Internally each card is an integer with this bit layout (Cactus Kev):
#
#  Bits 31-16: unused
#  Bits 15-12: suit bitmask  (1=clubs 2=diamonds 4=hearts 8=spades)
#  Bits 11- 8: rank 0-12    (2=0 … A=12)
#  Bits  7- 0: prime for rank (each rank maps to a unique prime)
#
# The prime encoding lets us detect pairs/trips/quads via multiplication
# and the suit bits let us detect flushes in one AND.

RANKS   = ['2','3','4','5','6','7','8','9','T','J','Q','K','A']
SUITS   = ['c','d','h','s']
# One prime per rank – product of any 5 distinct primes is unique
PRIMES  = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41]
SUIT_BITS = {'c': 0x8000, 'd': 0x4000, 'h': 0x2000, 's': 0x1000}

def _encode(card_str: str) -> int:
    r = RANKS.index(card_str[0])
    s = SUIT_BITS[card_str[1]]
    return s | (r << 8) | PRIMES[r]

def make_deck(num_players):
    num_decks = max(1, min(4, (num_players // 5) + 1))
    return [r+s for r in RANKS for s in SUITS] * num_decks

# ─────────────────────────────────────────────
# LOOKUP TABLE HAND EVALUATOR
# ─────────────────────────────────────────────
# Returns an integer in [1, 7462] where:
#   1     = Royal Flush  (best)
#   7462  = 7-2 offsuit high card (worst)
# LOWER score = STRONGER hand.
#
# Hand class boundaries (inclusive):
#   1   –  10  : Straight Flush  (class 8)
#  11   – 166  : Four of a Kind  (class 7)
# 167   – 322  : Full House      (class 6)
# 323   – 1599 : Flush           (class 5)
# 1600  – 1609 : Straight        (class 4)
# 1610  – 2467 : Three of a Kind (class 3)
# 2468  – 3325 : Two Pair        (class 2)
# 3326  – 6185 : One Pair        (class 1)
# 6186  – 7462 : High Card       (class 0)

class SimpleEvaluator:
    def score_five(self, cards) -> int:
        ranks = "23456789TJQKA"
        parsed = [(ranks.index(c[0]), c[1]) for c in cards]
        parsed.sort(key=lambda x: x[0], reverse=True)
        
        is_flush = len(set(c[1] for c in parsed)) == 1
        rs = [c[0] for c in parsed]
        
        is_straight = False
        if rs[0] - rs[-1] == 4 and len(set(rs)) == 5:
            is_straight = True
        elif rs == [12, 3, 2, 1, 0]: # wheel
            is_straight = True
            rs = [3, 2, 1, 0, -1] # effectively A becomes low
            
        counts = {}
        for r in rs: counts[r] = counts.get(r, 0) + 1
        
        freqs = sorted([(c, r) for r, c in counts.items()], reverse=True)
        pattern = [f[0] for f in freqs]
        
        if pattern == [5]: class_score = 9
        elif is_straight and is_flush: class_score = 8
        elif pattern == [4, 1]: class_score = 7
        elif pattern == [3, 2]: class_score = 6
        elif is_flush: class_score = 5
        elif is_straight: class_score = 4
        elif pattern == [3, 1, 1]: class_score = 3
        elif pattern == [2, 2, 1]: class_score = 2
        elif pattern == [2, 1, 1, 1]: class_score = 1
        else: class_score = 0
        
        tie_breaker = tuple(f[1] for f in freqs for _ in range(f[0]))
        
        int_score = class_score << 20
        for i, t in enumerate(tie_breaker):
            int_score |= (t + 1) << (16 - i*4)
            
        return -int_score

    def best_of_seven(self, cards) -> int:
        """Best 5-card score from up to 7 cards (lower = better)."""
        best = 999999999
        for combo in combinations(cards, 5):
            s = self.score_five(combo)
            if s < best:
                best = s
        return best

    def score_to_class(self, score: int) -> int:
        score = -score
        return score >> 20
        
EVALUATOR = SimpleEvaluator()

HAND_NAMES = ['High Card','One Pair','Two Pair','Three of a Kind','Straight',
              'Flush','Full House','Four of a Kind','Straight Flush', 'Five of a Kind']

# ── Public interface used by the rest of the server ──────────────

def best_hand_score(hole, community):
    """Return (score, class_int, name) for the best 5-card hand."""
    all_cards = hole + community
    score = EVALUATOR.best_of_seven(all_cards)
    cls   = EVALUATOR.score_to_class(score)
    return score, cls, HAND_NAMES[cls]

# ─────────────────────────────────────────────
# PLAYER STATE
# ─────────────────────────────────────────────

class Player:
    def __init__(self, pid, conn, addr, chips):
        self.pid    = pid
        self.conn   = conn
        self.addr   = addr
        self.chips  = chips
        self.hole   = []
        self.bet    = 0        # bet in current street
        self.total_bet = 0     # bet in current hand
        self.folded = False
        self.all_in = False
        self.active = True     # connected
        self.name   = f"Player_{pid}"
        self.lock   = threading.Lock()

    def send(self, msg: dict):
        try:
            data = json.dumps(msg) + '\n'
            self.conn.sendall(data.encode())
        except Exception:
            self.active = False

    def recv(self):
        self.conn.settimeout(10.0)
        buf = b''
        try:
            while b'\n' not in buf:
                chunk = self.conn.recv(1024)
                if not chunk:
                    self.active = False
                    return None
                buf += chunk
            self.conn.settimeout(None)
            return json.loads(buf.split(b'\n')[0].decode())
        except socket.timeout:
            print(f"[SERVER] {self.name} timed out.")
            return None
        except Exception:
            self.active = False
            return None

# ─────────────────────────────────────────────
# POKER SERVER
# ─────────────────────────────────────────────

class PokerServer:
    def __init__(self, host, port, num_players, starting_chips, big_blind):
        self.host           = host
        self.port           = port
        self.num_players    = num_players
        self.starting_chips = starting_chips
        self.big_blind      = big_blind
        self.small_blind    = big_blind // 2

        self.players  = []
        self.lock     = threading.Lock()
        self.ready    = threading.Event()
        self.db       = PokerDB()
        self.current_hand_id = None
        self.game_id  = datetime.datetime.now().strftime("game_%Y%m%d_%H%M%S")

    # ── Connection phase ──────────────────────
    def accept_players(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(self.num_players)
        print(f"[SERVER] Listening on {self.host}:{self.port} — waiting for {self.num_players} players…")

        while len(self.players) < self.num_players:
            conn, addr = srv.accept()
            pid = len(self.players)
            p = Player(pid, conn, addr, self.starting_chips)
            
            p.conn.settimeout(5.0)
            try:
                # Need to read raw inside try using p.recv() will use 10s default, which is fine, 
                # but let's just use it and rely on its builtin 10s timeout, or socket directly
                # because p.recv() overrides timeout to 10.0
                buf = b''
                while b'\n' not in buf:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                msg = json.loads(buf.split(b'\n')[0].decode())
                if msg and msg.get("type") == "login":
                    p.name = msg.get("name", p.name)
            except Exception:
                pass
            p.conn.settimeout(None)
            
            self.players.append(p)
            print(f"[SERVER] {p.name} (Player {pid}) connected from {addr}")

        print("[SERVER] All players connected. Starting game.")
        srv.close()

        names_dict = {p.pid: p.name for p in self.players}
        for p in self.players:
            self.db.add_player(p.pid, p.name)
            p.send({"type": "welcome", "pid": p.pid,
                    "chips": self.starting_chips,
                    "big_blind": self.big_blind,
                    "num_players": self.num_players,
                    "player_names": names_dict})

    # ── Broadcast ─────────────────────────────
    def broadcast(self, msg: dict, exclude=None):
        for p in self.players:
            if p.active and p != exclude:
                p.send(msg)

    # ── Main game loop ─────────────────────────
    def run(self):
        self.accept_players()
        dealer_idx = 0

        while True:
            alive = [p for p in self.players if p.active and p.chips > 0]
            if len(alive) < 2:
                winner = alive[0] if alive else None
                self.broadcast({"type": "game_over",
                                "winner": winner.pid if winner else -1})
                print(f"[SERVER] Game over. Winner: player {winner.pid if winner else 'none'}")
                break

            self.play_hand(alive, dealer_idx % len(alive))
            dealer_idx += 1
            time.sleep(1)

    # ── Single hand ───────────────────────────
    def play_hand(self, players, dealer_idx):
        deck = make_deck(self.num_players)
        random.shuffle(deck)
        community = []

        # Reset state
        for p in players:
            p.hole      = []
            p.bet       = 0
            p.total_bet = 0
            p.folded    = False
            p.all_in    = False

        n = len(players)
        sb_idx  = (dealer_idx + 1) % n
        bb_idx  = (dealer_idx + 2) % n
        pot     = 0

        # Post blinds
        pot += self._post_blind(players[sb_idx], self.small_blind)
        pot += self._post_blind(players[bb_idx],  self.big_blind)

        # Deal hole cards
        for p in players:
            p.hole = [deck.pop(), deck.pop()]
            p.send({"type": "hole_cards", "cards": p.hole,
                    "pid": p.pid, "chips": p.chips,
                    "pot": pot, "num_players": n})

        # Notify all players about the hand start (without hole cards)
        self.broadcast({"type": "hand_start",
                        "dealer": players[dealer_idx].pid,
                        "sb": players[sb_idx].pid,
                        "bb": players[bb_idx].pid,
                        "pot": pot,
                        "stacks": {p.pid: p.chips for p in players}})

        # Log hand start
        self.current_hand_id = self.db.log_hand_start(
            self.game_id, players[dealer_idx].pid, players[sb_idx].pid, players[bb_idx].pid, pot
        )
        # Log initial blind actions
        self.db.log_action(self.current_hand_id, "preflop", players[sb_idx].pid, "small_blind", self.small_blind, players[sb_idx].chips)
        self.db.log_action(self.current_hand_id, "preflop", players[bb_idx].pid, "big_blind", self.big_blind, players[bb_idx].chips)

        # ── Betting rounds ──
        streets = [
            ("preflop",  [],                          0),
            ("flop",     [deck.pop() for _ in range(3)], 0),
            ("turn",     [deck.pop()],                 0),
            ("river",    [deck.pop()],                 0),
        ]

        first_actor_preflop = (bb_idx + 1) % n

        for street_name, new_cards, _ in streets:
            community.extend(new_cards)
            if new_cards:
                self.broadcast({"type": "community_cards",
                                "street": street_name,
                                "cards": community,
                                "pot": pot})
                self.db.log_community(self.current_hand_id, street_name, community)

            active = [p for p in players if not p.folded and not p.all_in]
            if len([p for p in players if not p.folded]) <= 1:
                break

            # Reset per-street bets
            for p in players:
                p.bet = 0

            current_bet = self.big_blind if street_name == "preflop" else 0
            start_idx   = first_actor_preflop if street_name == "preflop" else (dealer_idx + 1) % n

            pot += self._betting_round(players, start_idx, current_bet, community, pot, street_name)

        # ── Showdown ──
        contenders = [p for p in players if not p.folded]
        if len(contenders) == 1:
            winner = contenders[0]
            winner.chips += pot
            self.broadcast({"type": "winner",
                            "pid": winner.pid,
                            "reason": "everyone_folded",
                            "pot": pot,
                            "stacks": {p.pid: p.chips for p in players}})
            score, cls, name = best_hand_score(winner.hole, community)
            self.db.log_showdown(self.current_hand_id, winner.pid, winner.hole, "everyone_folded", 0, True, pot)
            self._log_ml_data(winner.pid, winner.hole, community, "everyone_folded", score, True, pot)
            self.db.update_hand_pot(self.current_hand_id, pot)
        else:
            self._showdown(contenders, community, pot, players)

    def _post_blind(self, player, amount):
        amount = min(amount, player.chips)
        player.chips -= amount
        player.bet    = amount
        player.total_bet = amount
        return amount

    # ── Betting round ─────────────────────────
    def _betting_round(self, players, start_idx, current_bet, community, pot_so_far, street):
        n = len(players)
        pot_add = 0
        last_aggressor = None
        acted = set()
        idx = start_idx

        while True:
            p = players[idx % n]
            idx += 1

            if p.folded or p.all_in:
                # Skip but check if round should end
                active_can_act = [x for x in players if not x.folded and not x.all_in]
                if not active_can_act:
                    break
                # Check if everyone who can act has acted and bets are equal
                if all(x.pid in acted and x.bet == current_bet
                       for x in active_can_act):
                    break
                continue

            to_call = current_bet - p.bet

            # Build game state for this player
            state = {
                "type":          "action_request",
                "pid":           p.pid,
                "street":        street,
                "hole_cards":    p.hole,
                "community":     community,
                "pot":           pot_so_far + pot_add,
                "chips":         p.chips,
                "to_call":       to_call,
                "current_bet":   current_bet,
                "min_raise":     current_bet + max(current_bet, self.big_blind),
                "num_players":   len(players),
                "player_bets":   {x.pid: x.bet   for x in players},
                "player_chips":  {x.pid: x.chips for x in players},
                "player_folded": {x.pid: x.folded for x in players},
                "player_allin":  {x.pid: x.all_in for x in players},
            }
            p.send(state)

            action = p.recv()
            if action is None or not p.active:
                act = "timeout"
                amt = 0
            else:
                act = action.get("action", "fold")
                try:
                    raw_amt = action.get("amount", 0)
                    amt = float(raw_amt) if raw_amt is not None else 0
                    if math.isnan(amt):
                        amt = 0
                        act = "nan_action"
                except (ValueError, TypeError):
                    amt = 0
                    act = "invalid_action"

            # MANDATORY $5 MINIMUM CONTRIBUTION (EXCEPT IF ALREADY ALL-IN)
            if not p.all_in:
                # Calculate current contribution delta
                if act in ("raise", "bet"):
                    target_delta = amt - p.bet
                elif act == "allin":
                    target_delta = p.chips
                else:
                    # check, call, fold, timeout, nan
                    target_delta = (current_bet - p.bet) if act in ("call", "check") else 0
                
                # Check if they contributed enough
                if target_delta < 5:
                    boost = min(5, p.chips)
                    if boost > target_delta:
                        # If they were checking/calling, update to bet/raise
                        if act in ("call", "check"):
                            act = "bet" if current_bet == 0 else "raise"
                            amt = p.bet + boost
                        elif act in ("raise", "bet"):
                            amt = p.bet + boost
                        else:
                            # For fold/timeout/nan: strictly deduct the tax but don't change 'act'
                            # We apply it manually here since _apply_action(fold) adds nothing
                            tax = boost - target_delta
                            p.chips    -= tax
                            p.bet      += tax
                            p.total_bet+= tax
                            pot_add    += tax
                            if p.chips == 0: p.all_in = True

            pot_add += self._apply_action(p, act, amt, current_bet, street)
            
            # Map timeout/nan back to fold for logic flow if needed
            if act in ("timeout", "nan_action", "invalid_action"):
                p.folded = True

            if act in ("raise", "bet"):
                current_bet   = p.bet
                last_aggressor = p.pid
                acted = {p.pid}
            else:
                acted.add(p.pid)

            self.db.log_action(self.current_hand_id, street, p.pid, act, amt if act in ("raise", "bet") else p.bet, p.chips)
            self.db.update_hand_pot(self.current_hand_id, pot_so_far + pot_add)

            self.broadcast({"type": "player_action", "pid": p.pid,
                            "action": act, "amount": p.bet,
                            "chips": p.chips, "street": street}, exclude=p)

            # Check round-end conditions
            active_can_act = [x for x in players if not x.folded and not x.all_in]
            if not active_can_act:
                break
            if all(x.pid in acted and (x.bet == current_bet or x.chips == 0)
                   for x in active_can_act):
                break
            if len([x for x in players if not x.folded]) <= 1:
                break

        return pot_add

    def _apply_action(self, player, action, amount, current_bet, street):
        added = 0
        if action == "fold":
            player.folded = True

        elif action in ("call", "check"):
            to_call = min(current_bet - player.bet, player.chips)
            player.chips    -= to_call
            player.bet      += to_call
            player.total_bet+= to_call
            added            = to_call
            if player.chips == 0:
                player.all_in = True

        elif action in ("raise", "bet"):
            # amount = total bet level desired
            new_total = min(amount, player.chips + player.bet)
            delta = new_total - player.bet
            delta = max(delta, 0)
            player.chips    -= delta
            player.bet      += delta
            player.total_bet+= delta
            added            = delta
            if player.chips == 0:
                player.all_in = True

        elif action == "allin":
            delta = player.chips
            player.bet      += delta
            player.total_bet+= delta
            player.chips     = 0
            player.all_in    = True
            added            = delta

        return added

    # ── Showdown ──────────────────────────────
    def _showdown(self, contenders, community, pot, all_players):
        results = []
        for p in contenders:
            score, cls, name = best_hand_score(p.hole, community)
            results.append((score, cls, name, p))

        # Lower score = stronger hand
        results.sort(key=lambda x: x[0])
        best_score = results[0][0]
        winners = [p for score, cls, name, p in results if score == best_score]
        best_name = results[0][2]

        share     = pot // len(winners)
        remainder = pot % len(winners)

        for i, p in enumerate(winners):
            gain = share + (1 if i == 0 else 0) * remainder
            p.chips += gain

        reveal = {str(p.pid): {"cards": p.hole,
                               "hand":  best_hand_score(p.hole, community)[2],
                               "score": best_hand_score(p.hole, community)[0]}
                  for p in contenders}

        self.broadcast({
            "type":      "showdown",
            "hands":     reveal,
            "community": community,
            "winners":   [p.pid for p in winners],
            "pot":       pot,
            "hand_name": best_name,
            "stacks":    {p.pid: p.chips for p in all_players}
        })

        for p in contenders:
            is_winner = p in winners
            gain = (pot // len(winners)) if is_winner else 0
            # score, cls, name
            score, _, name = best_hand_score(p.hole, community)
            self.db.log_showdown(self.current_hand_id, p.pid, p.hole, name, score, is_winner, gain)
            self._log_ml_data(p.pid, p.hole, community, name, score, is_winner, gain)
        
        self.db.update_hand_pot(self.current_hand_id, pot)

    def _log_ml_data(self, pid, hole_cards, community_cards, hand_name, score, is_winner, pot_won):
        os.makedirs("ml_data", exist_ok=True)
        csv_path = os.path.join("ml_data", f"{self.game_id}.csv")
        write_header = not os.path.exists(csv_path)
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(['timestamp', 'hand_id', 'pid', 'hole_cards', 'community_cards', 'hand_name', 'score', 'is_winner', 'pot_won'])
            writer.writerow([
                datetime.datetime.now().isoformat(),
                self.current_hand_id,
                pid,
                json.dumps(hole_cards),
                json.dumps(community_cards),
                hand_name,
                score,
                int(is_winner),
                pot_won
            ])

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Poker Table Server")
    ap.add_argument("--host",    default="localhost")
    ap.add_argument("--port",    type=int, default=9999)
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--chips",   type=int, default=1000)
    ap.add_argument("--bb",      type=int, default=20)
    args = ap.parse_args()

    server = PokerServer(args.host, args.port, args.players, args.chips, args.bb)
    server.run()


