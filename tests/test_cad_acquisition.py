import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from cad_acquisition import Acquisition, install


class BulkAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.payloads = {}
        self.assets = []
        self.rows = []
        self.cards = {}
        self.add_asset("one", "same archive.zip", [("装配/零件.SLDPRT", b"part"),
                                               ("装配/组件.step", b"step")])
        self.add_asset("copy", "same archive.zip", [("装配/零件.SLDPRT", b"part"),
                                                ("装配/组件.step", b"step")], duplicate="one")
        self.add_asset("two", "single.step", [("single.step", b"another step")])
        self.write_catalog()

    def add_asset(self, key, name, members, duplicate=None):
        if duplicate:
            payload = self.payloads[duplicate]
        elif name.endswith(".zip"):
            data = io.BytesIO()
            with ZipFile(data, "w") as archive:
                for member, content in members:
                    archive.writestr(member, content)
            payload = data.getvalue()
        else:
            payload = members[0][1]
        self.payloads[key] = payload
        digest = hashlib.sha256(payload).hexdigest()
        self.assets.append({"asset_id": key, "source_id": key, "thread_id": 1,
                            "name": name, "path": f"raw/{key}/{name}", "status": "downloaded",
                            "bytes": len(payload), "sha256": digest,
                            "url": f"https://hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com/{digest}/{name}"})
        for index, (member, content) in enumerate(members):
            path = f"assets/1/{key}/{member}"
            row = {"path": path, "asset_id": key, "thread_id": 1, "content_type": "cad",
                   "extension": Path(member).suffix.lower(), "bytes": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()}
            self.rows.append(row)
            card_id = f"{key}-{index}"
            self.cards[card_id] = {"id": card_id, "entry": row,
                                   "cad_source": {"file_available": True},
                                   "source_url": "https://bbs.robomaster.com/article/1",
                                   "definition_id": "0:1:1:7" if member.endswith(".step") else None}

    def write_catalog(self):
        for relative, value in (
            ("catalog/inventory.json", self.rows),
            ("catalog/downloads.json", self.assets),
            ("catalog/open-cad-2025.json", []),
            ("reports/supplemental-downloads.json", []),
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value), encoding="utf-8")
        (self.root / "catalog/modules.jsonl").write_text(
            "\n".join(json.dumps(card) for card in self.cards.values()), encoding="utf-8")
        self.acquisition = Acquisition(self.root, self.cards)

    def serve(self, url, target, record):
        return install(lambda: io.BytesIO(self.payloads[record["asset_id"]]), target, record)

    def test_downloads_once_installs_all_paths_and_reuses_on_retry(self):
        with patch("cad_acquisition.download", side_effect=self.serve) as network:
            result = self.acquisition.acquire_all()
            self.assertEqual(network.call_count, 2)
        self.assertEqual((result["status"], result["cards_total"], result["cad_files_installed"]),
                         ("complete", 5, 5))
        for row in self.rows:
            self.assertEqual(hashlib.sha256((self.root / row["path"]).read_bytes()).hexdigest(), row["sha256"])
        with patch("cad_acquisition.download", side_effect=AssertionError("Unexpected download")):
            result = self.acquisition.acquire_all()
            single = self.acquisition.acquire("one-1")
        self.assertTrue(single["verified"])
        self.assertEqual(single["definition_id"], "0:1:1:7")
        self.assertFalse(any(item["downloaded"] for item in result["attachments"]))
        self.assertEqual(json.loads(Path(result["report_path"]).read_text())["status"], "complete")

    def test_failure_continues_and_retry_recovers(self):
        def failing_download(url, target, record):
            if record["asset_id"] == "copy":
                raise OSError("connection interrupted")
            return self.serve(url, target, record)
        with patch("cad_acquisition.download", side_effect=failing_download):
            result = self.acquisition.acquire_all()
        self.assertEqual((result["status"], result["failed"]), ("partial", 1))
        self.assertTrue((self.root / self.cards["two-0"]["entry"]["path"]).is_file())
        with patch("cad_acquisition.download", side_effect=self.serve) as network:
            result = self.acquisition.acquire_all()
            self.assertEqual(network.call_count, 1)
        self.assertEqual(result["status"], "complete")

    def test_changed_cad_is_reported_and_not_replaced(self):
        row = self.cards["copy-0"]["entry"]
        target = self.root / row["path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"changed")
        with patch("cad_acquisition.download", side_effect=self.serve):
            result = self.acquisition.acquire_all()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(target.read_bytes(), b"changed")
        self.assertTrue((self.root / self.cards["two-0"]["entry"]["path"]).is_file())

    def test_cli_all_and_exit_status(self):
        for asset in self.assets:
            target = self.root / asset["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.payloads[asset["asset_id"]])
        script = Path(__file__).resolve().parents[1] / "tools/cad_acquisition.py"
        command = [sys.executable, "-B", str(script), "--root", str(self.root), "--all"]
        result = subprocess.run(command, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["attachments_total"], 2)
        (self.root / self.rows[0]["path"]).write_bytes(b"changed")
        result = subprocess.run(command, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "partial")
        result = subprocess.run(command + ["one-0"], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
