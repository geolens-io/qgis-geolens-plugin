import json
import unittest
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from geolens_qgis.client import (
    GeoLensClient,
    parse_dataset,
    parse_tile_scope,
    refresh_delay_seconds,
    same_server,
)


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class Recorder:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request, timeout=0):
        self.requests.append(request)
        return Response(next(self.responses))


class Routes:
    """An opener answering by path; a missing path is a 404."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.paths = []

    def __call__(self, request, timeout=0):
        path = urlparse(request.full_url).path
        self.paths.append(path)
        if path not in self.bodies:
            raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(b"{}"))
        return Response(self.bodies[path])


class ClientTests(unittest.TestCase):
    def test_sends_identifying_user_agent(self):
        recorder = Recorder([{"features": []}])
        client = GeoLensClient("example.com", opener=recorder)
        client.search()
        agent = recorder.requests[0].get_header("User-agent")
        self.assertIsNotNone(agent)
        self.assertTrue(agent.startswith("qgis-geolens-plugin/"))
        self.assertNotIn("Python-urllib", agent)

    def test_normalizes_url_and_sends_api_key(self):
        recorder = Recorder([{"features": []}])
        client = GeoLensClient(" example.com/ ", "secret", recorder)
        self.assertEqual(client.search(), [])
        request = recorder.requests[0]
        self.assertEqual(request.get_header("X-api-key"), "secret")
        self.assertEqual(urlparse(request.full_url).netloc, "example.com")

    def test_capabilities_come_from_the_capabilities_endpoint(self):
        document = {"publish_contract": 2, "flags": {"enable_dataset_editing": True}}
        routes = Routes({"/api/capabilities": document})
        capabilities = GeoLensClient("example.com", opener=routes).capabilities()
        self.assertTrue(capabilities["dataset_editing"])
        self.assertEqual(capabilities["publish_contract"], 2)
        self.assertEqual(capabilities["document"], document)
        self.assertEqual(routes.paths, ["/api/capabilities"])

    def test_capabilities_fall_back_to_feature_flags_on_older_servers(self):
        routes = Routes(
            {"/api/settings/feature-flags/": {"enable_dataset_editing": True}}
        )
        capabilities = GeoLensClient("example.com", opener=routes).capabilities()
        self.assertTrue(capabilities["dataset_editing"])
        self.assertIsNone(capabilities["publish_contract"])
        self.assertEqual(
            routes.paths, ["/api/capabilities", "/api/settings/feature-flags/"]
        )

    def test_capabilities_without_either_endpoint_disable_editing(self):
        capabilities = GeoLensClient("example.com", opener=Routes({})).capabilities()
        self.assertFalse(capabilities["dataset_editing"])

    def test_parses_vector_and_raster_datasets(self):
        vector = parse_dataset(
            {
                "id": "roads",
                "properties": {
                    "title": "Roads",
                    "geometry_type": "LINESTRING",
                    "feature_count": 2,
                },
            }
        )
        raster = parse_dataset(
            {
                "id": "dem",
                "properties": {"record_type": "raster_dataset", "band_count": 1},
            }
        )
        self.assertEqual(vector.title, "Roads")
        self.assertFalse(vector.is_raster)
        self.assertTrue(raster.is_raster)

    def test_builds_signed_vector_tile_source(self):
        recorder = Recorder(
            [
                {
                    "kind": "vector",
                    "sig": "abc",
                    "exp": 42,
                    "scope": "roads",
                    "expires_in": 60,
                }
            ]
        )
        source = GeoLensClient("https://example.com", opener=recorder).tile_source(
            "id with space"
        )
        self.assertEqual(source["source_layer"], "data.roads")
        self.assertIn("/api/tiles/data.roads/{z}/{x}/{y}.pbf?", source["url"])
        self.assertEqual(parse_qs(urlparse(source["url"]).query)["sig"], ["abc"])
        self.assertIn("id%20with%20space", recorder.requests[0].full_url)

    def test_vector_tile_source_strips_partition_and_tenant_from_the_path(self):
        for scope, table in (
            ("roads:p3", "roads"),
            ("tenant_a:roads:p3", "roads"),
            ("roads", "roads"),
        ):
            with self.subTest(scope=scope):
                recorder = Recorder(
                    [{"kind": "vector", "sig": "abc", "exp": 42, "scope": scope}]
                )
                source = GeoLensClient(
                    "https://example.com", opener=recorder
                ).tile_source("d")
                url = urlparse(source["url"])
                query = parse_qs(url.query)
                self.assertEqual(
                    url.path, f"/api/tiles/data.{table}/{{z}}/{{x}}/{{y}}.pbf"
                )
                self.assertEqual(source["source_layer"], f"data.{table}")
                self.assertEqual(query["scope"], [scope])
                self.assertEqual(query.get("pv"), ["3"] if ":p3" in scope else None)

    def test_same_server_ignores_case_default_port_and_trailing_slash(self):
        for other in (
            "https://gis.example.org",
            "HTTPS://GIS.Example.org/",
            "https://gis.example.org:443",
        ):
            with self.subTest(other=other):
                self.assertTrue(same_server("https://gis.example.org", other))
        self.assertTrue(
            same_server("http://gis.example.org:80/geo/", "http://gis.example.org/geo")
        )

    def test_same_server_tells_servers_apart(self):
        for other in (
            "https://other.example.org",
            "http://gis.example.org",
            "https://gis.example.org:8443",
            "https://gis.example.org/other",
            "",
            "gis.example.org",
        ):
            with self.subTest(other=other):
                self.assertFalse(same_server("https://gis.example.org", other))
        self.assertFalse(same_server("", ""))

    def test_parses_tile_scopes(self):
        self.assertEqual(parse_tile_scope("roads:p3"), ("roads", "3"))
        self.assertEqual(parse_tile_scope("t:roads:p12"), ("roads", "12"))
        self.assertEqual(parse_tile_scope("roads"), ("roads", None))

    def test_refresh_follows_each_tokens_lifetime(self):
        # Tokens last from 61 seconds to 16 minutes; a fixed interval would
        # let the shortest one expire before it is renewed.
        self.assertLess(refresh_delay_seconds(61), 61)
        self.assertLess(refresh_delay_seconds(900), 900)
        self.assertGreater(refresh_delay_seconds(960), refresh_delay_seconds(61))
        self.assertEqual(refresh_delay_seconds(None), 30)
        self.assertEqual(refresh_delay_seconds("junk"), 30)
        self.assertEqual(refresh_delay_seconds(5), 10)

    def test_raster_tile_source_keeps_signed_template_and_lifetime(self):
        recorder = Recorder(
            [
                {
                    "kind": "raster",
                    "tile_url": "/raster-tiles/d/tiles/{z}/{x}/{y}.png?sig=s&exp=9&pv=0",
                    "expires_in": 61,
                }
            ]
        )
        source = GeoLensClient("https://example.com", opener=recorder).tile_source("d")
        self.assertEqual(
            source["url"],
            "https://example.com/raster-tiles/d/tiles/{z}/{x}/{y}.png?sig=s&exp=9&pv=0",
        )
        self.assertEqual(source["expires_in"], 61)

    def test_follows_next_links_but_rebases_origin(self):
        recorder = Recorder(
            [
                {
                    "type": "FeatureCollection",
                    "features": [{"id": 1}],
                    "links": [
                        {
                            "rel": "next",
                            "href": "http://localhost:8080/api/items?page=2",
                        }
                    ],
                },
                {"type": "FeatureCollection", "features": [{"id": 2}], "links": []},
            ]
        )
        collection = GeoLensClient("https://example.com", opener=recorder).features(
            "roads", 2
        )
        self.assertEqual([item["id"] for item in collection["features"]], [1, 2])
        self.assertEqual(urlparse(recorder.requests[1].full_url).netloc, "example.com")

    def test_feature_writes_use_expected_methods(self):
        recorder = Recorder([{"id": 9}, {}, {}])
        client = GeoLensClient("https://example.com", opener=recorder)
        feature = {
            "geometry": {"type": "Point", "coordinates": [0, 0]},
            "properties": {"name": "A"},
        }
        self.assertEqual(client.create_feature("places", feature), 9)
        client.update_feature("places", 9, feature)
        client.delete_feature("places", 9)
        self.assertEqual(
            [request.method for request in recorder.requests], ["POST", "PUT", "DELETE"]
        )


if __name__ == "__main__":
    unittest.main()
