import hashlib,json,sys,threading,unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from http_service import make_server
from hybrid_search import SemanticSearch
from library import search as sqlite_search

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
        self.assertEqual(self.get("/health")["models"],len(self.server.catalog.modules))
        self.assertGreater(self.get("/health")["models"],26)
        self.assertEqual(len(self.get("/v1/sections")["items"]),3)
        self.assertIn("/v1/models/{model_id}/package",self.get("/openapi.json")["paths"])

    def test_reuse_evidence_follows_the_selected_definition_and_download_policy(self):
        detail = self.get("/v1/models/rm-1935523-022")
        plan = self.get(detail["reuse_href"])
        self.assertEqual(plan["selection"]["definition_id"], "0:1:1:7")
        self.assertEqual(plan["preferred_artifact"], "file")
        self.assertEqual(plan["artifacts"]["file"]["sha256"], detail["entry"]["sha256"])
        component = self.get(plan["component_measurements"][0]["component_reuse_href"])
        self.assertEqual(component["selection"]["definition_id"], "0:1:1:8")
        self.assertTrue(component["interface_measurements"])

        split = self.get("/v1/models/rm-761042-c2ed254897d8b3e8e/reuse")
        self.assertNotIn("package", split["artifacts"])
        self.assertIn("resolve_native_references", split["required_checks"])
        self.assertFalse(split["verification"]["fit_to_target_verified"])
    def test_search_and_section_isolation(self):
        for section,q in [("gimbal","Yaw"),("chassis","chassis"),("wheel_or_leg","wheel_leg")]:
            rows=self.get(f"/v1/search?q={q}&section={section}")["items"]
            self.assertTrue(rows)
            self.assertTrue(all(row["section"]==section for row in rows))
            self.assertIn("verification",rows[0]);self.assertIn("source",rows[0])
        self.assertEqual(self.get("/v1/search?q=Yaw&section=chassis")["count"],0)

    def test_search_uses_source_backed_module_features(self):
        cases = {
            "3508双电机舵轮": "rm-803685-014",
            "MGN7 滑轨 气弹簧": "rm-803685-014",
            "行走与转向各用一个驱动": "rm-803685-014",
            "用导轨限制轮组竖直运动，气弹簧负责缓冲": "rm-803685-014",
            "麦轮 纵臂 双避震": "rm-1883259-002",
            "低成本聚氨酯辊子 铜套": "rm-1935523-024",
            "全向轮轮毂": "rm-1935523-025",
            "Ｐ１５．８ Hexroll 平替": "rm-1936175-017",
            "P15.8 双轴承支撑": "rm-1936175-018",
            "串联腿 同轴嵌套": "rm-776744-010",
            "15度侧取": "rm-1883259-004",
        }
        for query, expected_id in cases.items():
            with self.subTest(query=query):
                response = self.get("/v1/search?" + urlencode({"q": query, "limit": 1}))
                hit = response["items"][0]
                self.assertEqual(hit["id"], expected_id)
                self.assertTrue(hit["matches"])
                profile = hit["retrieval_profile"]
                self.assertTrue(profile["reuse_checks"])
                self.assertTrue(profile["facts"])
                for fact in profile["facts"]:
                    evidence = fact["evidence"]
                    self.assertTrue(evidence["locator"])
                    self.assertEqual(evidence["url"], hit["source_url"])
                self.assertFalse(hit["usage"]["ready_for_assembly"])

    def test_search_requires_explicit_model_number(self):
        other_ratio = self.get("/v1/search?" + urlencode({"q": "P15.76 减速箱"}))
        self.assertTrue({"rm-1936175-017", "rm-1936175-018"}.isdisjoint(
            item["id"] for item in other_ratio["items"]
        ))
        unknown_model = self.get("/v1/search?" + urlencode({"q": "不存在的 MGN999 滑轨"}))
        self.assertEqual(unknown_model["items"], [])

    def test_function_and_exact_model_search(self):
        def query(q, mode, **options):
            return self.get("/v1/search?" + urlencode({"q": q, "mode": mode, **options}))["items"]

        functional = query("行走和转向分别用独立驱动", "function")
        self.assertEqual(functional[0]["id"], "rm-803685-014")
        strict = query("3508 舵轮", "model")
        self.assertEqual(strict[0]["id"], "rm-803685-014")
        self.assertNotIn("gearbox", {hit["category"] for hit in strict})
        claim, = strict[0]["model_matches"]
        self.assertEqual((claim["code"], claim["role"]), ("3508", "used_component"))
        self.assertEqual(claim["evidence"]["type"], "author_pdf")

        for exact, expected_count in (("Ｐ１５．８", 2), ("P15.76", 0), ("P15d8", 0),
                                      ("ＭＧＮ７Ｃ", 3), ("MGN7 MGN7C", 0), ("803685", 0)):
            with self.subTest(q=exact):
                self.assertEqual(len(query(exact, "model")), expected_count)
        self.assertEqual(query("MGN7", "model", section="chassis"), [])
        self.assertEqual(query("ML1.0", "model")[0]["model_matches"][0]["role"], "module_model")
        self.assertEqual(query("Hexroll", "model")[0]["model_matches"][0]["role"], "reference_product")
        self.assertEqual(query("rm-1935523-022", "model")[0]["definition_id"], "0:1:1:7")

    @unittest.skipUnless(find_spec("chromadb") and find_spec("fastembed")
                         and (ROOT / "runtime/chroma/chroma.sqlite3").is_file(),
                         "Local vector artifacts are optional for SQLite-only installations")
    def test_hybrid_returns_both_channels_and_respects_component_filters(self):
        query = "把轮组接到悬挂的机加工法兰"
        response = self.get("/v1/search?" + urlencode({"q": query, "mode": "function", "section": "wheel_or_leg"}))
        self.assertEqual((response["retrieval"], response["vector_status"]), ("hybrid", "ready"))
        self.assertTrue(all(item["section"] == "wheel_or_leg" for item in response["items"]))
        self.assertEqual(response["items"][0]["id"], "rm-1935523-022")
        self.assertEqual(set(response["items"][0]["retrieval_channels"]), {"sqlite", "chroma"})

        params = [("q", query), ("mode", "function"), ("spec", "opening_count:9"),
                  ("spec", "opening_diameter_mm:6")]
        result = self.get("/v1/search?" + urlencode(params))
        self.assertEqual(result["retrieval"], "hybrid")
        hit, = result["items"]
        self.assertEqual(hit["cad_source"]["definition_id"], "0:1:1:8")
        self.assertIn("chroma", hit["retrieval_channels"])

    def test_function_falls_back_to_sqlite_and_model_never_calls_chroma(self):
        catalog = self.server.catalog
        semantic = SemanticSearch(ROOT, catalog.modules)
        query = "行走和转向分别用独立驱动"
        with patch.object(catalog, "semantic", semantic):
            with patch("hybrid_search.open_collection", side_effect=OSError("fixture")):
                result = self.get("/v1/search?" + urlencode({"q": query, "mode": "function"}))
            self.assertEqual((result["retrieval"], result["vector_status"]), ("lexical", "unavailable"))
            self.assertEqual([hit["id"] for hit in result["items"]],
                             [hit["id"] for hit in sqlite_search(ROOT, query, mode="function")])
            with patch.object(semantic, "search", side_effect=AssertionError("model reached Chroma")):
                unknown = self.get("/v1/search?q=P15.76&mode=model")
            self.assertEqual(unknown["items"], [])
            self.assertEqual(unknown["vector_status"], "not_requested")

    def test_functional_category_returns_evidence_and_original_cad(self):
        query = "接触面角度对不准时柔软吸盘能贴合，托盘撑住工件避免晃动"
        data = self.get("/v1/search?" + urlencode({"q": query, "mode": "function", "category": "gripper"}))
        self.assertTrue(all("gripper" in hit["mechanism_profile"]["categories"] for hit in data["items"]))
        hit = next(hit for hit in data["items"] if hit["id"] in
                   {"rm-761042-c2ed254897d8b3e8e", "rm-761042-ce916434f1d6c55d4"})
        advantage = next(claim for claim in hit["mechanism_profile"]["claims"] if claim["kind"] == "advantage")
        self.assertEqual(advantage["evidence"]["type"], "author_pdf")
        self.assertIn("23", advantage["evidence"]["locator"])
        self.assertEqual(hit["cad_source"]["sha256"], hit["entry"]["sha256"])

    def test_full_catalog_component_locators(self):
        platform = self.get("/v1/models/rm-815703-cb91f7be3de320dfd")
        self.assertEqual(platform["component_locator"]["kind"], "cad_file")
        self.assertEqual(platform["card_tier"], "source_reviewed")
        self.assertIn("ML1.0.SLDASM", platform["entry"]["path"])
        ace = self.get("/v1/models/rm-761042-c2ed254897d8b3e8e")
        self.assertIsNone(ace["downloads"]["package"])
        self.assertEqual(len(ace["dependency_scope"]["local_archive_volumes"]), 3)
        matches = [card for card in self.server.catalog.modules.values()
                   if card["source_id"] == "bbs-1935523" and card["definition_id"] == "0:1:1:23"]
        self.assertTrue(matches)
        for card in matches:
            self.assertEqual(card["component_locator"]["occurrence_count"], 28)
            self.assertEqual(card["component_locator"]["kind"], "step_definition")
        plate = self.get("/v1/search?" + urlencode({"q": "轮毂前夹板", "limit": 1}))["items"][0]
        self.assertEqual(plate["component_locator"]["definition_id"], "0:1:1:22")
        self.assertEqual(plate["component_locator"]["kind"], "step_definition")

    def test_mvp_live_native_instances(self):
        chassis = self.get("/v1/models/rm-1883259-001")
        locator = chassis["component_locator"]
        self.assertEqual(locator["native_tree_status"], "live_configuration_observed")
        self.assertEqual(locator["active_configuration"], "默认")
        self.assertIn({"card_id": "rm-1883259-002", "instance_count": 2},
                      locator["live_reference_targets"])
        swerve = self.get("/v1/models/rm-1883384-007")["component_locator"]
        self.assertEqual(swerve["native_tree_status"], "live_branch_observed")
        self.assertEqual(swerve["occurrence_count"], 4)
        self.assertFalse(swerve["live_reference_targets"])
        self.assertTrue(all(item["path_semantics"] == "saved_or_unresolved_path"
                            for item in swerve["occurrence_examples"]))
        self.assertFalse(chassis["usage"]["ready_for_assembly"])

    def test_search_and_detail_return_interface_evidence(self):
        hits = self.get("/v1/search?" + urlencode({"q": "3508双电机舵轮", "limit": 1}))["items"]
        swerve = hits[0]["interfaces"]["component_measurements"][0]
        plate = self.get("/v1/models/" + swerve["component_card_id"])
        self.assertEqual(plate["component_id"], swerve["component_id"])
        evidence, = plate["interfaces"]["measurements"]
        self.assertEqual(evidence["target"]["configuration"], "Mirror默认")
        self.assertEqual(len(evidence["geometry"]["hole_pattern"]["openings"]), 6)
        self.assertFalse(evidence["fit_to_target_verified"])

        frame = self.get("/v1/models/rm-803685-013")
        self.assertEqual(len(frame["interfaces"]["component_measurements"]), 3)
        parent = self.get("/v1/models/rm-1935523-022")
        link, = parent["interfaces"]["component_measurements"]
        flange = self.get("/v1/models/" + link["component_card_id"])
        self.assertEqual(flange["definition_id"], "0:1:1:8")
        self.assertEqual(len(flange["interfaces"]["hole_patterns"][0]["openings"]), 9)

    def test_component_alias_specs_and_original_source(self):
        alias = self.get("/v1/search?" + urlencode({"alias": "舵下滑轨悬挂"}))
        self.assertEqual(alias["items"][0]["id"], "rm-803685-014")
        self.assertIn({"value": "舵下滑轨悬挂", "kind": "reviewed_alias"},
                      alias["items"][0]["component_aliases"])
        params = [("spec", "opening_diameter_mm:6"), ("spec", "opening_count:9"),
                  ("spec", "opening_center_distance_mm:54.99:55.01")]
        flange, = self.get("/v1/search?" + urlencode(params))["items"]
        self.assertEqual(flange["id"], "rm-1935523-cc08defa62bc17d10")
        self.assertEqual(flange["cad_source"]["definition_id"], "0:1:1:8")
        self.assertEqual(flange["cad_source"]["sha256"], flange["entry"]["sha256"])
        self.assertTrue(flange["cad_source"]["archive_path"])
        self.assertTrue(all(spec["evidence"] and spec["unit"] for spec in flange["component_specs"]))
        self.assertEqual(self.get("/v1/search?spec=opening_diameter_mm:6.1")["items"], [])
        self.assertEqual(self.get("/v1/search?spec=opening_count:6&spec=opening_count:3")["items"], [])
        with self.assertRaises(HTTPError) as error:
            self.get("/v1/search?spec=opening_count:nan")
        self.assertEqual(error.exception.code, 400)

    def test_download_and_detail(self):
        mid="rm-1935523-001"
        detail=self.get("/v1/models/"+mid)
        self.assertFalse(detail["usage"]["ready_for_assembly"])
        source = self.server.catalog.assets[detail["entry"]["asset_id"]]
        if not (ROOT / detail["entry"]["path"]).is_file() or not (ROOT / source["path"]).is_file():
            self.skipTest("Original STEP and source archive are required for download verification")
        with urlopen(self.base+detail["downloads"]["model"],timeout=30) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(),detail["entry"]["sha256"])
        with urlopen(Request(self.base+detail["downloads"]["package"],method="HEAD"),timeout=30) as response:
            self.assertEqual(response.headers["X-Content-SHA256"],detail["downloads"]["package_sha256"])
    def test_invalid_requests(self):
        for path,code in [("/v1/search",400),("/v1/search?q=x&limit=0",400),("/v1/search?q=x&section=bad",400),("/v1/search?q=x&mode=bad",400),("/v1/models/no-such-model/file",404),("/v1/models/../../README.md",404)]:
            with self.assertRaises(HTTPError) as error:self.get(path)
            self.assertEqual(error.exception.code,code)
        for headers in [{"Origin":"https://example.com"},{"Host":"evil.example"}]:
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+"/health",headers=headers))
            self.assertEqual(error.exception.code,403)
    def test_changed_hash_refuses_download(self):
        mid="rm-1935523-001";entry=self.server.catalog.modules[mid]["entry"]
        if not (ROOT / entry["path"]).is_file():
            self.skipTest("Original STEP is required for the changed-hash check")
        original=entry["sha256"]
        try:
            entry["sha256"]="0"*64
            with self.assertRaises(HTTPError) as error:self.get("/v1/models/"+mid+"/file")
            self.assertEqual(error.exception.code,409)
        finally:entry["sha256"]=original
