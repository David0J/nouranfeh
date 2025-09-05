// wa_http_server.js
// Local HTTP API for WhatsApp sending (no Docker, no Meta API)
// whatsapp-web.js + Express, with QR and status endpoints for your GUI.
// Lock-proof: rotates Chrome user-data-dirs if a SingletonLock is present.

const express = require("express");
const cors = require("cors");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth } = require("whatsapp-web.js");
const os = require("os");
const path = require("path");
const fs = require("fs");

// ===== Config =====
const PORT = process.env.PORT || 3000;
const CLIENT_ID = process.env.CLIENT_ID || "nour";
const HEADLESS = (process.env.HEADLESS || "true").toLowerCase() !== "false";
const RATE_DELAY_MS = parseInt(process.env.RATE_DELAY_MS || "120", 10);

// Cache strategy (keep "none" to avoid LocalWebCache crash seen earlier)
const WEB_CACHE_STRATEGY = (process.env.WEB_CACHE_STRATEGY || "none").toLowerCase();
const REMOTE_CACHE_PATH =
  process.env.WEB_REMOTE_PATH ||
  "https://raw.githubusercontent.com/wppconnect-team/wa-version/main/wa-version.json";

// Root for Chrome profiles (we may create multiple if locked)
const PROFILES_ROOT = path.join(__dirname, ".chrome_profiles");
const DEFAULT_PROFILE_NAME = CLIENT_ID; // "nour" by default

// ===== Utility =====
function ensureDir(p) {
  try { fs.mkdirSync(p, { recursive: true }); } catch {}
}

function hasSingletonLocks(dir) {
  try {
    return (
      fs.existsSync(path.join(dir, "SingletonLock")) ||
      fs.existsSync(path.join(dir, "SingletonCookie"))
    );
  } catch { return false; }
}

function removeSingletonLocks(dir) {
  try {
    for (const f of ["SingletonLock", "SingletonCookie"]) {
      const p = path.join(dir, f);
      if (fs.existsSync(p)) fs.rmSync(p, { force: true });
    }
  } catch {}
}

function resolveChromePath() {
  if (process.env.PUPPETEER_EXECUTABLE_PATH) return process.env.PUPPETEER_EXECUTABLE_PATH;
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const candidates = [];
  if (os.platform() === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
      "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    );
  } else if (os.platform() === "win32") {
    const pf = process.env["PROGRAMFILES"] || "C:\\\\Program Files";
    const pf86 = process.env["PROGRAMFILES(X86)"] || "C:\\\\Program Files (x86)";
    candidates.push(
      path.join(pf, "Google/Chrome/Application/chrome.exe"),
      path.join(pf86, "Google/Chrome/Application/chrome.exe"),
      path.join(pf, "Microsoft/Edge/Application/msedge.exe"),
      path.join(pf86, "Microsoft/Edge/Application/msedge.exe"),
      path.join(pf, "BraveSoftware/Brave-Browser/Application/brave.exe")
    );
  } else {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/chromium-browser",
      "/usr/bin/chromium",
      "/snap/bin/chromium",
      "/usr/bin/brave-browser",
      "/usr/bin/microsoft-edge"
    );
  }
  for (const p of candidates) { try { if (fs.existsSync(p)) return p; } catch {} }
  return null;
}

function pickProfileDir() {
  ensureDir(PROFILES_ROOT);
  const base = path.join(PROFILES_ROOT, DEFAULT_PROFILE_NAME);
  // Try default profile; if locked, rotate to a timestamped one.
  if (!hasSingletonLocks(base)) return base;

  // Try to clear stale locks once
  removeSingletonLocks(base);
  if (!hasSingletonLocks(base)) return base;

  // Still locked -> rotate
  const stamp = new Date().toISOString().replace(/[:]/g, "-").split(".")[0]; // YYYY-MM-DDTHH-MM-SS
  const alt = path.join(PROFILES_ROOT, `${DEFAULT_PROFILE_NAME}_${stamp}`);
  ensureDir(alt);
  return alt;
}

// Clean WA auth directory singleton files (separate from Chrome profile)
function cleanAuthSingletons() {
  try {
    const sessDir = path.join(__dirname, ".wwebjs_auth", "session-" + CLIENT_ID);
    removeSingletonLocks(sessDir);
  } catch {}
}

// ===== Resolve Chrome =====
const CHROME_PATH = resolveChromePath();
if (!CHROME_PATH) {
  console.warn(
    "[WARN] Could not auto-detect Chrome/Chromium.\n" +
    "Set CHROME_PATH or PUPPETEER_EXECUTABLE_PATH to your browser binary.\n"
  );
}

// ===== Pick profile dir BEFORE creating the client =====
const USER_DATA_DIR = pickProfileDir();
console.log("Chrome profile dir:", USER_DATA_DIR);

// ===== Build whatsapp-web.js client =====
const puppeteerOpts = {
  headless: HEADLESS,
  executablePath: CHROME_PATH || undefined,
  args: [
    `--user-data-dir=${USER_DATA_DIR}`,
    "--profile-directory=Default",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-notifications",
    "--disable-dev-shm-usage",
    "--disable-infobars"
  ]
};

let webVersionCache =
  WEB_CACHE_STRATEGY === "none"
    ? { type: "none" }
    : { type: "remote", remotePath: REMOTE_CACHE_PATH };

const client = new Client({
  authStrategy: new LocalAuth({ clientId: CLIENT_ID }),
  puppeteer: puppeteerOpts,
  webVersionCache
});

// Share QR with GUI
let lastQRData = null;
client.on("qr", (qr) => {
  lastQRData = qr; // raw QR text
  console.log("\nScan this QR (first run only):\n");
  qrcode.generate(qr, { small: true });
});
client.on("ready", () => {
  console.log("WA client ready.");
  lastQRData = null;
});
client.on("auth_failure", (m) => console.error("Auth failure:", m));
client.on("disconnected", (r) => {
  console.log("Disconnected:", r);
});

// Initialize
(async () => {
  try {
    // Clean WA auth singletons and profile locks one more time (belt & suspenders)
    cleanAuthSingletons();
    removeSingletonLocks(USER_DATA_DIR);

    await client.initialize();
  } catch (e) {
    console.error("Failed to initialize WhatsApp client:", e);
    console.error(
      "Tips:\n" +
      "  • Ensure CHROME_PATH is correct (or install Chrome/Edge)\n" +
      "  • Prefer Node.js LTS (v20)\n"
    );
    process.exit(1);
  }
})();

// ===== HTTP API =====
const app = express();
app.use(cors());
app.use(express.json({ limit: "1mb" }));

function normPhone(raw) {
  if (!raw) return "";
  let d = String(raw).replace(/\D+/g, "");
  if (d.startsWith("00")) d = d.slice(2);
  return d; // digits only, no '+'
}

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    headless: HEADLESS,
    clientId: CLIENT_ID,
    webCacheStrategy: WEB_CACHE_STRATEGY,
    chromePath: CHROME_PATH || null,
    userDataDir: USER_DATA_DIR
  });
});

app.get("/status", (_req, res) => {
  res.json({
    ok: true,
    needQr: !!lastQRData,
    ready: !lastQRData
  });
});

app.get("/qr", (_req, res) => {
  if (!lastQRData) return res.status(404).json({ ok: false, error: "no-qr" });
  res.json({ ok: true, qr: lastQRData });
});

app.post("/send", async (req, res) => {
  try {
    const phone = normPhone(req.body.phone);
    const message = (req.body.message || "").toString();
    if (!phone || !message) {
      return res.status(400).json({ ok: false, error: "phone and message required" });
    }
    await client.sendMessage(`${phone}@c.us`, message);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: e?.message || String(e) });
  }
});

app.post("/bulk", async (req, res) => {
  try {
    const items = Array.isArray(req.body.items) ? req.body.items : [];
    const results = [];
    for (const it of items) {
      const phone = normPhone(it.phone);
      const message = (it.message || "").toString();
      if (!phone || !message) {
        results.push({ phone: it.phone, ok: false, error: "bad item" });
        continue;
      }
      try {
        await client.sendMessage(`${phone}@c.us`, message);
        results.push({ phone, ok: true });
        if (RATE_DELAY_MS > 0) await new Promise((r) => setTimeout(r, RATE_DELAY_MS));
      } catch (e) {
        results.push({ phone, ok: false, error: e?.message || String(e) });
      }
    }
    res.json({ ok: true, results });
  } catch (e) {
    res.status(500).json({ ok: false, error: e?.message || String(e) });
  }
});

// Graceful shutdown
function shutdown(sig) {
  console.log(`\n${sig} received, closing...`);
  try { client.destroy(); } catch {}
  process.exit(0);
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

app.listen(PORT, () => {
  console.log(`Local WA API listening on http://localhost:${PORT}`);
  console.log("Endpoints: /health, /status, /qr, /send, /bulk");
  console.log(`Headless: ${HEADLESS ? "ON" : "OFF"}`);
  if (CHROME_PATH) console.log("Chrome:", CHROME_PATH);
  console.log(`Web cache: ${WEB_CACHE_STRATEGY}`);
  console.log("Chrome profile dir:", USER_DATA_DIR);
});