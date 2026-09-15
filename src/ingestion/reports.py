"""One report model produces all three formats; unmeasured stages stay explicit."""

import html
import json
from pathlib import Path

from ingestion.export import shell
from ingestion.storage import atomic_write, write_json


def write_report(output: Path, report: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "metrics.json", report)
    title = report.get("title", "Experiment report")
    markdown = f"# {title}\n\n"
    for key, value in report.items():
        if key != "title":
            markdown += f"## {key.replace('_', ' ').title()}\n\n```json\n{json.dumps(value, indent=2, ensure_ascii=False)}\n```\n\n"
    atomic_write(output / "report.md", markdown.encode())
    atomic_write(
        output / "report.html", shell(title, "<pre>" + html.escape(markdown) + "</pre>").encode()
    )
