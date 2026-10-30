import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from app import create_app
from src.utils.live_photo import (
    LivePhotoError,
    live_asset_public_url,
    mux_motion_photo,
    register_live_asset,
)


def _jpeg_bytes(size=8, color=(200, 40, 40)):
    image = Image.new("RGB", (size, size), color)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _mp4_bytes(payload=b"live-track"):
    body = payload
    ftyp = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    mdat = (8 + len(body)).to_bytes(4, "big") + b"mdat" + body
    return ftyp + mdat


class LivePhotoMuxTest(unittest.TestCase):
    def test_mux_embeds_motion_photo_xmp_and_video_trailer(self):
        jpeg = _jpeg_bytes()
        video = _mp4_bytes()
        out = mux_motion_photo(jpeg, video, timestamp_us=12000)
        self.assertTrue(out.startswith(b"\xff\xd8"))
        self.assertTrue(out.endswith(video))
        self.assertIn(b"GCamera:MotionPhoto=\"1\"", out)
        self.assertIn(b"GCamera:MicroVideo=\"1\"", out)
        self.assertIn(f'GCamera:MicroVideoOffset="{len(video)}"'.encode("ascii"), out)
        self.assertIn(b"Item:Semantic=\"MotionPhoto\"", out)
        self.assertTrue(out.endswith(video))
        jpeg_end = len(out) - len(video)
        self.assertEqual(out[jpeg_end - 2:jpeg_end], b"\xff\xd9")

    def test_mux_converts_webp_still_to_jpeg(self):
        image = Image.new("RGB", (6, 6), (10, 80, 180))
        buffer = BytesIO()
        image.save(buffer, format="WEBP")
        out = mux_motion_photo(buffer.getvalue(), _mp4_bytes())
        self.assertTrue(out.startswith(b"\xff\xd8"))
        self.assertIn(b"GCamera:MotionPhoto=\"1\"", out)

    def test_is_mostly_black_image_detects_placeholder(self):
        from src.utils.live_photo import _is_mostly_black_image

        self.assertTrue(_is_mostly_black_image(_jpeg_bytes(16, (0, 0, 0))))
        self.assertFalse(_is_mostly_black_image(_jpeg_bytes(16, (200, 40, 40))))

    def test_mux_rejects_empty_parts(self):
        with self.assertRaises(LivePhotoError) as ctx:
            mux_motion_photo(b"", _mp4_bytes())
        self.assertEqual(ctx.exception.code, "LIVE_PHOTO_BUILD_FAILED")


class LivePhotoApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def parser(self, **overrides):
        from unittest.mock import Mock
        values = {
            "title": "实况图集",
            "desc": None,
            "video_url": None,
            "video_list": [],
            "cover_url": None,
            "author": None,
            "image_list": [{
                "url": "https://example.com/still.jpg",
                "live_photo_url": "https://example.com/live.mp4",
            }],
            "audio_url": None,
            "subtitles": None,
        }
        values.update(overrides)
        parser = Mock()
        parser.get_title_content.return_value = values["title"]
        parser.get_description.return_value = values["desc"]
        parser.get_real_video_url.return_value = values["video_url"]
        parser.get_video_list.return_value = values["video_list"]
        parser.get_cover_photo_url.return_value = values["cover_url"]
        parser.get_author_info.return_value = values["author"]
        parser.get_image_list.return_value = values["image_list"]
        parser.get_audio_url.return_value = values["audio_url"]
        parser.get_subtitles.return_value = values["subtitles"]
        return parser

    def test_parse_adds_live_asset_url(self):
        parser = self.parser()
        with patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.xiaohongshu.com/explore/1"):
            with patch("src.api.parse.ParserFactory.create_parser", return_value=parser):
                response = self.client.post("/api/parse", json={"text": "https://www.xiaohongshu.com/explore/1"})
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["data"]["image_list"][0]
        self.assertEqual(item["url"], "https://example.com/still.jpg")
        self.assertEqual(item["live_photo_url"], "https://example.com/live.mp4")
        self.assertIn("/api/v1/live-photo/", item["live_asset_url"])
        self.assertTrue(item["live_asset_url"].endswith("/LIVEMP.jpg"))
        self.assertTrue(item["live_asset_path"].endswith("/LIVEMP.jpg"))
        self.assertIn("/api/v1/live-photo/", item["live_asset_path"])
        token = item["live_asset_url"].rsplit("/live-photo/", 1)[-1].split("/")[0]
        with self.app.app_context():
            self.assertTrue(live_asset_public_url(token).endswith(f"{token}/LIVEMP.jpg"))

    def test_live_photo_endpoint_returns_jpeg(self):
        jpeg = _jpeg_bytes()
        video = _mp4_bytes()
        with self.app.app_context():
            token = register_live_asset("https://example.com/still.jpg", "https://example.com/live.mp4")
        with patch("src.utils.live_photo.download_media", side_effect=[jpeg, video]):
            response = self.client.get(f"/api/v1/live-photo/{token}/LIVEMP.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertIn("LIVEMP.jpg", response.headers.get("Content-Disposition", ""))
        self.assertTrue(response.data.startswith(b"\xff\xd8"))
        self.assertIn(b"GCamera:MotionPhoto=\"1\"", response.data)
        self.assertTrue(response.data.endswith(video))
        with patch("src.utils.live_photo.download_media", side_effect=[jpeg, video]):
            legacy = self.client.get(f"/api/v1/live-photo/{token}")
        self.assertEqual(legacy.status_code, 200)
        self.assertTrue(legacy.data.startswith(b"\xff\xd8"))
        with patch("src.utils.live_photo.download_media", side_effect=[jpeg, video]):
            dotted = self.client.get(f"/api/v1/live-photo/{token}.jpg")
        self.assertEqual(dotted.status_code, 200)
        self.assertTrue(dotted.data.startswith(b"\xff\xd8"))

    def test_unknown_token_expires(self):
        response = self.client.get("/api/v1/live-photo/not-a-real-token")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["succ"])
        self.assertEqual(payload["error_code"], "LIVE_PHOTO_EXPIRED")

    def test_download_rejects_private_hosts(self):
        from src.utils.live_photo import download_media
        with self.assertRaises(LivePhotoError) as ctx:
            download_media("http://127.0.0.1/secret.jpg", 1024)
        self.assertEqual(ctx.exception.code, "LIVE_PHOTO_INVALID")

    def test_live_asset_public_url_keeps_non_default_port_behind_proxy(self):
        with self.app.test_request_context(
            base_url="https://after7lab.cloud:81/",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "after7lab.cloud",
                "Host": "after7lab.cloud:81",
            },
        ):
            url = live_asset_public_url("testToken12")
        self.assertEqual(
            url,
            "https://after7lab.cloud:81/api/v1/live-photo/testToken12/LIVEMP.jpg",
        )

    def test_live_asset_public_url_respects_api_base_url_env(self):
        with patch.dict(os.environ, {"API_BASE_URL": "https://cdn.example.com:8443"}, clear=False):
            with self.app.test_request_context(
                base_url="https://after7lab.cloud:81/",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "after7lab.cloud",
                    "Host": "after7lab.cloud:81",
                },
            ):
                url = live_asset_public_url("envToken")
        self.assertEqual(
            url,
            "https://cdn.example.com:8443/api/v1/live-photo/envToken/LIVEMP.jpg",
        )
