"""Check each stand-in of blast.proxies against its real test at the pinned
commit: the stand-in must pass, and run every sqlglot line the real test runs
(each in a forked child, so caches filled by one can't hide lines from the
other). Writes {nodeid: {"cpu": stand-in CPU seconds}} for the ones that hold.

cd <sqlglot checkout> && SKIP_INTEGRATION=0 PYTHONPATH=../../src \
    python ../../tools/proxy_map.py <out.json>
"""

import json
import os
import sys
import time
import unittest

os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, os.getcwd())
ROOT = os.path.join(os.getcwd(), "sqlglot") + os.sep

from blast import proxies  # noqa: E402


def _run(nodeid, proxy, trace):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        import importlib

        path, cls_name, meth = nodeid.split("::")
        cls = getattr(importlib.import_module(path[:-3].replace("/", ".")), cls_name)
        if proxy:
            proxies.install(nodeid, cls)
        lines = set()
        if trace:
            def line(code, ln):
                if code.co_filename.startswith(ROOT):
                    lines.add((code.co_filename[len(ROOT):], ln))
                return sys.monitoring.DISABLE

            sys.monitoring.use_tool_id(3, "proxy_map")
            sys.monitoring.register_callback(3, sys.monitoring.events.LINE, line)
            sys.monitoring.set_events(3, sys.monitoring.events.LINE)
        res = unittest.TestResult()
        t0 = time.process_time()
        unittest.TestSuite([cls(meth)]).run(res)
        cpu = time.process_time() - t0
        if trace:
            sys.monitoring.set_events(3, 0)
        os.write(w, json.dumps({"ok": res.wasSuccessful() and not res.skipped, "cpu": cpu,
                                "lines": sorted(lines)}).encode())
        os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 1 << 20):
        data += chunk
    os.waitpid(pid, 0)
    return json.loads(data)


def main(out):
    result = {}
    for nodeid in proxies.PROXIES:
        real = _run(nodeid, False, True)
        stand = _run(nodeid, True, True)
        timed = _run(nodeid, True, False)
        missing = {tuple(x) for x in real["lines"]} - {tuple(x) for x in stand["lines"]}
        print(nodeid, "real ok", real["ok"], "proxy ok", stand["ok"], "missing lines", len(missing),
              sorted(missing)[:10], "proxy cpu", round(timed["cpu"], 3))
        if real["ok"] and stand["ok"] and timed["ok"] and not missing:
            result[nodeid] = {"cpu": round(timed["cpu"], 3)}
    with open(out, "w") as fh:
        json.dump(result, fh, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:])
