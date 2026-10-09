module.exports = {
  apps: [
    {
      name: "montada-api",

      cwd: "/var/www/vhosts/api.themontada.com/httpdocs/Montada-Backend/Montada",

      script:
        "/var/www/vhosts/api.themontada.com/httpdocs/Montada-Backend/venv/bin/gunicorn",

      args: [
        "Montada.asgi:application",
        "-k", "uvicorn_worker.UvicornWorker",
        "--workers", "3",
        "--bind", "127.0.0.1:8000",
        "--timeout", "120",
        "--graceful-timeout", "30",
        "--keep-alive", "5",
        "--max-requests", "2000",
        "--max-requests-jitter", "200"
      ].join(" "),

      interpreter: "none",
      exec_mode: "fork",

      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      
      kill_timeout: 30000,

      watch: false,

      env: {
        PYTHONUNBUFFERED: "1"
      },

      out_file: "/root/.pm2/logs/montada-api-out.log",
      error_file: "/root/.pm2/logs/montada-api-error.log",
      merge_logs: true,
      time: true
    }
  ]
};
