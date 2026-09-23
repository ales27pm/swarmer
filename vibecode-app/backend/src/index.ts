import "@vibecodeapp/proxy"; // DO NOT REMOVE OTHERWISE VIBECODE PROXY WILL NOT WORK
import { Hono } from "hono";
import { cors } from "hono/cors";
import "./env";
import "./db";
import { sampleRouter } from "./routes/sample";
import { pairingRouter } from "./routes/pairing";
import { tasksRouter } from "./routes/tasks";
import { approvalsRouter } from "./routes/approvals";
import { memoryRouter } from "./routes/memory";
import { agentsRouter } from "./routes/agents";
import { chatRouter } from "./routes/chat";
import { syncRouter } from "./routes/sync";
import { logger } from "hono/logger";

const app = new Hono();

// CORS middleware - validates origin against allowlist
const allowed = [
  /^http:\/\/localhost(:\d+)?$/,
  /^http:\/\/127\.0\.0\.1(:\d+)?$/,
  /^https:\/\/[a-z0-9-]+\.dev\.vibecode\.run$/,
  /^https:\/\/[a-z0-9-]+\.vibecode\.run$/,
  /^https:\/\/[a-z0-9-]+\.vibecodeapp\.com$/,
  /^https:\/\/[a-z0-9-]+\.vibecode\.dev$/,
  /^https:\/\/vibecode\.dev$/,
  // Freestyle sandbox provider preview domain (dynamically generated per sandbox+port).
  /^https:\/\/[a-z0-9-]+\.style\.dev$/,
  // Daytona sandbox provider preview domain (dynamically generated per sandbox+port; both shared regions).
  /^https:\/\/\d+-[a-z0-9-]+\.daytonaproxy01\.(net|eu)$/,
];

app.use(
  "*",
  cors({
    origin: (origin) => (origin && allowed.some((re) => re.test(origin)) ? origin : null),
    credentials: true,
  })
);

// Logging
app.use("*", logger());

// Health check endpoint
app.get("/health", (c) => c.json({ status: "ok" }));

// Routes
app.route("/api/sample", sampleRouter);
app.route("/api/pairing", pairingRouter);
app.route("/api/tasks", tasksRouter);
app.route("/api/approvals", approvalsRouter);
app.route("/api/memory", memoryRouter);
app.route("/api/agents", agentsRouter);
app.route("/api", chatRouter);
app.route("/api/sync", syncRouter);

const port = Number(process.env.PORT) || 3000;

export default {
  port,
  fetch: app.fetch,
};
