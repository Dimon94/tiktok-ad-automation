"""观察时点决定快照新旧；发布版本仅为同一时点的幂等顺序。"""

from app.modules.sync_control.contracts import PublicationVersion


def compare_publish_version(
    stored: PublicationVersion | None, candidate: PublicationVersion
) -> bool:
    return stored is None or (candidate.observed_at, candidate.version) >= (
        stored.observed_at,
        stored.version,
    )
