const ROOT = "/var/www/vhosts/uat.themontada.com/httpdocs/Montada-Backend";
const PYTHON = "/var/www/vhosts/uat.themontada.com/httpdocs/venv/bin/python";
const LOG_DIR = "/root/.pm2/logs";

// Shared options: every process is started/stopped/restarted together with the API
// (`pm2 start|restart|stop|reload ecosystem.config.cjs` or `pm2 restart all`).
const base = {
  cwd: `${ROOT}/Montada`,
  interpreter: "none",
  exec_mode: "fork",
  watch: false,
  env: { PYTHONUNBUFFERED: "1" },
  merge_logs: true,
  time: true,
};

// Long-running `manage.py` command that PM2 keeps alive.
const worker = (name, command, extra = {}) => ({
  ...base,
  name,
  script: PYTHON,
  args: `manage.py ${command}`,
  autorestart: true,
  max_restarts: 10,
  restart_delay: 5000,
  kill_timeout: 15000,
  out_file: `${LOG_DIR}/${name}-out.log`,
  error_file: `${LOG_DIR}/${name}-error.log`,
  ...extra,
});

// One-shot `manage.py` command: runs when PM2 starts/restarts, then again on the cron
// schedule. Exits when done (autorestart off) and shows as "stopped" between runs.
const job = (name, command, cron) =>
  worker(name, command, { autorestart: false, cron_restart: cron });

module.exports = {
  apps: [
    {
      ...base,
      name: "montada-api",

      script: "/var/www/vhosts/uat.themontada.com/httpdocs/venv/bin/gunicorn",

      args: [
        "Montada.asgi:application",
        "-k", "uvicorn_worker.UvicornWorker",
        "--workers", "3",
        "--bind", "127.0.0.1:1313",
        "--timeout", "120",
        "--graceful-timeout", "30",
        "--keep-alive", "5",
        "--max-requests", "2000",
        "--max-requests-jitter", "200"
      ].join(" "),

      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,

      kill_timeout: 30000,

      out_file: `${LOG_DIR}/montada-api-out.log`,
      error_file: `${LOG_DIR}/montada-api-error.log`,
    },

    // ---- Long-running background commands -------------------------------------
    worker("montada-signal-notifications", "poll_signal_change_notifications"),
    worker("montada-news-stream", "run_fxstreet_news_stream"),
    worker("montada-economic-reminders", "run_economic_calendar_reminders --schedule"),
    worker("montada-poll-scheduler", "update_poll_statuses --loop --interval 60"),

    // ---- Periodic one-shot commands -------------------------------------------
    job("montada-fetch-economic-calendar", "fetch_economic_calendar", "0 */6 * * *"),
    job("montada-cleanup-push-devices", "cleanup_push_devices", "0 3 * * *"),
  ]
};
