"""The on-disk handoff between a baked conditioning file and its registry entry."""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from .backends.base import _replace, validate_voice_id
from .backends.info import BACKENDS

log = logging.getLogger("engine")


class ConditioningCommit:
    """A prepare record beside the registry, owned by its existing cross-process lock.

    The old .pt stays recoverable until the registry rename succeeds. The registry's
    prepared image disappears in that rename, so recovery can tell exactly which side
    of the commit it is on, even if both versions have identical metadata. Neither a
    retry nor another voice's update has to infer the decision from entry contents.
    """

    def __init__(self, registry: Path):
        self.registry = registry
        self.marker = registry.with_name(registry.name + ".conditioning")
        self.preparing = self.marker.with_suffix(".conditioning-preparing")

    def _files(self, voice: str, backend: str, tx: str) -> tuple[Path, Path, Path, Path]:
        validate_voice_id(voice)
        if (not isinstance(backend, str) or backend not in BACKENDS or
                not isinstance(tx, str) or not re.fullmatch(r"[0-9a-f]{32}", tx)):
            raise ValueError("Invalid conditioning transaction backend or identity")
        folder = self.registry.parent / backend
        if folder.resolve() != self.registry.parent.resolve() / backend:
            raise ValueError(f"Conditioning transaction folder points outside its expected location: {folder}")
        target = folder / f"{voice}.pt"
        backup = folder / f"{voice}.pt.rollback-{tx}"
        new = folder / f"{voice}.pt.pending-{tx}"
        image = self.registry.with_name(f"{self.registry.name}.pending-{tx}")
        for path in (target, backup, new, image):
            if path.resolve() != path.parent.resolve() / path.name:
                raise ValueError(f"Unexpected redirected conditioning transaction file: {path}")
        return target, backup, new, image

    def publish(self, voice: str, backend: str, stage: Path, data: dict) -> None:
        """Called with the registry lock held and all previous transactions recovered."""
        tx = stage.name.rsplit("-", 1)[-1]
        target, backup, new, image = self._files(voice, backend, tx)
        if (stage.parent != target.parent or stage.resolve() != target.parent.resolve() / stage.name or
                not re.fullmatch(re.escape(target.name) + r"\.stage-\d+-[0-9a-f]{32}", stage.name)):
            raise ValueError("Unexpected staging path for conditioning transaction")
        had_old = target.exists()
        # A killed preparation can leave scratch files, but cannot touch either live file.
        # Publish the marker BEFORE creating those files so recovery can clean them too.
        pid = int(stage.name.rsplit("-", 2)[-2])
        record = dict(version=1, voice=voice, backend=backend, tx=tx, pid=pid, had_old=had_old)
        committed = False
        try:
            # Exclusive creation avoids writing through an unexpected link at this name.
            with self.preparing.open("x", encoding="utf-8") as f:
                json.dump(record, f)
            _replace(self.preparing, self.marker)
        finally:
            self.preparing.unlink(missing_ok=True)
        try:
            with image.open("x", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            if had_old:
                # Keep the live path present throughout publication. A killed fallback copy
                # leaves an incomplete scratch file, never an apparently valid backup.
                try:
                    backup.hardlink_to(target)
                except OSError:
                    shutil.copy2(target, new)
                    _replace(new, backup)
            _replace(stage, new)
            _replace(new, target)
            _replace(image, self.registry)
            committed = True
        finally:
            try:
                self.recover()
            except OSError:
                if not committed:
                    raise
                log.warning("Committed conditioning; cleanup will retry at %s", self.marker, exc_info=True)

    def recover(self) -> None:
        """Recover under the registry lock. A second kill during recovery is safe."""
        try:
            raw = self.marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        if self.marker.is_symlink():
            raise ValueError(f"Unexpected link at conditioning transaction: {self.marker}")
        rec = json.loads(raw)
        if (not isinstance(rec, dict) or type(rec.get("version")) is not int or rec["version"] != 1 or
                type(rec.get("had_old")) is not bool or
                type(rec.get("pid")) is not int or not 0 < rec["pid"] <= 2**32 - 1):
            raise ValueError(f"Invalid conditioning transaction in {self.marker}")
        target, backup, new, image = self._files(rec.get("voice"), rec.get("backend"), rec.get("tx"))
        stage = target.with_name(f"{target.name}.stage-{rec['pid']}-{rec['tx']}")
        if image.exists():
            # Missing backup means the original never moved or an earlier recovery restored it.
            if rec["had_old"] and backup.exists():
                _replace(backup, target)
            elif not rec["had_old"] and target.exists():
                target.unlink()
        for leftover in (backup, new, stage, image):
            leftover.unlink(missing_ok=True)
        self.marker.unlink()
