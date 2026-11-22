"""Pass chance of each slow test when another test of its file fails, from
labelled synthetic commits (tools/synth.py label). The runner uses it to keep
running a slow test after its file has a failure, when passing is likely
enough to pay for its CPU (see blast.runner, Result.keep).

python tools/sibling_prior.py <tests.json> <out.json> <answers.json>...
"""

import json
import sys

MIN_SECONDS = 3.0
MIN_SEEN = 10


def main(tests_path, out, *answers):
    tests = {t["number"]: t for t in json.load(open(tests_path))["tests"]}
    slow = [n for n, t in tests.items() if t["seconds"] >= MIN_SECONDS]
    seen = {n: [0, 0] for n in slow}  # passes, observations
    for path in answers:
        for a in json.load(open(path))["commits"].values():
            failing = set(a["failing"])
            for n in slow:
                if tests[n]["file"] in {tests[f]["file"] for f in failing - {n} if f in tests}:
                    seen[n][1] += 1
                    seen[n][0] += n not in failing
    prior = {tests[n]["id"]: round(p / k, 3) for n, (p, k) in seen.items() if k >= MIN_SEEN}
    json.dump(prior, open(out, "w"), indent=1, sort_keys=True)
    print(prior)


if __name__ == "__main__":
    main(*sys.argv[1:])
