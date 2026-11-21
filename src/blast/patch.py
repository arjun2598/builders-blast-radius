"""Unified-diff parsing and applying, with no external tools (the run image has
no git or patch)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    lines: list[str]  # each starts with ' ', '-' or '+'


@dataclass
class FileDiff:
    old_path: str | None  # None when the file is created
    new_path: str | None  # None when the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def path(self) -> str:
        return self.old_path or self.new_path  # type: ignore[return-value]

    def changed_old_lines(self) -> set[int]:
        """Old-file line numbers touched by the diff. An insertion marks the
        old lines on both sides of it."""
        out: set[int] = set()
        for h in self.hunks:
            old = h.old_start
            for line in h.lines:
                tag = line[:1]
                if tag == " ":
                    old += 1
                elif tag == "-":
                    out.add(old)
                    old += 1
                elif tag == "+":
                    out.add(max(old - 1, 1))
                    out.add(old)
        return out


def _strip(path: str) -> str | None:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileDiff(m.group(1), m.group(2)) if m else FileDiff(None, None)
            files.append(cur)
            hunk = None
        elif cur is not None and hunk is None and line.startswith("--- "):
            cur.old_path = _strip(line[4:])
        elif cur is not None and hunk is None and line.startswith("+++ "):
            cur.new_path = _strip(line[4:])
        elif cur is not None and line.startswith("new file mode"):
            cur.old_path = None
            cur.new_mode = line.split()[-1]
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new_path = None
        elif cur is not None and line.startswith("rename from "):
            cur.old_path = line[len("rename from "):]
        elif cur is not None and line.startswith("rename to "):
            cur.new_path = line[len("rename to "):]
        elif cur is not None and line.startswith("old mode "):
            cur.old_mode = line.split()[-1]
        elif cur is not None and line.startswith("new mode "):
            cur.new_mode = line.split()[-1]
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur.binary = True
        elif cur is not None and line.startswith("@@"):
            m = HUNK.match(line)
            hunk = Hunk(int(m.group(1)), [])
            cur.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line.startswith("\\"):
            # "\ No newline at end of file" applies to the previous line
            if hunk.lines:
                hunk.lines[-1] = hunk.lines[-1] + "\\NOEOL"
        elif hunk is not None and line == "":
            hunk.lines.append(" ")  # some tools drop the space on blank context
        i += 1
    return files


class ApplyError(Exception):
    pass


def apply(old_text: str | None, fd: FileDiff) -> str | None:
    """Return the new file content (None if deleted)."""
    if fd.new_path is None:
        return None
    if not fd.hunks:
        return old_text
    src = (old_text or "").split("\n")
    trailing_nl = old_text is None or old_text.endswith("\n")
    if old_text and old_text.endswith("\n"):
        src.pop()
    out: list[str] = []
    pos = 0  # index into src
    new_eol = True
    for h in fd.hunks:
        start = max(h.old_start - 1, 0) if h.old_start else 0
        expected = [l[1:].replace("\\NOEOL", "") for l in h.lines if l[:1] in (" ", "-")]
        # tolerate small offsets like `patch` does
        found = None
        for delta in range(0, 200):
            for cand in (start + delta, start - delta):
                if cand >= pos and src[cand:cand + len(expected)] == expected:
                    found = cand
                    break
            if found is not None:
                break
        if found is None:
            raise ApplyError(f"hunk at {h.old_start} does not apply to {fd.path}")
        out.extend(src[pos:found])
        pos = found
        for l in h.lines:
            tag, body = l[:1], l[1:]
            noeol = body.endswith("\\NOEOL")
            body = body.replace("\\NOEOL", "")
            if tag == " ":
                out.append(src[pos])
                pos += 1
                if noeol:
                    new_eol = False
            elif tag == "-":
                pos += 1
            else:
                out.append(body)
                new_eol = not noeol
        if any(l.endswith("\\NOEOL") and l[:1] == "-" for l in h.lines) and not any(
            l[:1] in " +" and l.endswith("\\NOEOL") for l in h.lines
        ):
            new_eol = True
    out.extend(src[pos:])
    if pos < len(src):
        new_eol = trailing_nl
    text = "\n".join(out)
    return text + "\n" if new_eol and out else text
