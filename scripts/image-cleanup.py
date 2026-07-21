#!/usr/bin/env python3
"""
Crictl Image Cleanup
=====================
Menghapus image containerd yang:
  1. TIDAK dirujuk oleh container manapun (running maupun exited), DAN
  2. Sudah lebih tua dari `cleanup.retention_days` (default 30 hari)

"Umur" image didekati dari mtime file blob-nya di content store containerd,
karena CRI/crictl tidak menyimpan timestamp pull secara langsung.

Exit codes:
    0 Success
    1 Config Error
    2 Failed to list images (crictl images)
    3 Failed to list containers (crictl ps -a)
    4 One or more image removals failed
    5 Unexpected error
"""

import os
import sys
import json
import time
import logging
import fcntl
import subprocess
import errno
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML. Install with: sudo apt install python3-yaml", file=sys.stderr)
    sys.exit(5)


class ExitCode:
    SUCCESS = 0
    CONFIG_ERROR = 1
    LIST_IMAGES_FAILED = 2
    LIST_CONTAINERS_FAILED = 3
    REMOVAL_FAILED = 4
    UNEXPECTED_ERROR = 5


class CleanupError(Exception):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


# -----------------------------------------------------------------------
# JSON structured logging (pola sama dengan etcd-backup-service)
# -----------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key in ("action", "status", "image", "age_days", "size_bytes"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(cfg):
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "/var/log/image-cleanup/image-cleanup.log")
    level_name = log_cfg.get("level", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("image-cleanup")
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = JsonFormatter()
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def load_config(config_path):
    try:
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise CleanupError(f"Config file not found: {config_path}", ExitCode.CONFIG_ERROR) from e
    except yaml.YAMLError as e:
        raise CleanupError(f"Invalid YAML config: {e}", ExitCode.CONFIG_ERROR) from e

    if not cfg:
        raise CleanupError("Config file is empty", ExitCode.CONFIG_ERROR)

    required_sections = ["cleanup", "crictl", "containerd", "lock", "logging"]
    for section in required_sections:
        if section not in cfg:
            raise CleanupError(f"Missing config section: {section}", ExitCode.CONFIG_ERROR)

    return cfg


# -----------------------------------------------------------------------
# Lock mechanism
# -----------------------------------------------------------------------
class ProcessLock:
    def __init__(self, lock_path, logger):
        self.lock_path = lock_path
        self.logger = logger
        self._fd = None

    def acquire(self):
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.lock_path, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise CleanupError(
                    f"Another cleanup process is already running (lock: {self.lock_path})",
                    ExitCode.UNEXPECTED_ERROR,
                ) from e
            raise
        self._fd.write(str(os.getpid()))
        self._fd.flush()
        self.logger.info("Lock acquired", extra={"action": "lock_acquire", "status": "ok"})

    def release(self):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
                Path(self.lock_path).unlink(missing_ok=True)
                self.logger.info("Lock released", extra={"action": "lock_release", "status": "ok"})
            except Exception:
                self.logger.warning("Failed to cleanly release lock", extra={"action": "lock_release", "status": "warning"})


# -----------------------------------------------------------------------
# crictl helpers
# -----------------------------------------------------------------------
def run_crictl(args, cfg, timeout=None):
    crictl_path = cfg["crictl"]["path"]
    timeout = timeout or cfg["crictl"].get("command_timeout_seconds", 60)
    try:
        return subprocess.run([crictl_path] + args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise CleanupError(f"crictl binary not found: {crictl_path}", ExitCode.UNEXPECTED_ERROR) from e


def normalize_digest(ref):
    """Buang prefix 'sha256:' kalau ada, supaya perbandingan konsisten."""
    if ref and ref.startswith("sha256:"):
        return ref[len("sha256:"):]
    return ref


def list_images(cfg, logger):
    result = run_crictl(["images", "-o", "json"], cfg)
    if result.returncode != 0:
        raise CleanupError(f"crictl images failed: {result.stderr.strip()}", ExitCode.LIST_IMAGES_FAILED)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CleanupError(f"crictl images returned invalid JSON: {e}", ExitCode.LIST_IMAGES_FAILED) from e

    images = data.get("images", [])
    logger.info(f"Found {len(images)} image(s) total", extra={"action": "list_images", "status": "ok"})
    return images


def list_used_image_ids(cfg, logger):
    """Kumpulkan image ID yang dirujuk container manapun (running + exited),
    supaya image yang masih dipakai TIDAK PERNAH jadi kandidat hapus."""
    result = run_crictl(["ps", "-a", "-o", "json"], cfg)
    if result.returncode != 0:
        raise CleanupError(f"crictl ps -a failed: {result.stderr.strip()}", ExitCode.LIST_CONTAINERS_FAILED)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CleanupError(f"crictl ps -a returned invalid JSON: {e}", ExitCode.LIST_CONTAINERS_FAILED) from e

    containers = data.get("containers", [])
    used_ids = set()
    for c in containers:
        image_ref = c.get("imageRef") or (c.get("image") or {}).get("image")
        if image_ref:
            used_ids.add(normalize_digest(image_ref))

    logger.info(
        f"Found {len(containers)} container(s), {len(used_ids)} distinct image(s) in use",
        extra={"action": "list_used_images", "status": "ok"},
    )
    return used_ids


def get_image_age_days(image_id, cfg, logger):
    """Proxy umur image lewat mtime blob-nya di content store containerd.
    Return None kalau blob tidak ditemukan (age tidak diketahui)."""
    content_dir = Path(cfg["containerd"]["content_dir"])
    blob_path = content_dir / normalize_digest(image_id)

    if not blob_path.exists():
        logger.warning(
            f"Content blob not found for image {image_id}, age unknown - skipping for safety",
            extra={"action": "age_check", "status": "warning", "image": image_id},
        )
        return None

    mtime = blob_path.stat().st_mtime
    age_days = (time.time() - mtime) / 86400
    return round(age_days, 1)


def is_protected(image, patterns):
    """Cek apakah salah satu repoTags/repoDigests image cocok dengan
    pattern yang dilindungi (case-insensitive substring match)."""
    refs = (image.get("repoTags") or []) + (image.get("repoDigests") or [])
    refs_lower = [r.lower() for r in refs]
    for pattern in patterns:
        pattern_lower = pattern.lower()
        if any(pattern_lower in ref for ref in refs_lower):
            return True
    return False


def remove_image(image, cfg, logger, max_retries=2, retry_delay_seconds=5):
    """crictl rmi kadang gagal dengan DeadlineExceeded/RST_STREAM saat node
    sedang sibuk atau image berlayer banyak - biasanya transient, jadi
    dicoba beberapa kali sebelum benar-benar dianggap gagal."""
    ref = (image.get("repoTags") or image.get("repoDigests") or [image["id"]])[0]

    attempt = 0
    last_stderr = ""
    while attempt <= max_retries:
        attempt += 1
        result = run_crictl(["rmi", image["id"]], cfg)
        if result.returncode == 0:
            logger.info(
                f"Removed image: {ref}",
                extra={"action": "remove_image", "status": "ok", "image": ref, "size_bytes": image.get("size", 0)},
            )
            return True

        last_stderr = result.stderr.strip()
        is_transient = "deadlineexceeded" in last_stderr.lower() or "rst_stream" in last_stderr.lower()

        if is_transient and attempt <= max_retries:
            logger.warning(
                f"Transient failure removing {ref} (attempt {attempt}/{max_retries + 1}), retrying: {last_stderr}",
                extra={"action": "remove_image", "status": "retry", "image": ref},
            )
            time.sleep(retry_delay_seconds)
            continue

        break

    logger.error(
        f"Failed to remove image {ref}: {last_stderr}",
        extra={"action": "remove_image", "status": "failed", "image": ref},
    )
    return False


# -----------------------------------------------------------------------
# Prometheus metrics (node_exporter textfile collector)
# -----------------------------------------------------------------------
def write_metrics(cfg, metrics, logger):
    prom_cfg = cfg.get("prometheus", {})
    directory = Path(prom_cfg.get("textfile_directory", "/var/lib/node_exporter/textfile_collector"))
    filename = prom_cfg.get("metric_filename", "image_cleanup.prom")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    tmp_target = directory / f".{filename}.tmp"

    lines = [
        "# HELP image_cleanup_success Whether the last cleanup run succeeded (1) or failed (0)",
        "# TYPE image_cleanup_success gauge",
        f"image_cleanup_success {metrics['success']}",
        "# HELP image_cleanup_dry_run Whether the last run was dry-run (1) or actually deleted (0)",
        "# TYPE image_cleanup_dry_run gauge",
        f"image_cleanup_dry_run {metrics['dry_run']}",
        "# HELP image_cleanup_images_total Total images found on the node",
        "# TYPE image_cleanup_images_total gauge",
        f"image_cleanup_images_total {metrics['total_images']}",
        "# HELP image_cleanup_images_candidates Images eligible for removal (unused + older than retention)",
        "# TYPE image_cleanup_images_candidates gauge",
        f"image_cleanup_images_candidates {metrics['candidates']}",
        "# HELP image_cleanup_images_removed Images actually removed in this run",
        "# TYPE image_cleanup_images_removed gauge",
        f"image_cleanup_images_removed {metrics['removed']}",
        "# HELP image_cleanup_images_failed Image removals that failed in this run",
        "# TYPE image_cleanup_images_failed gauge",
        f"image_cleanup_images_failed {metrics['failed']}",
        "# HELP image_cleanup_bytes_freed Bytes freed in this run (0 if dry-run)",
        "# TYPE image_cleanup_bytes_freed gauge",
        f"image_cleanup_bytes_freed {metrics['bytes_freed']}",
        "# HELP image_cleanup_last_run_timestamp Unix timestamp of the last run",
        "# TYPE image_cleanup_last_run_timestamp gauge",
        f"image_cleanup_last_run_timestamp {metrics['last_run_timestamp']}",
        "",
    ]
    tmp_target.write_text("\n".join(lines))
    tmp_target.replace(target)
    logger.info(f"Metrics written to {target}", extra={"action": "write_metrics", "status": "ok"})


# -----------------------------------------------------------------------
# Main orchestration
# -----------------------------------------------------------------------
def main():
    config_path = os.environ.get("IMAGE_CLEANUP_CONFIG", "/etc/image-cleanup/config.yaml")
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    try:
        cfg = load_config(config_path)
    except CleanupError as e:
        print(json.dumps({"level": "ERROR", "message": str(e), "exit_code": e.exit_code}))
        sys.exit(e.exit_code)

    logger = setup_logging(cfg)
    dry_run = cfg["cleanup"].get("dry_run", True)
    retention_days = cfg["cleanup"].get("retention_days", 30)
    protected_patterns = cfg.get("protected", {}).get("patterns", [])

    logger.info(
        f"Starting image cleanup run (dry_run={dry_run}, retention_days={retention_days})",
        extra={"action": "start", "status": "info"},
    )

    lock = ProcessLock(cfg["lock"]["file"], logger)
    metrics = {
        "success": 0,
        "dry_run": 1 if dry_run else 0,
        "total_images": 0,
        "candidates": 0,
        "removed": 0,
        "failed": 0,
        "bytes_freed": 0,
        "last_run_timestamp": int(time.time()),
    }

    exit_code = ExitCode.SUCCESS
    try:
        lock.acquire()

        images = list_images(cfg, logger)
        used_ids = list_used_image_ids(cfg, logger)
        metrics["total_images"] = len(images)

        candidates = []
        for image in images:
            image_id = normalize_digest(image["id"])

            if image_id in used_ids:
                continue  # masih dipakai container - jangan disentuh

            if is_protected(image, protected_patterns):
                logger.info(
                    f"Image protected by config, skipping: {image.get('repoTags')}",
                    extra={"action": "protected", "status": "skipped", "image": str(image.get("repoTags"))},
                )
                continue

            age_days = get_image_age_days(image_id, cfg, logger)
            if age_days is None:
                continue  # umur tidak diketahui - jangan hapus demi keamanan

            if age_days < retention_days:
                continue  # masih cukup baru

            candidates.append((image, age_days))

        metrics["candidates"] = len(candidates)
        logger.info(
            f"{len(candidates)} image(s) eligible for removal "
            f"(unused + older than {retention_days} days)",
            extra={"action": "evaluate", "status": "info"},
        )

        for image, age_days in candidates:
            ref = (image.get("repoTags") or image.get("repoDigests") or [image["id"]])[0]
            if dry_run:
                logger.info(
                    f"[DRY-RUN] Would remove image: {ref} (age={age_days}d, size={image.get('size', 0)} bytes)",
                    extra={"action": "dry_run_candidate", "status": "would_remove", "image": ref, "age_days": age_days},
                )
                metrics["removed"] += 1  # dihitung sebagai "would remove" di dry-run
                metrics["bytes_freed"] += int(image.get("size", 0))
            else:
                if remove_image(image, cfg, logger):
                    metrics["removed"] += 1
                    metrics["bytes_freed"] += int(image.get("size", 0))
                else:
                    metrics["failed"] += 1

        if metrics["failed"] > 0:
            exit_code = ExitCode.REMOVAL_FAILED

        metrics["success"] = 1 if exit_code == ExitCode.SUCCESS else 0

        logger.info(
            f"Cleanup run completed: {metrics['removed']} removed, {metrics['failed']} failed, "
            f"{metrics['bytes_freed']} bytes freed"
            + (" [DRY-RUN, nothing actually deleted]" if dry_run else ""),
            extra={"action": "complete", "status": "ok"},
        )

    except CleanupError as e:
        exit_code = e.exit_code
        logger.error(str(e), extra={"action": "cleanup_run", "status": "failed"})
    except Exception as e:
        exit_code = ExitCode.UNEXPECTED_ERROR
        logger.error(f"Unexpected error: {e}", extra={"action": "cleanup_run", "status": "failed"}, exc_info=True)
    finally:
        try:
            write_metrics(cfg, metrics, logger)
        except Exception as e:
            logger.warning(f"Failed to write metrics: {e}", extra={"action": "write_metrics", "status": "warning"})
        lock.release()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()