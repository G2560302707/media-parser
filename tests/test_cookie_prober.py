"""Cookie 双跑对照探针测试：全部 mock 解析链路，不出网。"""

import unittest
from unittest.mock import patch

from src.utils import cookie_registry
from src.utils.cookie_prober import _judge, _run_parse, probe_platform


def _ok_result(live_count=0, image_count=3):
    return {
        "ok": True,
        "reason": "OK",
        "detail": {"has_video": True, "image_count": image_count, "live_count": live_count},
        "live_count": live_count,
    }


def _fail_result(reason="MEDIA_NOT_FOUND"):
    return {"ok": False, "reason": reason, "detail": {}, "live_count": 0}


class JudgeMatrixTest(unittest.TestCase):
    def test_both_ok_no_gain(self):
        status, detail = _judge("douyin", _ok_result(0), _ok_result(0))
        self.assertEqual(status, "healthy")
        self.assertEqual(detail["signal"], "BOTH_OK")

    def test_cookie_gain_via_live_count(self):
        status, detail = _judge("douyin", _ok_result(live_count=2), _ok_result(0))
        self.assertEqual(status, "healthy")
        self.assertEqual(detail["signal"], "COOKIE_GAIN")

    def test_with_ok_without_fail(self):
        status, detail = _judge("douyin", _ok_result(), _fail_result())
        self.assertEqual(status, "healthy")
        self.assertEqual(detail["signal"], "COOKIE_GAIN")

    def test_with_fail_without_ok_is_stale(self):
        status, detail = _judge("douyin", _fail_result(), _ok_result())
        self.assertEqual(status, "stale")
        self.assertEqual(detail["signal"], "COOKIE_BROKEN")

    def test_both_failed_is_probe_failed(self):
        status, detail = _judge("douyin", _fail_result(), _fail_result("REDIRECT_FAILED"))
        self.assertEqual(status, "probe_failed")
        self.assertEqual(detail["signal"], "BOTH_FAILED")


class ProbePlatformTest(unittest.TestCase):
    def setUp(self):
        # 每个用例重置防抖时间戳，避免用例间串扰。
        import src.utils.cookie_prober as prober
        prober._last_probe_at.clear()

    def test_unregistered_platform(self):
        result = probe_platform("nosuch")
        self.assertEqual(result["status"], "probe_failed")

    def test_not_configured_short_circuits(self):
        with patch("src.utils.cookie_registry.get_cookie_source", return_value="none"):
            result = probe_platform("douyin")
        self.assertEqual(result["status"], "not_configured")

    def test_debounce_blocks_second_call(self):
        def fake_parse(url, platforms):
            return _ok_result()

        with patch("src.utils.cookie_registry.get_cookie_source", return_value="database"), \
             patch("src.utils.cookie_prober._canary_url", return_value="https://v.douyin.com/x/"), \
             patch.object(cookie_registry, "record_probe"), \
             patch("src.utils.cookie_prober._run_parse", side_effect=fake_parse):
            self.assertEqual(probe_platform("douyin")["status"], "healthy")
            # 同平台 5 分钟内第二次探针被防抖拦截，不执行解析。
            second = probe_platform("douyin")
        self.assertEqual(second["status"], "debounced")

    def test_full_probe_records_and_judges(self):
        calls = []

        def fake_parse(url, platforms):
            calls.append(url)
            # 第一路（带 Cookie）失败、第二路（无 Cookie 对照）成功 → stale 实锤。
            return _fail_result() if len(calls) == 1 else _ok_result()

        with patch("src.utils.cookie_registry.get_cookie_source", return_value="database"), \
             patch("src.utils.cookie_prober._canary_url", return_value="https://v.douyin.com/x/"), \
             patch("src.utils.cookie_prober._run_parse", side_effect=fake_parse), \
             patch.object(cookie_registry, "record_probe") as record:
            result = probe_platform("douyin")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["detail"]["signal"], "COOKIE_BROKEN")
        self.assertEqual(len(calls), 2)
        record.assert_called_once()
        self.assertEqual(record.call_args[0][1], "stale")

    def test_no_cookie_shadowing_takes_effect(self):
        """_no_cookie 上下文内 cookie_manager 读取被遮蔽。"""
        import src.utils.cookie_manager as cm
        from src.utils.cookie_prober import _no_cookie

        with patch.object(cm, "get_platform_cookie", return_value="sessionid=real;", create=True):
            with _no_cookie():
                self.assertEqual(cm.get_platform_cookie("douyin"), "")
            self.assertEqual(cm.get_platform_cookie("douyin"), "sessionid=real;")

    def test_run_parse_platform_mismatch(self):
        from utils.web_fetcher import UrlParser

        with patch("src.utils.cookie_prober.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/1"), \
             patch.object(UrlParser, "get_platform", return_value="快手"):
            result = _run_parse("https://v.douyin.com/x/", {"抖音"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "PLATFORM_MISMATCH")


if __name__ == "__main__":
    unittest.main()
