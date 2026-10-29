from io import BytesIO
import os
import plistlib
import tempfile
import unittest

from app import create_app
from src.auth import SHORTCUT_KEY_NAME
from src.db import get_db
from src.utils.shortcut_builder import build_shortcut_plist


class ShortcutOnboardingTest(unittest.TestCase):
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

    def csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    def test_shortcut_pages_are_public(self):
        guide = self.client.get("/shortcut")
        self.assertEqual(guide.status_code, 200)
        html = guide.get_data(as_text=True)
        self.assertIn("获取快捷指令", html)
        self.assertIn("/shortcut.shortcut", html)
        self.assertIn("去注册，自动拿密钥", html)

        runner = self.client.get("/s", query_string={"text": "看看 https://v.douyin.com/abc/"})
        self.assertEqual(runner.status_code, 200)
        body = runner.get_data(as_text=True)
        self.assertIn("v.douyin.com/abc", body)
        self.assertIn("/api/v1/parse", body)

    def test_shortcut_file_contains_parser_url(self):
        response = self.client.get("/shortcut.shortcut")
        self.assertEqual(response.status_code, 200)
        self.assertIn("shortcut", response.headers.get("Content-Disposition", ""))
        workflow = plistlib.loads(response.data)
        self.assertEqual(workflow["WFWorkflowName"], "媒体解析")
        self.assertIn("ActionExtension", workflow["WFWorkflowTypes"])
        dumped = str(workflow)
        self.assertIn("/s?text=", dumped)

    def test_builder_uses_current_host(self):
        payload = build_shortcut_plist("https://after7lab.cloud:81")
        workflow = plistlib.loads(payload)
        self.assertIn("https://after7lab.cloud:81/s?text=", str(workflow))

    def test_register_auto_login_and_creates_shortcut_key(self):
        response = self.client.post(
            "/auth/register",
            data={
                "csrf_token": self.csrf(),
                "username": "phone_user",
                "password": "password123",
                "confirm_password": "password123",
                "from": "shortcut",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/console/welcome", response.headers["Location"])
        self.assertIn("from=shortcut", response.headers["Location"])

        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("user_id"))
            self.assertTrue(str(sess.get("new_api_key", "")).startswith("mp-"))

        welcome = self.client.get("/console/welcome?from=shortcut", follow_redirects=True)
        self.assertEqual(welcome.status_code, 200)
        html = welcome.get_data(as_text=True)
        self.assertIn("mp-", html)
        self.assertIn("复制密钥", html)
        self.assertIn("365 天免费试用", html)

        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE username='phone_user'").fetchone()
            key = get_db().execute(
                "SELECT * FROM api_keys WHERE user_id=?", (user["id"],)
            ).fetchone()
            self.assertIsNotNone(key)
            self.assertEqual(key["name"], SHORTCUT_KEY_NAME)
            self.assertTrue(key["key"].startswith("mp-"))

    def test_register_keeps_existing_admin_session(self):
        self.client.post(
            "/auth/setup",
            data={
                "csrf_token": self.csrf(),
                "username": "admin",
                "password": "password123",
                "confirm_password": "password123",
            },
        )
        self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"},
        )
        response = self.client.post(
            "/auth/register",
            data={
                "csrf_token": self.csrf(),
                "username": "created_by_admin",
                "password": "password123",
                "confirm_password": "password123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/login"))
        with self.client.session_transaction() as sess:
            admin = None
            with self.app.app_context():
                admin = get_db().execute("SELECT id FROM users WHERE username='admin'").fetchone()
            self.assertEqual(sess.get("user_id"), admin["id"])
        with self.app.app_context():
            created = get_db().execute(
                "SELECT id FROM users WHERE username='created_by_admin'"
            ).fetchone()
            key = get_db().execute(
                "SELECT name FROM api_keys WHERE user_id=?", (created["id"],)
            ).fetchone()
            self.assertEqual(key["name"], SHORTCUT_KEY_NAME)

    def test_login_from_shortcut_opens_welcome(self):
        self.client.post(
            "/auth/register",
            data={
                "csrf_token": self.csrf(),
                "username": "back_user",
                "password": "password123",
                "confirm_password": "password123",
            },
        )
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["csrf_token"] = "test-csrf"
        response = self.client.post(
            "/auth/login",
            data={
                "csrf_token": "test-csrf",
                "username": "back_user",
                "password": "password123",
                "from": "shortcut",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/console/welcome", response.headers["Location"])
        self.assertIn("from=shortcut", response.headers["Location"])

    def test_landing_links_to_shortcut_guide(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("/shortcut", html)
        self.assertIn("iPhone 快捷指令", html)

    def login_admin(self):
        self.client.post(
            "/auth/setup",
            data={
                "csrf_token": self.csrf(),
                "username": "admin",
                "password": "password123",
                "confirm_password": "password123",
            },
        )
        self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"},
        )

    @staticmethod
    def custom_shortcut_bytes(name="自定义指令"):
        return plistlib.dumps(
            {
                "WFWorkflowName": name,
                "WFWorkflowActions": [{
                    "WFWorkflowActionIdentifier": "is.workflow.actions.comment",
                    "WFWorkflowActionParameters": {"WFCommentActionText": name},
                }],
                "WFWorkflowTypes": ["ActionExtension"],
            },
            fmt=plistlib.FMT_BINARY,
        )

    def test_admin_can_replace_shortcut_file_without_rebuild(self):
        self.login_admin()
        uploaded = self.custom_shortcut_bytes()
        response = self.client.post(
            "/admin/shortcut",
            data={
                "csrf_token": self.csrf(),
                "action": "save",
                "shortcut_icloud_url": "",
                "shortcut_file": (BytesIO(uploaded), "custom.shortcut"),
            },
        )
        self.assertEqual(response.status_code, 302)

        downloaded = self.client.get("/shortcut.shortcut")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, uploaded)
        self.assertEqual(plistlib.loads(downloaded.data)["WFWorkflowName"], "自定义指令")

        settings = self.client.get("/admin/settings")
        self.assertIn("你上传的文件", settings.get_data(as_text=True))

        restored = self.client.post(
            "/admin/shortcut",
            data={"csrf_token": self.csrf(), "action": "restore", "shortcut_icloud_url": ""},
        )
        self.assertEqual(restored.status_code, 302)
        generated = self.client.get("/shortcut.shortcut")
        self.assertEqual(plistlib.loads(generated.data)["WFWorkflowName"], "媒体解析")

    def test_admin_icloud_link_changes_install_button(self):
        self.login_admin()
        icloud = "https://www.icloud.com/shortcuts/abcd1234"
        response = self.client.post(
            "/admin/shortcut",
            data={
                "csrf_token": self.csrf(),
                "action": "save",
                "shortcut_icloud_url": icloud,
            },
        )
        self.assertEqual(response.status_code, 302)

        guide = self.client.get("/shortcut")
        html = guide.get_data(as_text=True)
        self.assertIn(icloud, html)
        self.assertIn("将通过 Apple iCloud 安装", html)

        rejected = self.client.post(
            "/admin/shortcut",
            data={
                "csrf_token": self.csrf(),
                "action": "save",
                "shortcut_icloud_url": "https://example.com/not-icloud",
            },
        )
        self.assertEqual(rejected.status_code, 302)
        still = self.client.get("/shortcut").get_data(as_text=True)
        self.assertIn(icloud, still)

    def test_invalid_shortcut_upload_is_rejected(self):
        self.login_admin()
        self.client.post(
            "/admin/shortcut",
            data={
                "csrf_token": self.csrf(),
                "action": "save",
                "shortcut_icloud_url": "",
                "shortcut_file": (BytesIO(b"not a shortcut"), "fake.shortcut"),
            },
        )
        downloaded = self.client.get("/shortcut.shortcut")
        self.assertEqual(plistlib.loads(downloaded.data)["WFWorkflowName"], "媒体解析")
