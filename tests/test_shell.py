"""The offline shell: what the served sw.js tells phones to cache, and when it
tells them the cache is stale. Needs no database."""

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"


def served_sw(client):
    res = client.get("/sw.js")
    assert res.status_code == 200, res.status_code
    version = re.search(r"const SHELL_VERSION = (\"[^\"]*\");", res.text)
    urls = re.search(r"const SHELL_URLS = (\[.*?\]);", res.text)
    assert version and urls, "placeholders were not filled in"
    return res, json.loads(version.group(1)), json.loads(urls.group(1))


class ServedWorkerTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_placeholders_are_filled_and_cache_is_disabled(self):
        res, version, _ = served_sw(self.client)
        self.assertNotIn("__SHELL_", res.text)
        self.assertRegex(version, r"^[0-9a-f]{16}$")
        self.assertEqual(res.headers["cache-control"], "no-cache")
        self.assertIn("javascript", res.headers["content-type"])

    def test_version_endpoint_matches_worker(self):
        _, version, _ = served_sw(self.client)
        self.assertEqual(self.client.get("/api/v1/version").json()["shell_version"], version)

    def test_every_static_file_is_cached_except_the_excluded_ones(self):
        """The whole point: a new file in static/ must reach phones' offline
        cache without anyone editing a list."""
        _, _, urls = served_sw(self.client)
        expected = {"/app"} | {
            f"/static/{p.relative_to(STATIC).as_posix()}" for p in STATIC.rglob("*")
            if p.is_file() and p.name not in main.SHELL_EXCLUDE
            and not any(part.startswith(".") for part in p.relative_to(STATIC).parts)}
        self.assertEqual(set(urls), expected)
        for name in ("/static/sw.js", "/static/selftest.html", "/static/index.html"):
            self.assertNotIn(name, urls)

    def test_every_cached_url_is_actually_servable(self):
        """Install fails outright if any one file 404s, and then nothing works offline."""
        _, _, urls = served_sw(self.client)
        for u in urls:
            self.assertEqual(self.client.get(u).status_code, 200, u)


class FingerprintTest(unittest.TestCase):
    """Run against a scratch copy of static/ so real files are never touched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fielddesk-shell-"))
        self.static = self.tmp / "static"
        shutil.copytree(STATIC, self.static)
        patcher = mock.patch.object(main, "STATIC_DIR", self.static)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_editing_a_file_changes_the_version(self):
        before = main.shell_version()
        (self.static / "app.js").write_text((self.static / "app.js").read_text() + "\n// edit\n")
        self.assertNotEqual(main.shell_version(), before)

    def test_editing_index_html_changes_the_version(self):
        before = main.shell_version()
        (self.static / "index.html").write_text("<!doctype html><p>changed</p>")
        self.assertNotEqual(main.shell_version(), before)

    def test_adding_a_file_lists_it_and_changes_the_version(self):
        before = main.shell_version()
        (self.static / "image_leads.js").write_text("export {};\n")
        self.assertIn("/static/image_leads.js", main.shell_urls())
        self.assertNotEqual(main.shell_version(), before)

    def test_hidden_files_are_ignored(self):
        before = main.shell_version()
        (self.static / ".DS_Store").write_bytes(b"\0junk")
        self.assertEqual(main.shell_version(), before)
        self.assertNotIn("/static/.DS_Store", main.shell_urls())

    def test_missing_placeholder_fails_loudly(self):
        sw = self.static / "sw.js"
        sw.write_text(sw.read_text().replace("[/* __SHELL_URLS__ */]", "[]"))
        with self.assertRaises(RuntimeError):
            main.render_service_worker()


if __name__ == "__main__":
    unittest.main()
