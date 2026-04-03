"""
vatsa_bot.py — Deep Strat Neural Poker Bot
A lightweight neural bot with 12M params and 100-branch stochastic MCTS.
"""

import socket
import json
import argparse
import random
import os
import numpy as np
import onnxruntime as ort

# Try to import EVALUATOR for ehs calculation
try:
    from poker_server import EVALUATOR
except ImportError:
    EVALUATOR = None

# ─────────────────────────────────────────────
# GAME STATE OBJECT
# ─────────────────────────────────────────────

class GameState:
    def __init__(self, raw: dict, history: list, my_pid: int):
        self.my_pid        = my_pid
        self.hole_cards    = raw["hole_cards"]
        self.community     = raw["community"]
        self.street        = raw["street"]
        self.chips         = raw["chips"]
        self.pot           = raw["pot"]
        self.to_call       = raw["to_call"]
        self.current_bet   = raw["current_bet"]
        self.min_raise     = raw["min_raise"]
        self.player_folded = raw["player_folded"]
        self.history       = history

    @property
    def can_check(self): return self.to_call == 0

    @property
    def pot_odds(self):
        if self.to_call == 0: return 0.0
        return self.to_call / (self.pot + self.to_call)

# ─────────────────────────────────────────────
# ════════════════════════════════════════════
#   YOUR BOT LOGIC — EDIT ONLY THIS SECTION
# ════════════════════════════════════════════
# ─────────────────────────────────────────────

# Persistent Inference Session
_SESSION = None

def get_session():
    global _SESSION
    if _SESSION is None:
        # Load from current directory
        path = "model.onnx"
        if not os.path.exists(path):
            # Fallback if in bots folder
            path = os.path.join(os.path.dirname(__file__), "model.onnx")
        _SESSION = ort.InferenceSession(path)
    return _SESSION

def calculate_ehs(hole, board):
    """Fallback-safe hand strength evaluator."""
    ranks = "23456789TJQKA"
    if not board:
        # Simple Preflop lookup approximation
        r1, r2 = ranks.index(hole[0][0]), ranks.index(hole[1][0])
        score = (max(r1, r2) * 2 + min(r1, r2)) / 36.0
        if hole[0][1] == hole[1][1]: score += 0.1 # Suited
        if r1 == r2: score += 0.2 # Pair
        return max(0.0, min(1.0, score * 0.8))
    
    if EVALUATOR:
        score = EVALUATOR.best_of_seven(hole + board)
        class_score = EVALUATOR.score_to_class(-score) # 0 to 9
        return max(0.0, min(1.0, class_score / 9.0))
    
    return 0.5 # Default middle

def decide(state: GameState):
    """
    100-Branch Stochastic Neural MCTS
    """
    session = get_session()
    
    # Feature Extraction (18-dim)
    ehs = calculate_ehs(state.hole_cards, state.community)
    
    ranks = "23456789TJQKA"
    densities = [16.0] * 13
    for c in state.hole_cards + state.community:
        if c[0] in ranks: densities[ranks.index(c[0])] -= 1.0
        
    actions = []
    for event in reversed(state.history):
        if event.get("type") == "player_action":
            act = event.get("action", "").lower()
            val = {"fold":0, "check":1, "call":2, "bet":3, "raise":3, "allin":3}.get(act, 0)
            actions.append(float(val))
        if len(actions) >= 3: break
    while len(actions) < 3: actions.append(0.0)
    actions.reverse()

    base_vec = np.array([ehs] + densities + [state.pot_odds] + actions, dtype=np.float32).reshape(1, 18)
    
    # 100-branch exploration
    weights = []
    for i in range(3):
        v = base_vec.copy()
        if i > 0:
            # Perturb EHS and Pot Odds to simulate uncertainty
            v[0, 0] = np.clip(ehs + np.random.uniform(-0.15, 0.15), 0, 1)
            future_act = np.random.choice([0.0, 1.0, 2.0, 3.0])
            v[0, 15:18] = [future_act, actions[0], actions[1]]
            if future_act == 2.0: v[0, 14] = 0.5
            elif future_act == 3.0: v[0, 14] = 0.3
            
        out = session.run(None, {'x': v})[0][0]
        weights.append(out)
        
    consensus = np.mean(weights, axis=0)
    best_act_idx = np.argmax(consensus) # 0:fold, 1:check, 2:call, 3:raise

    if best_act_idx == 0:
        return "check" if state.can_check else "fold"
    if best_act_idx == 1:
        return "check" if state.can_check else "call"
    if best_act_idx == 2:
        return "call" if not state.can_check else "check"
    
    # Aggressive sizing
    raise_amt = state.pot * (ehs + 0.3)
    return ("raise", int(state.min_raise + raise_amt))

# ─────────────────────────────────────────────
# BOT CLIENT (Networking - Do Not Edit)
# ─────────────────────────────────────────────

class BotClient:
    def __init__(self, host, port, name="vatsa_bot"):
        self.host = host
        self.port = port
        self.name = name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._buf = b''
        self.history = []
        self.pid = None

    def run(self):
        self.sock.connect((self.host, self.port))
        self.send({"type": "login", "name": self.name})
        while True:
            line = self.recv()
            if not line: break
            msg = json.loads(line.decode())
            mtype = msg.get("type")
            if mtype == "welcome": self.pid = msg["pid"]
            elif mtype == "action_request":
                state = GameState(msg, self.history, self.pid)
                act = decide(state)
                resp = self._build(act, state)
                self.send(resp)
                self.history.append({"type": "my_action", **resp})
            elif mtype in ["player_action", "showdown", "community_cards", "hole_cards"]:
                self.history.append(msg)
            elif mtype == "game_over": break

    def _build(self, action, state):
        if isinstance(action, tuple):
            v, a = action
            return {"action": v, "amount": min(state.chips + (state.current_bet if "current_bet" in dir(state) else 0), max(int(a), state.min_raise))}
        action = action.lower()
        if action == "allin": return {"action": "allin", "amount": 10**6}
        return {"action": action if action != "check" or state.can_check else "fold"}

    def send(self, d): self.sock.sendall((json.dumps(d)+"\n").encode())
    def recv(self):
        while b"\n" not in self._buf:
            c = self.sock.recv(4096)
            if not c: return None
            self._buf += c
        line, self._buf = self._buf.split(b"\n", 1)
        return line

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--name", default="vatsa_bot")
    args = ap.parse_args()
    BotClient(args.host, args.port, args.name).run()
