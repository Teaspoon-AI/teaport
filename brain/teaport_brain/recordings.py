#
# recordings.py — where conversation recordings go, and how long they stay (#114).
#
# Two writers keep files of calls on disk: the owner's call recording (call_recording.py,
# TEAPORT_CALL_RECORDING) and the diagnostic caller-audio dump (audio_dump.py,
# TEAPORT_AUDIO_DUMP). Both name their files here (free_stem), and both prune their
# directory here (prune): files older than a number of days go, then the oldest until
# the directory is under its size cap. The dump had no cap and reached 2.1 GB on the
# appliance; a box that records calls must not fill its disk with them.
#
# A recording is a GROUP of files sharing a stem: a call's .wav and .txt, a dump's .wav
# and .marks, and a resumed call's .r1, .r2 parts (free_stem). Pruning takes or keeps a
# group whole, by the newest file in it, so a call never loses half of itself. Only
# files whose names match the writer's own pattern are touched: a directory the owner
# points at may hold other files, and those are not ours to delete.
#
import asyncio
import os
import re
import time

from loguru import logger

# The extensions our writers create; nothing else in a recordings directory is touched.
_OURS = (".wav", ".txt", ".marks")


def free_stem(stem: str, extensions=(".wav", ".marks", ".txt")) -> str:
    """`stem`, or `stem.r1`, `stem.r2`, ... -- the first with no file of `extensions`
    on disk. Never truncate an existing recording: the same call recorded twice is a call
    resumed by a restarted brain, and the earlier file is its first half."""
    candidate, n = stem, 0
    while any(os.path.exists(candidate + ext) for ext in extensions):
        n += 1
        candidate = f"{stem}.r{n}"
    return candidate


def _groups(directory: str, pattern: re.Pattern) -> dict:
    """{stem: [(path, size, mtime), ...]} for our files in `directory`; the stem is the
    name up to its first dot, so a call's parts and sidecars are one group."""
    groups: dict = {}
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return groups
    for e in entries:
        if not e.is_file(follow_symlinks=False) or not e.name.endswith(_OURS):
            continue
        if not pattern.match(e.name):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        groups.setdefault(e.name.split(".", 1)[0], []).append((e.path, st.st_size, st.st_mtime))
    return groups


def prune(directory: str, keep_days: float, max_mb: float, pattern: re.Pattern,
          keep: frozenset = frozenset(), now: float | None = None) -> list:
    """Delete our recordings in `directory` older than `keep_days` (0: no age limit), then
    the oldest until the rest fit in `max_mb` (0: no cap). `keep`: stems never deleted
    (the recording being written). The deleted stems, oldest first."""
    if not directory or (keep_days <= 0 and max_mb <= 0):
        return []
    now = time.time() if now is None else now
    groups = _groups(directory, pattern)
    # Oldest first, by each group's newest file: a long call's first part is as old as
    # its last.
    order = sorted(groups, key=lambda s: max(m for _, _, m in groups[s]))
    doomed = []
    if keep_days > 0:
        cutoff = now - keep_days * 86400
        doomed = [s for s in order
                  if s not in keep and max(m for _, _, m in groups[s]) < cutoff]
    if max_mb > 0:
        total = sum(size for s in order if s not in doomed for _, size, _ in groups[s])
        for s in order:
            if total <= max_mb * 1024 * 1024:
                break
            if s in keep or s in doomed:
                continue
            doomed.append(s)
            total -= sum(size for _, size, _ in groups[s])
    for s in doomed:
        for path, _, _ in groups[s]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning(f"recordings: could not delete {path} ({e})")
    if doomed:
        logger.info(f"recordings: pruned {len(doomed)} old recording(s) from {directory}")
    return doomed


def prune_soon(directory: str, keep_days: float, max_mb: float, pattern: re.Pattern,
               keep: frozenset = frozenset()) -> None:
    """prune(), off the event loop when one is running (a directory of thousands of
    files is a scan and a stat each), inline otherwise. Never raises."""
    def run():
        try:
            prune(directory, keep_days, max_mb, pattern, keep)
        except Exception as e:  # noqa: BLE001 -- pruning must never cost a call
            logger.warning(f"recordings: pruning {directory} failed ({e!r})")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run()
        return
    loop.run_in_executor(None, run)
