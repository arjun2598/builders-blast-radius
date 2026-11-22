"""Scaled-down stand-ins for slow tests.

tests/test_parser.py::TestParser::test_max_nodes parses a statement of about a
million nodes to check the parser's node limit. Its stand-in makes the same
calls and assertions at a hundredth of the size, with the limits scaled down
with the statement. tools/proxy_map.py checks at the pinned commit that the
stand-in runs every sqlglot line the real test runs (data/proxy.json). Only
then does the runner swap it in.
"""

from __future__ import annotations


def max_nodes(self):
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

    width = 100
    depth = 100
    expr = "x" + "=x" * depth
    cols = [expr for _ in range(width)]
    sql = f"SELECT {','.join(cols)}"

    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=10_000)
    self.assertIn("Maximum number of AST nodes", str(ctx.exception))

    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=500)
    self.assertIn("(500)", str(ctx.exception))

    with self.assertRaises(ParseError):
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)


PROXIES = {"tests/test_parser.py::TestParser::test_max_nodes": max_nodes}


def install(nodeid: str, cls) -> bool:
    fn = PROXIES.get(nodeid)
    if fn is None:
        return False
    setattr(cls, nodeid.rsplit("::", 1)[1], fn)
    return True
