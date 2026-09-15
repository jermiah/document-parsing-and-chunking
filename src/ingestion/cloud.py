"""Private Supabase object storage, with local files used as a read-through cache."""

import mimetypes
from pathlib import Path
from urllib.parse import quote

import httpx

from ingestion.config import Settings
from ingestion.storage import atomic_write, confined


class CloudFiles:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.storage_backend == "supabase"
        if self.enabled and (
            not settings.supabase_url.startswith("https://")
            or not settings.supabase_secret_key.get_secret_value()
        ):
            raise ValueError("Supabase storage needs SUPABASE_URL and SUPABASE_SECRET_KEY")

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        key = self.settings.supabase_secret_key.get_secret_value()
        headers = {"apikey": key}
        if not key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + key
        headers.update(kwargs.pop("headers", {}))
        try:
            response = httpx.request(
                method,
                self.settings.supabase_url.rstrip("/") + "/storage/v1/" + path,
                headers=headers,
                timeout=120,
                **kwargs,
            )
            if response.status_code == 400:
                # Hosted Storage can wrap a missing-object 404 in HTTP 400.
                error = response.json()
                if (
                    error.get("code") in {"NoSuchBucket", "NoSuchKey"}
                    or str(error.get("statusCode")) == "404"
                ):
                    response.status_code = 404
            if response.status_code not in (200, 201, 404):
                raise RuntimeError(f"Cloud storage returned HTTP {response.status_code}")
            return response
        except httpx.HTTPError:
            raise RuntimeError("Cloud storage could not be reached") from None

    def ensure_bucket(self):
        if not self.enabled:
            return
        bucket = self.settings.supabase_bucket
        result = self.request("GET", "bucket/" + quote(bucket, safe=""))
        if result.status_code == 404:
            result = self.request(
                "POST", "bucket", json={"id": bucket, "name": bucket, "public": False}
            )
        if result.status_code == 404 or result.json().get("public", False):
            raise RuntimeError("Use an existing private Supabase storage bucket")

    def put(self, key: str, path: Path):
        if not self.enabled:
            return
        with path.open("rb") as file:
            response = self.request(
                "POST",
                "object/"
                + quote(self.settings.supabase_bucket, safe="")
                + "/"
                + quote(key, safe="/"),
                content=file,
                headers={
                    "x-upsert": "true",
                    "Content-Length": str(path.stat().st_size),
                    "Content-Type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                },
            )
        if response.status_code == 404:
            raise RuntimeError("Supabase storage bucket is missing")

    def get(self, root: Path, key: str, prefix: str) -> Path:
        path = confined(root, key)
        if not path.is_file() and self.enabled:
            response = self.request(
                "GET",
                "object/"
                + quote(self.settings.supabase_bucket, safe="")
                + "/"
                + quote(prefix + "/" + key, safe="/"),
            )
            if response.status_code != 404:
                atomic_write(path, response.content)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path

    def publish(self, original: Path, original_key: str, output: Path, artifact_key: str):
        if not self.enabled:
            return
        self.put("data/" + original_key, original)
        # Publish HTML last, after its supporting files.
        files = sorted(p for p in output.rglob("*") if p.is_file())
        files.sort(key=lambda p: p.name == "index.html")
        for path in files:
            self.put("artifacts/" + artifact_key + "/" + path.relative_to(output).as_posix(), path)

    def remove_artifact_tree(self, artifact_key: str):
        """List only one verified run prefix, then delete objects through the Storage API."""
        parts = artifact_key.split("/")
        if len(parts) != 3 or parts[0] != "runs" or any(p in {"", ".", ".."} for p in parts):
            raise ValueError("Expected a run artifact directory")
        if not self.enabled:
            return
        bucket = quote(self.settings.supabase_bucket, safe="")
        prefix = "artifacts/" + artifact_key
        pending, objects = [prefix], []
        while pending:
            directory = pending.pop()
            offset = 0
            while True:
                response = self.request(
                    "POST",
                    "object/list/" + bucket,
                    json={
                        "prefix": directory + "/",
                        "limit": 100,
                        "offset": offset,
                        "sortBy": {"column": "name", "order": "asc"},
                    },
                )
                if response.status_code == 404:
                    raise RuntimeError("Storage bucket unavailable during cleanup")
                rows = response.json()
                if not isinstance(rows, list):
                    raise RuntimeError("Invalid storage listing")
                for row in rows:
                    name = row["name"]
                    if not name or name in {".", ".."} or "/" in name or "\\" in name:
                        raise ValueError("Invalid storage object name")
                    key = directory + "/" + name
                    if row.get("id") is None:
                        pending.append(key)
                    else:
                        objects.append(key)
                if len(rows) < 100:
                    break
                offset += len(rows)
        for offset in range(0, len(objects), 100):
            response = self.request(
                "DELETE", "object/" + bucket, json={"prefixes": objects[offset : offset + 100]}
            )
            if response.status_code == 404:
                raise RuntimeError("Storage bucket unavailable during cleanup")
