import importlib.util
import io
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "server" / "app.py"
spec = importlib.util.spec_from_file_location("formatflip_runtime_hardening", APP_PATH)
appmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appmod)


def make_sample_mp4(path: Path) -> str:
    ffmpeg = appmod._find_ffmpeg()
    if not ffmpeg:
        raise unittest.SkipTest("FFmpeg is not installed")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=128x96:rate=8:duration=0.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    return ffmpeg


class RuntimeHardeningTests(unittest.TestCase):
    def setUp(self):
        appmod.app.config["TESTING"] = True

    def _post_mp4(self, payload: bytes):
        return appmod.app.test_client().post(
            "/api/convert",
            data={"output_format": "mp3", "file": (io.BytesIO(payload), "sample.mp4")},
            content_type="multipart/form-data",
        )

    def test_cleanup_is_lazy_and_idempotent(self):
        original_thread = appmod._cleanup_thread
        original_before = appmod.ensure_temp_dirs_and_cleanup
        appmod._cleanup_thread = None
        started = []
        try:
            with patch.object(appmod.threading.Thread, "start", lambda thread: started.append(thread)):
                client = appmod.app.test_client()
                with client.get("/health") as response:
                    self.assertEqual(response.status_code, 200)
                first = appmod._cleanup_thread
                self.assertIsNotNone(first)
                self.assertEqual(len(started), 1)
                with client.get("/health") as response:
                    self.assertEqual(response.status_code, 200)
                self.assertEqual(len(started), 2)
                self.assertIsNot(appmod._cleanup_thread, first)
        finally:
            appmod._cleanup_thread = original_thread
            appmod.app.before_request_funcs[None].remove(original_before)
            appmod.app.before_request_funcs[None].append(original_before)

    def test_ffmpeg_process_output_is_drained_without_unbounded_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "tiny.mp4"
            out = Path(temp) / "tiny.mp3"
            sample = appmod.UPLOAD_DIR / f"runtime-{uuid.uuid4().hex}.mp4"
            make_sample_mp4(sample)
            try:
                with patch.object(appmod.subprocess, "run", side_effect=AssertionError("subprocess.run must not buffer all output")):
                    ok, error = appmod._run_conversion(sample, out, "mp3")
                self.assertTrue(ok, error)
                self.assertGreater(out.stat().st_size, 100)
            finally:
                sample.unlink(missing_ok=True)

    def test_cleanup_ignores_processing_jobs_and_keeps_fresh_results(self):
        output = appmod.OUTPUT_DIR / f"keep-{uuid.uuid4().hex}.mp3"
        output.write_bytes(b"id3-test")
        old_created = time.time() - appmod.FILE_TTL_SECONDS - 100
        fresh_created = time.time()
        with appmod.jobs_lock:
            appmod.jobs["test-old"] = {"status": "completed", "created_at": old_created, "output_path": str(output)}
            appmod.jobs["test-fresh"] = {"status": "completed", "created_at": fresh_created, "output_path": str(output)}
            appmod.jobs["test-processing"] = {"status": "processing", "created_at": old_created}
        try:
            worker_source = (APP_PATH).read_text(encoding="utf-8")
            self.assertIn("value.get(\"status\") != \"processing\"", worker_source)
            self.assertIn("output_paths_in_use", worker_source)
        finally:
            with appmod.jobs_lock:
                for key in ("test-old", "test-fresh", "test-processing"):
                    appmod.jobs.pop(key, None)
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
