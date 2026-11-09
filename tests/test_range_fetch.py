import concurrent.futures
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

from tools import range_fetch as rf


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        data = self.server.data
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"fixture-v1"')
        self.end_headers()

    def do_GET(self):
        server = self.server
        with server.lock:
            requested = self.headers.get("Range")
            server.requests[requested] = server.requests.get(requested, 0) + 1
            attempt = server.requests[requested]
        if server.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.org/private.zip")
            self.end_headers()
            return
        data = server.data
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested or "")
        if match and server.mode not in ("ignore", "html"):
            start, end = map(int, match.groups())
            body = data[start:end + 1]
            total = server.total_override or len(data)
            self.send_response(206)
            actual_start = start + 1 if server.mode == "bad-range" else start
            self.send_header("Content-Range", f"bytes {actual_start}-{end}/{total}")
        else:
            start, end = 0, len(data) - 1
            body = b"<!DOCTYPE html><html>login</html>" if server.mode == "html" else data
            self.send_response(200)
        chunked_fail = (server.mode == "chunked-once" and end > start and attempt == 1)
        if chunked_fail:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"fixture-v1"')
        self.send_header("Content-Type",
                         "text/html" if server.mode == "html" else "application/octet-stream")
        self.end_headers()
        fail = (end > start and (
            (server.mode == "truncate-once" and attempt == 1)
            or (server.mode == "fail-part" and start // server.chunk_size == 1)))
        try:
            if chunked_fail:
                self.wfile.write(("%x\r\n" % len(body)).encode("ascii") + body[:max(1, len(body)//2)])
            else:
                if server.mode == "fail-part" and start > server.chunk_size and start // server.chunk_size == 1:
                    self.wfile.write(b"")
                elif server.mode == "truncate-always" and len(body) > 8192:
                    self.wfile.write(body[:8192])
                else:
                    self.wfile.write(body[:max(1, len(body)//2)] if fail else body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


class RangeDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "fixture.zip"
        self.server.data = b"PK\x03\x04" + bytes(range(256)) * 1100
        self.server.mode = "normal"
        self.server.total_override = None
        self.server.requests = {}
        self.server.lock = threading.Lock()
        self.server.chunk_size = 64 * 1024
        self.url = "https://hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com/fixture.zip"
        port = self.server.server_port

        class LocalTestHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, request):
                def connection(host, timeout=45, **kwargs):
                    return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
                return self.do_open(connection, request)

        def test_opener():
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                LocalTestHTTPSHandler(),
                rf._AllowedRedirectHandler(),
            )
        self.opener_patch = mock.patch.object(rf, "_build_opener", side_effect=test_opener)
        self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.sleep_patch = mock.patch.object(rf.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def download(self, **kwargs):
        return rf.resumable_download(
            self.url, self.destination, chunk_bytes=self.server.chunk_size, **kwargs
        )

    def test_parallel_ranges_produce_exact_binary_and_hash(self):
        result = self.download(expected_size=len(self.server.data))
        self.assertEqual(self.destination.read_bytes(), self.server.data)
        self.assertEqual(result["sha256"], hashlib.sha256(self.server.data).hexdigest())
        self.assertGreater(result["parts"], 1)
        self.assertEqual(result["reused_parts"], 0)
        self.assertTrue(result["range_supported"])

    def test_truncated_chunks_retry_before_atomic_publish(self):
        self.server.mode = "truncate-once"
        result = self.download()
        self.assertEqual(result["size"], len(self.server.data))
        self.assertEqual(self.destination.read_bytes(), self.server.data)
        self.assertTrue(any(key.startswith("bytes=32768-") for key in self.server.requests))

    def test_chunked_incomplete_read_is_retried(self):
        self.server.mode = "chunked-once"
        result = self.download()
        self.assertEqual(result["size"], len(self.server.data))
        self.assertEqual(self.destination.read_bytes(), self.server.data)
        self.assertTrue(any(key.startswith("bytes=32768-") for key in self.server.requests))

    def test_every_request_can_truncate_and_resume_at_exact_suffix(self):
        self.server.mode = "truncate-always"
        result = self.download()
        self.assertEqual(self.destination.read_bytes(), self.server.data)
        self.assertEqual(result["sha256"], hashlib.sha256(self.server.data).hexdigest())
        self.assertEqual(self.server.requests["bytes=0-65535"], 1)
        self.assertIn("bytes=8192-65535", self.server.requests)
        self.assertIn("bytes=57344-65535", self.server.requests)

    def test_wrong_content_range_is_rejected(self):
        self.server.mode = "bad-range"
        with self.assertRaisesRegex(rf.DownloadError, "Content-Range"):
            self.download()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.server.requests["bytes=0-0"], 6)

    def test_resume_preserves_complete_parts_after_failed_attempt(self):
        self.server.mode = "fail-part"
        self.destination.write_bytes(b"old destination remains intact")
        with self.assertRaises(rf.DownloadError):
            self.download(workers=2)
        self.assertEqual(self.destination.read_bytes(), b"old destination remains intact")
        partials = list(self.destination.parent.glob(".parts/*/*.partial"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].stat().st_size, self.server.chunk_size // 2)
        self.assertGreater(len(list(self.destination.parent.glob(".parts/*/*.part"))), 0)
        self.server.mode = "normal"
        self.server.requests = {}
        result = self.download()
        self.assertGreater(result["reused_parts"], 0)
        self.assertEqual(self.destination.read_bytes(), self.server.data)

    def test_complete_parts_are_reused_on_next_invocation(self):
        self.download()
        self.destination.unlink()
        self.server.requests = {}
        result = self.download()
        self.assertEqual(result["parts"], result["reused_parts"])
        self.assertEqual(self.server.requests, {"bytes=0-0": 1})

    def test_oversized_remote_is_rejected_before_parts_created(self):
        self.server.total_override = 1024**3
        with self.assertRaisesRegex(rf.DownloadError, "max_bytes"):
            self.download(max_bytes=1024**2)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.destination.parent / ".parts").exists())

    def test_expected_size_mismatch_is_rejected(self):
        with self.assertRaisesRegex(rf.DownloadError, "expected_size"):
            self.download(expected_size=123)
        self.assertFalse(self.destination.exists())

    def test_unapproved_redirect_is_rejected(self):
        self.server.mode = "redirect"
        with self.assertRaisesRegex(rf.DownloadError, "approved"):
            self.download()
        self.assertEqual(sum(self.server.requests.values()), 1)
        self.assertFalse(self.destination.exists())

    def test_ignoring_range_is_allowed_for_bounded_small_asset(self):
        self.server.mode = "ignore"
        result = self.download()
        self.assertFalse(result["range_supported"])
        self.assertEqual(self.destination.read_bytes(), self.server.data)

    def test_large_ignored_range_is_rejected(self):
        self.server.mode = "ignore"
        self.server.data = b"PK\x03\x04" + b"x" * (9 * 1024**2)
        with self.assertRaisesRegex(rf.DownloadError, "8 MiB"):
            self.download()
        self.assertFalse(self.destination.exists())

    def test_html_login_page_is_not_published(self):
        self.server.mode = "html"
        with self.assertRaisesRegex(rf.DownloadError, "login"):
            self.download()
        self.assertFalse(self.destination.exists())

    def test_html_masquerading_as_binary_is_not_published(self):
        self.server.data = b"<!DOCTYPE html><html>login</html>"
        with self.assertRaisesRegex(rf.DownloadError, "HTML"):
            self.download()
        self.assertFalse(self.destination.exists())

    def test_non_allowlisted_initial_url_is_rejected(self):
        with self.assertRaisesRegex(rf.DownloadError, "approved"):
            rf.resumable_download("https://example.org/a.zip", self.destination)
        self.assertEqual(self.server.requests, {})

    def test_cli_rejects_destination_outside_project(self):
        with self.assertRaises(SystemExit) as ctx:
            rf.main([self.url, "--destination", str(self.destination)])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.server.requests, {})


if __name__ == "__main__":
    unittest.main()
