import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / 'scripts' / 'install_systemd_user.py'
spec = importlib.util.spec_from_file_location('install_systemd_user', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SystemdInstallTests(unittest.TestCase):
    def test_rendered_units_are_portable_and_complete(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'units'
            project = Path(td) / 'checkout'
            project.mkdir()
            (project / 'dragonfly_telegram_poster.py').write_text('# stub\n', encoding='utf-8')
            env = Path(td) / 'dragonfly.env'
            env.write_text('TELEGRAM_BOT_TOKEN=\n', encoding='utf-8')

            paths = installer.render_units(project_dir=project, env_file=env, output_dir=out)
            names = {p.name for p in paths}
            rendered = '\n'.join(p.read_text(encoding='utf-8') for p in paths)

        self.assertEqual(names, {
            'dragonfly-bridge.target',
            'dragonfly-feed-cache.service',
            'dragonfly-watch.service',
            'dragonfly-stats-hot.service',
            'dragonfly-stats-cold.service',
            'dragonfly-comments-0.service',
            'dragonfly-comments-17.service',
            'dragonfly-comments-34.service',
        })
        self.assertNotIn('{{PROJECT_DIR}}', rendered)
        self.assertNotIn('{{ENV_FILE}}', rendered)
        self.assertNotIn('/home/wacotal', rendered)
        self.assertIn('Restart=always', rendered)
        self.assertIn('dragonfly-bridge.target', rendered)
        self.assertIn('refresh-feed-cache-watch --count 60 --offset 0 --interval 20', rendered)
        self.assertIn('sync-stats-watch --count 20 --offset 0 --interval 30 --use-feed-cache', rendered)
        self.assertIn('sync-comments-watch --count 16 --offset 34 --interval 30 --send-existing --hot-count 20 --use-feed-cache', rendered)


if __name__ == '__main__':
    unittest.main()
