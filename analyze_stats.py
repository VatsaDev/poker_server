
import csv
from collections import defaultdict

csv_path = r"c:\Users\vatsa\Desktop\projects\poke_server\ml_data\game_20260402_175419.csv"
stats = defaultdict(lambda: {"wins": 0, "pots": 0, "hands": 0})

with open(csv_path, 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        pid = int(row['pid'])
        stats[pid]["hands"] += 1
        if row['is_winner'] == '1':
            stats[pid]["wins"] += 1
            stats[pid]["pots"] += int(row['pot_won'])

print("Game Report:")
names = {0: "MCTS_3", 1: "MCTS_10", 2: "MCTS_100"}
for pid in sorted(stats.keys()):
    s = stats[pid]
    print(f"{names[pid]} (PID {pid}):")
    print(f"  Wins: {s['wins']}")
    print(f"  Total Pot Won: {s['pots']}")
    print(f"  Win Rate (at showdown): {s['wins']/(s['hands']):.1%}")
