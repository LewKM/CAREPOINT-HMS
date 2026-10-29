"""Build app/static/icons.svg — a sprite of just the Lucide icons the app uses.

    curl -sfLO https://unpkg.com/lucide-static@0.469.0/icon-nodes.json
    python -m scripts.build_icons icon-nodes.json

Re-run after using a new icon name. Any quoted string in the templates, Python or JS that
names a Lucide icon is included, so names passed through variables (the NAV table) are found too.
"""
import json
import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "app"
SOURCES = [*ROOT.glob("templates/**/*.html"), *ROOT.glob("*.py"), *ROOT.glob("routers/*.py"), *ROOT.glob("static/js/*.js")]


def main(nodes_path: str) -> None:
    nodes = json.loads(Path(nodes_path).read_text())
    found = set()
    for src in SOURCES:
        found.update(re.findall(r"""["']([a-z][a-z0-9-]*)["']""", src.read_text()))
    names = sorted(found & nodes.keys())
    symbols = []
    for name in names:
        body = "".join(f"<{tag} " + " ".join(f'{k}="{escape(str(v))}"' for k, v in attrs.items()) + "/>"
                       for tag, attrs in nodes[name])
        symbols.append(f'<symbol id="{name}" viewBox="0 0 24 24">{body}</symbol>')
    out = ROOT / "static" / "icons.svg"
    out.write_text('<svg xmlns="http://www.w3.org/2000/svg">\n' + "\n".join(symbols) + "\n</svg>\n")
    print(f"{len(names)} icons → {out.relative_to(ROOT.parent)} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "icon-nodes.json")
