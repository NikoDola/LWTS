"""Build a compact Italian->English lookup DB from a Wiktionary (kaikki.org) dump.

The kaikki.org "Italian" extraction is one JSON object per line, each a single
word entry with English `senses[].glosses`, usage `examples`, and (for nouns)
gender in the `it-noun` head template. We distill that ~750 MB dump into a small
SQLite file keyed by lemma, used at runtime by `dictionary.py` for instant,
offline, free word lookups (no LLM).

Usage (one-time, from the project root):
    python -m backend.build_dict                 # downloads the dump if missing
    python -m backend.build_dict --src foo.jsonl # use an already-downloaded dump

Source: https://kaikki.org/dictionary/Italian/  (Wiktionary, CC-BY-SA)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DUMP_URL = "https://kaikki.org/dictionary/Italian/kaikki.org-dictionary-Italian.jsonl"
DEFAULT_SRC = DATA_DIR / "kaikki.org-dictionary-Italian.jsonl"
DEFAULT_OUT = DATA_DIR / "it_en.sqlite"

MAX_GLOSSES = 4   # senses (meanings) to keep per entry (the popup shows a few)


def _download(url: str, dest: Path) -> None:
    """Stream the (large) dump to disk with a simple progress readout."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}\n  -> {dest}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  {got / total:5.1%} ({got >> 20} / {total >> 20} MB)",
                          end="", flush=True)
        print()


def _gender(entry: dict) -> str:
    """Italian noun gender from the it-noun head template, or '-'."""
    for h in entry.get("head_templates", []) or []:
        if h.get("name") == "it-noun":
            g = str((h.get("args") or {}).get("1", "")).lower()
            if g.startswith("f"):
                return "feminine"
            if g.startswith("mf"):
                return "masculine or feminine"
            if g.startswith("m"):
                return "masculine"
    return "-"


def _extract(entry: dict):
    """Pull (senses, form_of, tags) from a kaikki entry, or None.

    `senses` is a list of {"g": gloss, "ex": example, "en": english} — one per
    distinct meaning (e.g. come -> how / as / like), each with its OWN example so
    the popup can show every meaning and an example for each.

    `form_of` is the base lemma when this entry is an inflected/derived form
    (e.g. vivrò -> vivere), so runtime can follow it to the real gloss. `tags`
    are that form's grammatical tags (first-person, future, plural, ...).
    """
    senses: list[dict] = []
    seen: set[str] = set()
    form_of = ""
    tags: list[str] = []
    for sense in entry.get("senses", []) or []:
        gl = sense.get("glosses") or []
        gloss = gl[0].strip() if gl else ""
        if gloss and gloss not in seen:
            seen.add(gloss)
            ex = en = ""
            for e in sense.get("examples", []) or []:
                if not e.get("text"):
                    continue
                t = e["text"].strip()
                g = (e.get("english") or e.get("translation") or "").strip()
                if g:                      # prefer an example that has a translation
                    ex, en = t, g
                    break
                if not ex:                 # else keep the first text-only example
                    ex = t
            senses.append({"g": gloss, "ex": ex, "en": en})
        if not form_of:
            fo = sense.get("form_of") or sense.get("alt_of")
            if isinstance(fo, list) and fo and fo[0].get("word"):
                form_of = fo[0]["word"].lower()
                tags = sense.get("tags") or []
        if len(senses) >= MAX_GLOSSES and form_of:
            break
    if not senses:
        return None
    return senses[:MAX_GLOSSES], form_of, tags


def build(src: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    con = sqlite3.connect(out)
    con.execute(
        "CREATE TABLE entries (lemma TEXT, pos TEXT, senses TEXT, "
        "gender TEXT, form_of TEXT, tags TEXT)"
    )

    rows = []
    kept = seen = 0
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            seen += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("lang_code") != "it" or not entry.get("word"):
                continue
            got = _extract(entry)
            if got is None:
                continue
            senses, form_of, tags = got
            rows.append((
                entry["word"].lower(),
                entry.get("pos", ""),
                json.dumps(senses, ensure_ascii=False),
                _gender(entry),
                form_of, json.dumps(tags, ensure_ascii=False),
            ))
            kept += 1
            if len(rows) >= 5000:
                con.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?)", rows)
                rows.clear()
            if seen % 100000 == 0:
                print(f"\r  scanned {seen:,} entries, kept {kept:,}", end="", flush=True)

    if rows:
        con.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?)", rows)
    con.execute("CREATE INDEX idx_lemma ON entries(lemma)")
    con.commit()
    con.close()
    print(f"\nBuilt {out} — {kept:,} entries from {seen:,} scanned.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Italian->English lookup DB.")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help="kaikki.org Italian JSONL dump (downloaded if missing)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output SQLite path")
    args = ap.parse_args()

    if not args.src.is_file():
        _download(DUMP_URL, args.src)
    build(args.src, args.out)


if __name__ == "__main__":
    main()
