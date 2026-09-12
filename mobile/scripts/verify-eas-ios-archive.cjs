// EAS invokes this after archive export. Any failure prevents a successful build.
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

if (process.env.EAS_BUILD_PLATFORM === "android") {
  console.log("iOS archive verification does not apply to Android.");
  process.exit(0);
}

const mobileRoot = path.resolve(__dirname, "..");
const archiveDirectory = path.join(mobileRoot, "ios", "build");
if (!fs.existsSync(archiveDirectory)) {
  console.error("iOS archive verification failed: ios/build is missing.");
  process.exit(1);
}
const archives = fs.readdirSync(archiveDirectory, { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".ipa"))
  .map((entry) => path.join(archiveDirectory, entry.name));
if (archives.length !== 1) {
  console.error(`iOS archive verification requires exactly one IPA; found ${archives.length}.`);
  process.exit(1);
}
const result = spawnSync("python3", [path.join(__dirname, "verify-ios-archive.py"), archives[0]], {
  stdio: "inherit",
});
if (result.error) console.error("Could not run the iOS archive verifier:", result.error.message);
process.exit(result.status ?? 1);
