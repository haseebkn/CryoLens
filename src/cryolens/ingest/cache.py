"""Local disk cache management with LRU eviction policy and byte tracking."""

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Metadata for a cached item on disk."""

    key: str
    path: Path
    size_bytes: int
    last_accessed: float
    pinned: bool = False


class LocalCacheManager:
    """Manages local storage under data/cache and data/raw with LRU eviction."""

    def __init__(
        self,
        cache_dir: Path | str = "./data/cache",
        max_size_bytes: int = 20 * 1024 * 1024 * 1024,  # 20 GB default limit
    ) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = max_size_bytes
        self.total_bytes_transferred: int = 0
        self._pinned_keys: set[str] = set()

    def get_path(self, key: str, subdirectory: str | None = None) -> Path:
        """Resolve expected disk path for a given cache key."""
        target_dir = self.cache_dir if subdirectory is None else self.cache_dir / subdirectory
        target_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize key for filesystem
        safe_key = key.replace("/", "_").replace(":", "_")
        return target_dir / safe_key

    def contains(self, key: str, subdirectory: str | None = None) -> bool:
        """Check if an item exists and is non-empty in the cache."""
        path = self.get_path(key, subdirectory)
        if not path.exists():
            return False
        if path.is_file() and path.stat().st_size > 0:
            self._touch(path)
            return True
        if path.is_dir() and any(path.iterdir()):
            self._touch(path)
            return True
        return False

    def pin(self, key: str) -> None:
        """Mark an item as pinned so LRU eviction will not remove it."""
        self._pinned_keys.add(key)

    def unpin(self, key: str) -> None:
        """Unpin an item allowing eviction if storage thresholds are exceeded."""
        self._pinned_keys.discard(key)

    def record_transfer(self, bytes_count: int) -> None:
        """Track data volume transferred across the network."""
        self.total_bytes_transferred += bytes_count
        logger.info(
            "Transferred %d bytes. Total network volume: %.2f MB",
            bytes_count,
            self.total_bytes_transferred / (1024 * 1024),
        )

    def get_total_usage_bytes(self) -> int:
        """Calculate total size of all files currently stored in cache."""
        total = 0
        for root, _, files in os.walk(self.cache_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
        return total

    def evict_if_needed(self) -> int:
        """Evict least-recently-accessed unpinned files if total size exceeds limit."""
        current_usage = self.get_total_usage_bytes()
        if current_usage <= self.max_size_bytes:
            return 0

        logger.warning(
            "Cache size (%.2f GB) exceeds limit (%.2f GB). Initiating LRU eviction.",
            current_usage / (1024**3),
            self.max_size_bytes / (1024**3),
        )

        entries: list[CacheEntry] = []
        for item in self.cache_dir.iterdir():
            if item.name.startswith(".") or item.name in self._pinned_keys:
                continue

            try:
                if item.is_file():
                    stat = item.stat()
                    entries.append(
                        CacheEntry(
                            key=item.name,
                            path=item,
                            size_bytes=stat.st_size,
                            last_accessed=stat.st_atime,
                        )
                    )
                elif item.is_dir():
                    dir_size = sum(
                        os.path.getsize(os.path.join(r, f))
                        for r, _, files in os.walk(item)
                        for f in files
                    )
                    stat = item.stat()
                    entries.append(
                        CacheEntry(
                            key=item.name,
                            path=item,
                            size_bytes=dir_size,
                            last_accessed=stat.st_atime,
                        )
                    )
            except OSError:
                continue

        # Sort ascending by last accessed time (oldest first)
        entries.sort(key=lambda e: e.last_accessed)

        bytes_reclaimed = 0
        for entry in entries:
            if current_usage - bytes_reclaimed <= self.max_size_bytes:
                break
            try:
                if entry.path.is_file():
                    entry.path.unlink()
                elif entry.path.is_dir():
                    shutil.rmtree(entry.path)
                bytes_reclaimed += entry.size_bytes
                logger.info(
                    "Evicted %s (reclaimed %.2f MB)", entry.key, entry.size_bytes / (1024**2)
                )
            except OSError as exc:
                logger.error("Failed to evict %s: %s", entry.path, exc)

        return bytes_reclaimed

    def _touch(self, path: Path) -> None:
        """Update access timestamp on file or directory."""
        try:
            now = time.time()
            os.utime(path, (now, now))
        except OSError:
            pass
