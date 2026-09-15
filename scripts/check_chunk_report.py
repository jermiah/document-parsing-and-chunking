"""Check local HTML image/file links and source anchors without a browser or network."""

import argparse
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


def check(root: Path) -> dict:
    problems = []
    targets: dict[Path, set[str]] = {}
    checked = 0
    for page in root.rglob("*.html"):
        soup = BeautifulSoup(page.read_text("utf-8"), "html.parser")
        for element in soup.select("a[href], img[src]"):
            url = urlsplit(element.get("href", element.get("src", "")))
            if url.scheme or url.netloc:
                continue
            target = (page.parent / unquote(url.path)).resolve() if url.path else page.resolve()
            checked += 1
            if not target.is_file():
                problems.append(f"Missing file: {target}")
            elif url.fragment and target.suffix == ".html":
                if target not in targets:
                    parsed = BeautifulSoup(target.read_text("utf-8"), "html.parser")
                    targets[target] = {str(tag["id"]) for tag in parsed.select("[id]")}
                if unquote(url.fragment) not in targets[target]:
                    problems.append(f"Missing anchor: {target}#{url.fragment}")
    result = {"checked_links": checked, "problems": sorted(set(problems))}
    if problems:
        raise ValueError(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_directory", type=Path)
    print(json.dumps(check(parser.parse_args().report_directory), indent=2))
