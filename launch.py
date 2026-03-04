#!/usr/bin/env python3
"""
launch.py — Start the poker server + N bot processes automatically.

Usage:
    python launch.py [--players N] [--chips CHIPS] [--bb BB]

Each bot gets a unique name (Bot0, Bot1, …).
All processes run in the foreground; Ctrl-C shuts everything down.
"""

import subprocess
import sys
import time
import argparse
import signal
import os

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=4)
    ap.add_argument("--chips",   type=int, default=1000)
    ap.add_argument("--bb",      type=int, default=20)
    ap.add_argument("--host",    default="localhost")
    ap.add_argument("--port",    type=int, default=9999)
    args = ap.parse_args()

    procs = []

    # Start server
    server_cmd = [
        sys.executable, "poker_server.py",
        "--host",    args.host,
        "--port",    str(args.port),
        "--players", str(args.players),
        "--chips",   str(args.chips),
        "--bb",      str(args.bb),
    ]
    print(f"[LAUNCH] Starting server: {' '.join(server_cmd)}")
    procs.append(subprocess.Popen(server_cmd))
    time.sleep(0.5)   # give server a moment to bind

    # Start bots
    for i in range(args.players):
        bot_cmd = [
            sys.executable, "bot.py",
            "--host", args.host,
            "--port", str(args.port),
            "--name", f"Bot{i}",
        ]
        print(f"[LAUNCH] Starting {bot_cmd[-1]}")
        procs.append(subprocess.Popen(bot_cmd))
        time.sleep(0.1)

    print("[LAUNCH] All processes started. Press Ctrl-C to stop.\n")

    def shutdown(sig, frame):
        print("\n[LAUNCH] Shutting down…")
        for p in procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Wait for server to finish
    procs[0].wait()
    for p in procs[1:]:
        p.terminate()

if __name__ == "__main__":
    main()
