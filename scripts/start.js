// Lanzador de la plataforma: arranca el servidor FastAPI (uvicorn) y abre el
// navegador automáticamente en cuanto responde. Uso: `npm start`.
const { spawn } = require("child_process");
const path = require("path");

const PORT = process.env.PORT || 8011;
const URL = `http://localhost:${PORT}`;
const backend = path.join(__dirname, "..", "backend");

// Servidor en primer plano (hereda stdio: los logs y Ctrl+C funcionan igual).
const server = spawn(
  "python",
  ["-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", String(PORT)],
  { cwd: backend, stdio: "inherit" }
);
server.on("exit", (code) => process.exit(code ?? 0));

// Abre el navegador cuando /health responde (o tras varios intentos).
async function abrirNavegador() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`${URL}/health`);
      if (r.ok) break;
    } catch {
      /* el servidor aún no escucha */
    }
    await new Promise((res) => setTimeout(res, 500));
  }
  const [cmd, args] =
    process.platform === "win32"
      ? ["cmd", ["/c", "start", "", URL]]
      : process.platform === "darwin"
      ? ["open", [URL]]
      : ["xdg-open", [URL]];
  spawn(cmd, args, { stdio: "ignore", detached: true }).unref();
}
abrirNavegador();
