import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
spec=importlib.util.spec_from_file_location("ingest",Path(__file__).resolve().parents[1]/"tools"/"ingest_forum.py")
ingest=importlib.util.module_from_spec(spec);spec.loader.exec_module(ingest)
class ImportSafetyTests(unittest.TestCase):
    def test_nuxt_attachment_decode(self):
        body='<p>原始版本</p><a data-w-e-type="attachment" href="https://bbs-web-static.robomaster.com/a/云台.zip" data-file-name="云台.zip" data-file-len="17" data-file-md5="123">附件</a>'
        raw='<title>例子-RoboMaster 社区</title><script>window.__NUXT__={body:'+json.dumps(body,ensure_ascii=True)+'}</script>'
        got=ingest.parse_page(raw,7)
        self.assertEqual(got["attachments"][0]["name"],"云台.zip")
        self.assertEqual(got["attachments"][0]["declared_bytes"],17)
        self.assertFalse(got["authentication_used"])
    def test_archive_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ["../outside.sldprt","/absolute.step","C:/absolute.step",r"..\outside.step"]:
                with self.assertRaises(ValueError):ingest.safe_member_path(root,name)
            self.assertEqual(ingest.safe_member_path(root,"模块/云台.step"),(root/"模块"/"云台.step").resolve())
    def test_source_hosts(self):
        for url in ["http://bbs.robomaster.com/a","https://evil.example/model.zip","https://user:pass@bbs.robomaster.com/a"]:
            with self.assertRaises(ValueError):ingest.encoded_url(url)
        self.assertIn("%E4%BA%91",ingest.encoded_url("https://bbs-web-static.robomaster.com/云台.zip"))
if __name__=="__main__":unittest.main()
