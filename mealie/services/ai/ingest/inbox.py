"""
The inbox folder (docs/ai/PHASE2.md §1.3): `AI_INGEST_INBOX_DIR/<group-slug>/<household-slug>/`, scanned by the
dispatcher. Files are taken once they've settled, claimed by an atomic rename, opened once with `O_NOFOLLOW` and passed
to intake, then moved to `processed/` (or `failed/` with the reason).

The signature is final. Work item B2 provides the scanner; until then nothing is scanned.
"""


def scan_once() -> int:
    """One scan of every household folder (skipped while paused): the number of files ingested"""
    return 0
