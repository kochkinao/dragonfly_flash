import os
import unittest
from pathlib import Path

ROOT = Path(__file__).parent


class PM2DeployTests(unittest.TestCase):
    def test_pm2_bundle_contains_expected_processes_and_cache_flags(self):
        ecosystem = ROOT / 'ecosystem.config.cjs'
        deploy = ROOT / 'scripts' / 'deploy_prod.sh'
        poller = ROOT / 'scripts' / 'prod_branch_poller.sh'
        status = ROOT / 'scripts' / 'pm2_status.sh'

        for path in (ecosystem, deploy, poller, status):
            self.assertTrue(path.exists(), f'missing {path.relative_to(ROOT)}')

        text = ecosystem.read_text(encoding='utf-8')
        for name in (
            'dragonfly-watch',
            'dragonfly-feed-cache',
            'dragonfly-stats-hot',
            'dragonfly-stats-cold',
            'dragonfly-comments-0',
            'dragonfly-comments-17',
            'dragonfly-comments-34',
        ):
            self.assertIn(name, text)
        self.assertIn('refresh-feed-cache-watch', text)
        self.assertIn('--use-feed-cache', text)
        self.assertIn('DRAGONFLY_ENV_FILE', text)
        self.assertNotIn('/home/wacotal', text)
        self.assertNotIn('TELEGRAM_BOT_TOKEN=', text)
        self.assertNotIn('Domain name.txt', text)

        deploy_text = deploy.read_text(encoding='utf-8')
        self.assertIn('origin/prod', deploy_text)
        self.assertIn('git reset --hard', deploy_text)
        self.assertIn('python3 -m py_compile', deploy_text)
        self.assertIn('doctor --no-network', deploy_text)
        self.assertIn('pm2 startOrReload ecosystem.config.cjs --update-env', deploy_text)

        poller_text = poller.read_text(encoding='utf-8')
        self.assertIn('scripts/deploy_prod.sh', poller_text)
        self.assertIn('origin/prod', poller_text)
        self.assertIn('PM2_DEPLOY_INTERVAL', poller_text)

        for script in (deploy, poller, status):
            self.assertTrue(os.access(script, os.X_OK), f'{script.name} must be executable')


if __name__ == '__main__':
    unittest.main()
