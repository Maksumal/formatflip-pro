import io
import importlib.util
import unittest
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FFMPEG_BIN = shutil.which("ffmpeg") or r"C:\Users\chann\AppData\Local\hermes\tools\ffmpeg-9.0.1-win32-x64\bin\ffmpeg.exe"
spec = importlib.util.spec_from_file_location("formatflip_app", Path(__file__).with_name("app.py"))
appmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appmod)


class FormatFlipTests(unittest.TestCase):
    def setUp(self):
        appmod.app.config["TESTING"] = True
        self.client = appmod.app.test_client()

    def test_home_serves_real_landing_page(self):
        with self.client.get("/") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"FormatFlip", response.data)

    def test_health_reports_ffmpeg(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ffmpeg"])

    def test_format_catalog(self):
        data = self.client.get("/api/formats").get_json()
        self.assertIn("mp3", data["video_to_audio"])
        self.assertIn("webm", data["video_to_video"])

    def test_missing_file_is_rejected(self):
        response = self.client.post("/api/convert", data={"output_format":"mp3"})
        self.assertEqual(response.status_code, 400)

    def test_unsupported_format_is_rejected(self):
        response = self.client.post("/api/convert", data={"output_format":"exe", "file":(io.BytesIO(b"x"),"clip.mp4")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)

    def test_unsupported_input_type_is_rejected(self):
        response = self.client.post("/api/convert", data={"output_format":"mp3", "file":(io.BytesIO(b"x"),"clip.exe")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 415)

    def test_real_mp4_to_mp3_conversion_download_and_cleanup(self):
        ffmpeg = FFMPEG_BIN
        sample = appmod.UPLOAD_DIR / "tiny-test-input.mp4"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=0.6", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample)]
        import subprocess
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        try:
            with sample.open("rb") as stream:
                response = self.client.post("/api/convert", data={"output_format":"mp3", "file":(stream,"sample.mp4")}, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            job_id = response.get_json()["job_id"]
            self.assertEqual(self.client.get(f"/api/status/{job_id}").get_json()["status"], "completed")
            with self.client.get(f"/download/{job_id}") as download:
                self.assertEqual(download.status_code, 200)
                self.assertTrue(download.data.startswith(b"ID3") or download.data[:2] == b"\xff\xfb")
            self.assertTrue(appmod.jobs[job_id]["output_path"])
        finally:
            sample.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
