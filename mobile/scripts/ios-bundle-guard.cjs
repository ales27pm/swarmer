const fs = require("node:fs");
const path = require("node:path");
const { route_keys: routeKeys } = require("./ios-bundle-contract.json");

const bootstrapModules = [
  "src/lib/application-api/network-bridge.tsx",
  "src/lib/sync/live-sync-provider.tsx",
];

// Check the graph Metro actually assembled, not just files present on disk.
// A Release bundle containing only Expo Router crashes with "No routes found".
function assertIOSApplicationGraph(projectRoot, graph) {
  const options = graph.transformOptions;
  if (options?.platform !== "ios" || options.dev !== false ||
      (options.customTransformOptions?.environment ?? "client") !== "client") {
    return;
  }

  const modulesRoot = path.join(projectRoot, "node_modules");
  if (fs.lstatSync(modulesRoot).isSymbolicLink()) {
    throw new Error(
      "iOS Release requires its own node_modules directory. Copy dependencies " +
      "into the release snapshot; do not share Expo Router through a directory symlink."
    );
  }

  const modulePaths = new Set(graph.dependencies.keys());
  const required = [
    ...routeKeys.map((key) => path.join("app", key)),
    ...bootstrapModules,
  ];
  const missing = required.filter((relative) => {
    const absolute = path.resolve(projectRoot, relative);
    return !fs.existsSync(absolute) || !modulePaths.has(fs.realpathSync(absolute));
  });
  if (missing.length) {
    throw new Error(
      "Refusing incomplete iOS Release bundle: application modules missing from " +
      "the Metro graph: " + missing.join(", ")
    );
  }
}

module.exports = { assertIOSApplicationGraph, bootstrapModules };
