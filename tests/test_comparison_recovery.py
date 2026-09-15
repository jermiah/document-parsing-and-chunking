import json

from ingestion.config import Settings
from ingestion.jobs import restore_comparison_inputs
from ingestion.storage import atomic_write, confined


def test_comparison_restores_required_inputs_from_remote_cache(tmp_path):
    settings = Settings(_env_file=None, artifact_dir=tmp_path / "empty-cache")
    key = "runs/document/batch"
    objects = {
        key + "/canonical.json": json.dumps({"assets": [{"key": "assets/figure.png"}]}).encode(),
        key + "/manifest.json": b"{}",
        key + "/assets/figure.png": b"image bytes",
        key + "/raw/docling_document.json": b"{}",
    }
    fetched = []

    class RemoteCache:
        enabled = True

        def get(self, root, object_key, prefix):
            assert prefix == "artifacts"
            path = confined(root, object_key)
            if not path.exists():
                fetched.append(object_key)
                atomic_write(path, objects[object_key])
            return path

    cloud = RemoteCache()
    paths = restore_comparison_inputs(settings, cloud, [key])
    assert paths == [settings.artifact_dir / key]
    assert set(fetched) == set(objects)
    assert (paths[0] / "assets/figure.png").read_bytes() == b"image bytes"
    restore_comparison_inputs(settings, cloud, [key])
    assert len(fetched) == 4
