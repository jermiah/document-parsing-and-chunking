"""Count actual model input, including special tokens; never silently truncate."""

from pathlib import Path

from tokenizers import Tokenizer

from ingestion.storage import file_hash


class TokenBudget:
    def __init__(self, path: Path, cap: int):
        if not path.is_file():
            raise RuntimeError(f"Missing tokenizer {path}. Run doc-ingest download-models first.")
        self.tokenizer = Tokenizer.from_file(str(path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cap = cap
        self.identity = "sha256:" + file_hash(path)

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=True).ids)

    def split(self, text: str, prefix: str = "") -> list[tuple[str, int, int]]:
        """Prefer sentence/line boundaries; fall back to character spans without dropping text."""
        if self.count(prefix) >= self.cap - 4:
            raise ValueError("Context prefix exhausts token budget")
        result = []
        start = 0
        while start < len(text):
            low, high = start + 1, len(text)
            end = start
            while low <= high:
                mid = (low + high) // 2
                if self.count(prefix + text[start:mid]) <= self.cap:
                    end, low = mid, mid + 1
                else:
                    high = mid - 1
            if end == start:
                raise ValueError("A source character cannot fit the configured budget")
            if end < len(text):
                boundary = max(
                    text.rfind("\n", start, end),
                    text.rfind(". ", start, end),
                    text.rfind(" ", start, end),
                )
                if boundary > start + (end - start) // 2:
                    end = boundary + 1
            result.append((text[start:end], start, end))
            start = end
        return result
