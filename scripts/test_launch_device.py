import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


LAUNCHER = Path(__file__).with_name("launch_device.sh")


class KioskLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.result = self.root / "browser.json"
        self.environment = {
            **os.environ,
            "PATH": f"{self.root}:{os.environ['PATH']}",
            "TEST_HOME": str(self.root / "home"),
            "TEST_RESULT": str(self.result),
            "TEST_UID": str(os.geteuid()),
        }
        self.stub("ps", 'if [[ "$*" == *gnome-shell* ]]; then printf "kioskuser\\n"; else printf "Xwayland :99 -auth /tmp/kiosk-test-auth\\n"; fi')
        self.stub("id", 'if [[ "$1" == -u ]]; then printf "%s\\n" "$TEST_UID"; else printf "kioskgroup\\n"; fi')
        self.stub("getent", 'printf "kioskuser:x:1000:1000::%s:/bin/bash\\n" "$TEST_HOME"')
        self.stub("install", '/bin/mkdir -p "${@: -1}"')
        self.stub("runuser", 'shift 3; exec "$@"')
        self.stub("pkill", 'printf "unexpected pkill\\n" >&2; exit 99')
        self.stub("curl", 'exit "${TEST_HTTP_STATUS:-0}"')
        browser = f"""#!{os.sys.executable}
import json
import os
from pathlib import Path
import sys
Path(os.environ['TEST_RESULT']).write_text(json.dumps({{
    'pid': os.getpid(),
    'args': sys.argv[1:],
    'display': os.environ.get('DISPLAY'),
    'backend': os.environ.get('GDK_BACKEND'),
}}))
sys.exit(42)
"""
        target = self.root / "firefox"
        target.write_text(browser)
        target.chmod(0o755)

    def stub(self, name, body):
        target = self.root / name
        target.write_text(f"#!/bin/bash\n{body}\n")
        target.chmod(0o755)

    def launch(self, url="http://127.0.0.1:8787/"):
        process = subprocess.Popen(
            ["bash", str(LAUNCHER), url],
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        return process, stdout, stderr

    def test_foreground_browser_propagates_exit_without_killing(self):
        process, stdout, stderr = self.launch()
        self.assertEqual(process.returncode, 42, stdout + stderr)
        self.assertNotIn("unexpected pkill", stderr)
        result = json.loads(self.result.read_text())
        self.assertEqual(result["display"], ":99")
        self.assertEqual(result["backend"], "x11")
        if os.geteuid() != 0:
            self.assertEqual(result["pid"], process.pid)
        self.assertIn("--kiosk", result["args"])

    def test_cache_buster_preserves_query_and_fragment(self):
        process, stdout, stderr = self.launch("http://127.0.0.1:8787/?mode=live#camera")
        self.assertEqual(process.returncode, 42, stdout + stderr)
        result = json.loads(self.result.read_text())
        url = urlsplit(result["args"][-1])
        query = parse_qs(url.query)
        self.assertEqual(query["mode"], ["live"])
        self.assertTrue(query["kiosk_start"][0].isdigit())
        self.assertEqual(url.fragment, "camera")

    def test_missing_desktop_fails_instead_of_reporting_success(self):
        self.stub("ps", "exit 1")
        process, stdout, stderr = self.launch()
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("No GNOME desktop session found", stdout + stderr)
        self.assertFalse(self.result.exists())

    def test_unreachable_backend_does_not_open_an_error_page(self):
        self.environment["TEST_HTTP_STATUS"] = "7"
        process, stdout, stderr = self.launch()
        self.assertEqual(process.returncode, 7, stdout + stderr)
        self.assertFalse(self.result.exists())

    @unittest.skipUnless(os.geteuid() == 0, "requires root launcher branch")
    def test_root_launch_preserves_browser_exit_through_runuser(self):
        self.environment["TEST_UID"] = "1000"
        process, stdout, stderr = self.launch()
        self.assertEqual(process.returncode, 42, stdout + stderr)
        self.assertTrue(self.result.exists())


if __name__ == "__main__":
    unittest.main()