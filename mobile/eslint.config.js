const { defineConfig, globalIgnores } = require("eslint/config");
const expoConfig = require("eslint-config-expo/flat");

module.exports = defineConfig([
  globalIgnores(["dist/*", "coverage/*"]),
  expoConfig,
  { files: ["scripts/*.cjs"], languageOptions: { globals: { __dirname: "readonly" } } },
]);
