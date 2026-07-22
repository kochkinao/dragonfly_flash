// PM2 production process file for Dragonfly Flash Telegram bridge.
// Secrets are loaded by dragonfly_telegram_poster.py from DRAGONFLY_ENV_FILE;
// keep real tokens in ~/dragonfly.env, never in this repository.
const envFile = process.env.DRAGONFLY_ENV_FILE || `${process.env.HOME}/dragonfly/dragonfly.env`;
const stateDir = process.env.DRAGONFLY_STATE_DIR || `${process.env.HOME}/dragonfly/state`;
const logDir = process.env.DRAGONFLY_LOG_DIR || `${process.env.HOME}/dragonfly/logs`;
const python = process.env.PYTHON || 'python3';
const dbFile = `${stateDir}/dragonfly_telegram_poster.sqlite3`;
const appLogFile = `${logDir}/dragonfly_telegram_poster.log`;
const baseEnv = {
  PYTHONUNBUFFERED: '1',
  DRAGONFLY_ENV_FILE: envFile,
  DRAGONFLY_STATE_DIR: stateDir,
  DRAGONFLY_LOG_DIR: logDir,
};

function app(name, args, extraEnv = {}) {
  return {
    name,
    script: python,
    args,
    cwd: __dirname,
    interpreter: 'none',
    autorestart: true,
    max_restarts: 20,
    min_uptime: '10s',
    restart_delay: 10000,
    kill_timeout: 15000,
    env: { ...baseEnv, ...extraEnv },
    out_file: `${logDir}/${name}.out.log`,
    error_file: `${logDir}/${name}.err.log`,
    merge_logs: true,
    time: true,
  };
}

const common = [
  'dragonfly_telegram_poster.py',
  '--env-file', envFile,
  '--db', dbFile,
  '--log-file', appLogFile,
];

module.exports = {
  apps: [
    app('dragonfly-watch', [
      ...common,
      '--request-delay', '2',
      '--send-delay', '2',
      '--text-delay', '2',
      '--photo-delay', '5',
      '--album-delay', '5',
      '--animation-delay', '5',
      '--mixed-media-delay', '5',
      '--media-item-delay', '2',
      '--poll-interval', '15',
      '--max-attempts', '3',
      '--keep-sent', '50000',
      'watch',
      '--max-gap-scan', '200',
    ]),
    app('dragonfly-feed-cache', [
      ...common,
      '--dragonfly-account', 'backup_1',
      '--request-delay', '2',
      'refresh-feed-cache-watch',
      '--count', '60',
      '--offset', '0',
      '--interval', '20',
    ]),
    app('dragonfly-stats-hot', [
      ...common,
      '--dragonfly-account', 'backup_1',
      '--request-delay', '2',
      '--poll-interval', '15',
      'sync-stats-watch',
      '--count', '20',
      '--offset', '0',
      '--interval', '30',
      '--use-feed-cache',
    ]),
    app('dragonfly-stats-cold', [
      ...common,
      '--dragonfly-account', 'backup_1',
      '--request-delay', '2',
      '--poll-interval', '15',
      'sync-stats-watch',
      '--count', '30',
      '--offset', '20',
      '--interval', '60',
      '--use-feed-cache',
    ]),
    app('dragonfly-comments-0', [
      ...common,
      '--dragonfly-account', 'backup_2',
      '--request-delay', '2',
      'sync-comments-watch',
      '--count', '17',
      '--offset', '0',
      '--interval', '30',
      '--send-existing',
      '--hot-count', '20',
      '--use-feed-cache',
    ]),
    app('dragonfly-comments-17', [
      ...common,
      '--dragonfly-account', 'backup_3',
      '--request-delay', '2',
      'sync-comments-watch',
      '--count', '17',
      '--offset', '17',
      '--interval', '30',
      '--send-existing',
      '--hot-count', '20',
      '--use-feed-cache',
    ]),
    app('dragonfly-comments-34', [
      ...common,
      '--dragonfly-account', 'backup_4',
      '--request-delay', '2',
      'sync-comments-watch',
      '--count', '16',
      '--offset', '34',
      '--interval', '30',
      '--send-existing',
      '--hot-count', '20',
      '--use-feed-cache',
    ]),
  ],
};
