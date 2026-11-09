import hashlib,json,sys,threading,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from http_service import make_server

class HttpAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=make_server(ROOT,0)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}"
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def get(self,path):
        with urlopen(self.base+path,timeout=30) as response:return json.load(response)
    def test_health_and_contract(self):
        self.assertEqual(self.get("/health")["models"],26)
        self.assertEqual(len(self.get("/v1/sections")["items"]),3)
        self.assertIn("/v1/models/{model_id}/package",self.get("/openapi.json")["paths"])
    def test_search_and_section_isolation(self):
        for section,q in [("gimbal","Yaw"),("chassis","chassis"),("wheel_or_leg","wheel_leg")]:
            rows=self.get(f"/v1/search?q={q}&section={section}")["items"]
            self.assertTrue(rows)
            self.assertTrue(all(row["section"]==section for row in rows))
            self.assertIn("verification",rows[0]);self.assertIn("source",rows[0])
        self.assertEqual(self.get("/v1/search?q=Yaw&section=chassis")["count"],0)
    def test_download_and_detail(self):
        mid="rm-1935523-001"
        detail=self.get("/v1/models/"+mid)
        self.assertFalse(detail["usage"]["ready_for_assembly"])
        asset = self.server.catalog.assets.get(detail["entry"]["asset_id"])
        if not asset or not all((ROOT / row["path"]).is_file() for row in (detail["entry"], asset)):
            self.skipTest("Requires the original sample and package in the local asset library")
        with urlopen(self.base+detail["downloads"]["model"],timeout=30) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(),detail["entry"]["sha256"])
        with urlopen(Request(self.base+detail["downloads"]["package"],method="HEAD"),timeout=30) as response:
            self.assertEqual(response.headers["X-Content-SHA256"],detail["downloads"]["package_sha256"])
    def test_invalid_requests(self):
        for path,code in [("/v1/search",400),("/v1/search?q=x&limit=0",400),("/v1/search?q=x&section=bad",400),("/v1/models/no-such-model/file",404),("/v1/models/../../README.md",404)]:
            with self.assertRaises(HTTPError) as error:self.get(path)
            self.assertEqual(error.exception.code,code)
        for headers in [{"Origin":"https://example.com"},{"Host":"evil.example"}]:
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+"/health",headers=headers))
            self.assertEqual(error.exception.code,403)
    def test_changed_hash_refuses_download(self):
        mid="rm-1935523-001";entry=self.server.catalog.modules[mid]["entry"]
        if not (ROOT / entry["path"]).is_file():
            self.skipTest("Requires the original sample in the local asset library")
        original=entry["sha256"]
        try:
            entry["sha256"]="0"*64
            with self.assertRaises(HTTPError) as error:self.get("/v1/models/"+mid+"/file")
            self.assertEqual(error.exception.code,409)
        finally:entry["sha256"]=original
