"""Release contains every regression test and fixture; never relies on an old EXE."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import build_release

class ReleaseTests(unittest.TestCase):
    def test_source_archive_has_tests_fixtures_and_no_runtime_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(build_release, 'DIST', folder):
            path = build_release.zip_sources()
            with zipfile.ZipFile(path) as z:
                self.assertIsNone(z.testzip())
                names = {n.split('/',1)[1] for n in z.namelist()}
                root = Path(build_release.ROOT)
                self.assertTrue({p.relative_to(root).as_posix() for p in (root/'tests').glob('test_*.py')} <= names)
                self.assertIn('tests/fixtures/live_20261117/cinema_box_200.json', names)
                self.assertIn('tools/controlled_live_probe.py', names)
                self.assertFalse(any(n.startswith(('.git/', '.v1.5.1_previous/', 'raw/', 'output/')) or n.endswith('.db') for n in names))

    def test_source_only_does_not_build_windows_executable(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(build_release, 'DIST', folder), \
             patch('sys.argv', ['build_release.py', '--source-only']), \
             patch.object(build_release, 'build_exe', side_effect=AssertionError('unexpected EXE build')):
            self.assertEqual(build_release.main(), 0)
