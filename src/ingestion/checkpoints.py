"""Check saved run compatibility before accepting a resume request."""

from ingestion.config import ProcessingConfig


class CheckpointCompatibilityError(ValueError):
    pass


def saved_config(run) -> ProcessingConfig:
    """Never mix batches generated with different settings or model assets."""
    try:
        config = ProcessingConfig.model_validate(run.metrics["config"])
        compatible = config.fingerprint() == run.config_hash
    except (KeyError, ValueError, OSError):
        compatible = False
    if not compatible:
        saved = run.metrics.get("completed_pages", 0)
        detail = (
            f"The {saved} saved page passes remain available in run history."
            if saved
            else "This run has no saved checkpoint pages."
        )
        raise CheckpointCompatibilityError(
            "This run was created with a different processing configuration, application policy "
            "or model assets and cannot resume with the current installation. "
            + detail
            + " Use Reprocess to start a new run with the current settings."
        )
    return config


def resume_block_reason(run) -> str | None:
    if run.metrics.get("deletion_requested"):
        return "This run is being deleted and cannot be resumed."
    if run.metrics.get("job_version") != 1:
        return "This older run has no checkpoint support. Use Reprocess."
    if run.status not in {"failed", "paused"}:
        return "Only failed or paused runs can be resumed."
    try:
        saved_config(run)
    except CheckpointCompatibilityError as exc:
        return str(exc)
    return None
