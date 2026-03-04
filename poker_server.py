"""
poker_server.py — Texas Hold'em Poker Table Server

N bot clients connect via TCP. The server handles all game logic:
dealing cards, blinds, betting rounds, side pots, showdown, etc.

Usage:
    python poker_server.py [--host HOST] [--port PORT] [--players N] [--chips CHIPS] [--bb BIG_BLIND]

Defaults: host=localhost, port=9999, players=4, chips=1000, bb=20
"""

import socket
import threading
import json
import random
import argparse
import time
import sys
from collections import defaultdict
from itertools import combinations

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

def make_deck():
    return [r+s for r in RANKS for s in SUITS]

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

class _LUT:
    """
    Pure-Python lookup table evaluator.
    Built once at import time; evaluation of any 5-card hand is O(1).
    Handles every edge case: wheel straight (A-2-3-4-5),
    straight flush vs plain straight, all flush/pair interactions.
    """
    # Unique prime product for each of the 5-card rank patterns
    # We use the same prime-product trick as Cactus Kev.

    # All 5-card rank combos produce a unique product of primes in [2..41].
    # We store two dicts:
    #   _flush_rank[prime_product] → score  (for flushes / straight-flushes)
    #   _rank[prime_product]       → score  (for non-flushes)

    def __init__(self):
        self._flush = {}   # prime_product → score  (flush + straight-flush)
        self._mixed = {}   # prime_product → score  (pairs, trips, quads, straights, high-card)
        self._build()

    # ── straight detection ──────────────────
    @staticmethod
    def _is_straight(ranks_set):
        """Return the high-card rank (0-12) of the straight, or -1."""
        if len(ranks_set) != 5:
            return -1
        hi = max(ranks_set)
        lo = min(ranks_set)
        if hi - lo == 4:
            return hi
        # Wheel: A-2-3-4-5  (A=12, 2=0, 3=1, 4=2, 5=3)
        if ranks_set == {12, 0, 1, 2, 3}:
            return 3   # 5-high straight
        return -1

    def _build(self):
        score = [0]   # mutable counter

        def nxt():
            score[0] += 1
            return score[0]

        # ── Straight flushes (10 total: Royal down to 5-high) ──
        # High card ranks 12 (A) down to 3 (wheel high=3)
        for hi in [12,11,10,9,8,7,6,5,4,3]:
            if hi == 3:
                rs = {12,0,1,2,3}   # wheel
            else:
                rs = set(range(hi-4, hi+1))
            prod = 1
            for r in rs:
                prod *= PRIMES[r]
            self._flush[prod] = nxt()   # straight-flush

        # ── Four of a Kind (156 combos) ──
        # Sorted: AAAA+K down to 2222+3
        quads_order = [(q, k) for q in range(12,-1,-1) for k in range(12,-1,-1) if k != q]
        for q, k in quads_order:
            prod = PRIMES[q]**4 * PRIMES[k]
            self._mixed[prod] = nxt()

        # ── Full House (156 combos) ──
        fh_order = [(t, p) for t in range(12,-1,-1) for p in range(12,-1,-1) if p != t]
        for t, p in fh_order:
            prod = PRIMES[t]**3 * PRIMES[p]**2
            self._mixed[prod] = nxt()

        # ── Flush (non-straight, 1277 combos) ──
        from itertools import combinations as _comb
        # All 5-card rank combos, no 5-in-a-row, sorted by descending rank vector
        flush_combos = []
        for combo in _comb(range(13), 5):
            rs = set(combo)
            hi = _LUT._is_straight(rs)
            if hi == -1:  # not a straight
                flush_combos.append(sorted(combo, reverse=True))
        flush_combos.sort(reverse=True)
        for combo in flush_combos:
            prod = 1
            for r in combo:
                prod *= PRIMES[r]
            self._flush[prod] = nxt()

        # ── Straight (10 combos, non-flush) ──
        for hi in [12,11,10,9,8,7,6,5,4,3]:
            if hi == 3:
                rs = {12,0,1,2,3}
            else:
                rs = set(range(hi-4, hi+1))
            prod = 1
            for r in rs:
                prod *= PRIMES[r]
            self._mixed[prod] = nxt()

        # ── Three of a Kind (858 combos) ──
        trips_order = []
        for t in range(12,-1,-1):
            kickers = [(k1,k2) for k1 in range(12,-1,-1)
                                for k2 in range(k1-1,-1,-1)
                                if k1 != t and k2 != t]
            for k1, k2 in kickers:
                trips_order.append((t, k1, k2))
        for t, k1, k2 in trips_order:
            prod = PRIMES[t]**3 * PRIMES[k1] * PRIMES[k2]
            self._mixed[prod] = nxt()

        # ── Two Pair (858 combos) ──
        tp_order = []
        for p1 in range(12,-1,-1):
            for p2 in range(p1-1,-1,-1):
                for k in range(12,-1,-1):
                    if k != p1 and k != p2:
                        tp_order.append((p1, p2, k))
        for p1, p2, k in tp_order:
            prod = PRIMES[p1]**2 * PRIMES[p2]**2 * PRIMES[k]
            self._mixed[prod] = nxt()

        # ── One Pair (2860 combos) ──
        op_order = []
        for p in range(12,-1,-1):
            kickers = [(k1,k2,k3) for k1 in range(12,-1,-1)
                                   for k2 in range(k1-1,-1,-1)
                                   for k3 in range(k2-1,-1,-1)
                                   if p not in (k1,k2,k3)]
            for k1,k2,k3 in kickers:
                op_order.append((p,k1,k2,k3))
        for p,k1,k2,k3 in op_order:
            prod = PRIMES[p]**2 * PRIMES[k1] * PRIMES[k2] * PRIMES[k3]
            self._mixed[prod] = nxt()

        # ── High Card (1277 combos, non-flush, non-straight) ──
        hc_combos = []
        for combo in _comb(range(13), 5):
            rs = set(combo)
            if _LUT._is_straight(rs) == -1:
                hc_combos.append(sorted(combo, reverse=True))
        hc_combos.sort(reverse=True)
        for combo in hc_combos:
            prod = 1
            for r in combo:
                prod *= PRIMES[r]
            self._mixed[prod] = nxt()

    def score_five(self, cards) -> int:
        """
        Evaluate a 5-card hand (list of card strings).
        Returns an integer 1 (best) … 7462 (worst).
        """
        encoded = [_encode(c) for c in cards]
        # Flush detection: AND all suit bits; if non-zero all cards share a suit
        suit_and = encoded[0] & encoded[1] & encoded[2] & encoded[3] & encoded[4] & 0xF000
        # Prime product (rank only)
        prod = 1
        for e in encoded:
            prod *= (e & 0xFF)
        if suit_and:
            return self._flush[prod]
        return self._mixed[prod]

    def best_of_seven(self, cards) -> int:
        """Best 5-card score from up to 7 cards (lower = better)."""
        best = 9999
        for combo in combinations(cards, 5):
            s = self.score_five(combo)
            if s < best:
                best = s
        return best

    def score_to_class(self, score: int) -> int:
        """Map a score [1-7462] to a hand class 0-8."""
        if score <=   10: return 8   # Straight Flush
        if score <=  166: return 7   # Four of a Kind
        if score <=  322: return 6   # Full House
        if score <= 1599: return 5   # Flush
        if score <= 1609: return 4   # Straight
        if score <= 2467: return 3   # Three of a Kind
        if score <= 3325: return 2   # Two Pair
        if score <= 6185: return 1   # One Pair
        return 0                     # High Card

# Build the table once at import time (takes ~0.2 s, purely in Python)
print("[EVAL] Building hand evaluation lookup table…", end=" ", flush=True)
EVALUATOR = _LUT()
print("done.")

HAND_NAMES = ['High Card','One Pair','Two Pair','Three of a Kind','Straight',
              'Flush','Full House','Four of a Kind','Straight Flush']

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
        self.lock   = threading.Lock()

    def send(self, msg: dict):
        try:
            data = json.dumps(msg) + '\n'
            self.conn.sendall(data.encode())
        except Exception:
            self.active = False

    def recv(self):
        buf = b''
        while b'\n' not in buf:
            chunk = self.conn.recv(1024)
            if not chunk:
                self.active = False
                return None
            buf += chunk
        return json.loads(buf.split(b'\n')[0].decode())

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
            self.players.append(p)
            print(f"[SERVER] Player {pid} connected from {addr}")
            p.send({"type": "welcome", "pid": pid,
                    "chips": self.starting_chips,
                    "big_blind": self.big_blind,
                    "num_players": self.num_players})

        print("[SERVER] All players connected. Starting game.")
        srv.close()

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
        deck = make_deck()
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
                p.folded = True
                self.broadcast({"type": "player_action", "pid": p.pid,
                                "action": "fold", "street": street}, exclude=p)
                acted.add(p.pid)
            else:
                act  = action.get("action", "fold")
                amt  = action.get("amount", 0)
                pot_add += self._apply_action(p, act, amt, current_bet, street)
                if act in ("raise", "bet"):
                    current_bet   = p.bet
                    last_aggressor = p.pid
                    acted = {p.pid}   # others need to act again
                else:
                    acted.add(p.pid)

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


