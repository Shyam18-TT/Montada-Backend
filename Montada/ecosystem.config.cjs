const path = require("path");

const cwd = __dirname;
const pythonBin =
  process.env.PYTHON_BIN ||
  (process.platform === "win32" ? "python" : "python3");
const webConcurrency = Math.max(
  1,
  parseInt(process.env.WEB_CONCURRENCY || "2", 10)
);
const basePort = parseInt(process.env.PORT || "8000", 10);
const bindHost = process.env.BIND_HOST || "0.0.0.0";

const apps = Array.from({ length: webConcurrency }, (_, index) => {
  const port = String(basePort + index);
  const instanceId = index + 1;

  return {
    name: `montada-asgi-${instanceId}`,
    cwd,
    script: pythonBin,
    args: ["-m", "daphne", "-b", bindHost, "-p", port, "Montada.asgi:application"],
    interpreter: "none",
    exec_mode: "fork",
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    kill_timeout: 15000,
    min_uptime: "10s",
    env: {
      DJANGO_SETTINGS_MODULE: "Montada.settings",
      PYTHONUNBUFFERED: "1",
      PORT: port,
    },
    error_file: path.join(cwd, "logs", `pm2-montada-${instanceId}-error.log`),
    out_file: path.join(cwd, "logs", `pm2-montada-${instanceId}-out.log`),
    merge_logs: false,
    time: true,
  };
});

module.exports = {
  apps,
};
