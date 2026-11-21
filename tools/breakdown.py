"""Per-commit cost of an output vs. the ideal (only the failing tests)."""
import json, sys
sample, out = sys.argv[1], sys.argv[2]
tests = json.load(open(f"{sample}/tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ans = json.load(open(f"{sample}/answers.json"))["commits"]
tot = ideal_tot = 0
for line in open(out):
    o = json.loads(line); ws = [w for w in o["workers"] if w]
    cost = (max(sum(ms[t] for t in w) for w in ws) + 2000 * len(ws)) / 1000 if ws else 0
    f = ans[o["commit"]]["failing"]
    ideal = (sum(ms[t] for t in f) + 2000) / 1000 if f else 0
    tot += cost; ideal_tot += ideal
    flag = "  <==" if cost - ideal > 1 else ""
    print(f"{o['commit']} {ans[o['commit']]['kind']:8} cost={cost:6.2f} ideal={ideal:6.2f} n={sum(map(len, ws)):4} w={len(ws)}{flag}")
print(f"total {tot:.1f} ideal {ideal_tot:.1f}")
