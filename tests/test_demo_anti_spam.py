import os
import tempfile
import unittest
from app import create_app
from src.api.access import rate_limiter


from unittest.mock import Mock, patch


class DemoAntiSpamTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            rate_limiter.reset()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_honeypot_trap_rejection(self):
        """测试盲填蜜罐字段 website 的自动化请求被拒绝 400"""
        response = self.client.post("/api/parse", json={
            "text": "https://v.douyin.com/abc1234/",
            "website": "bot-filled-trap"
        })
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["succ"])
        self.assertEqual(data["error_code"], "INVALID_REQUEST")

    @patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/7341234567890123456")
    @patch("src.api.parse.ParserFactory.create_parser")
    def test_ip_rate_limiting(self, mock_factory, mock_fetch):
        """测试同一 IP 短时间内超过 5 次体验请求被限流 429"""
        mock_parser = Mock()
        mock_parser.get_title_content.return_value = "测试"
        mock_parser.get_real_video_url.return_value = "https://example.com/a.mp4"
        mock_parser.get_video_list.return_value = []
        mock_parser.get_cover_photo_url.return_value = None
        mock_parser.get_author_info.return_value = None
        mock_parser.get_image_list.return_value = []
        mock_parser.get_audio_url.return_value = None
        mock_parser.get_subtitles.return_value = None
        mock_factory.return_value = mock_parser

        self.app.testing = False
        # 生产模式（testing=False）：未登录且未携带 API Key，匿名解析一律 401 拒绝
        for i in range(6):
            res = self.client.post("/api/parse", json={"text": f"https://v.douyin.com/test{i}/"})
            self.assertEqual(res.status_code, 401)
            data = res.get_json()
            self.assertFalse(data["succ"])
            self.assertEqual(data["error_code"], "API_KEY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
