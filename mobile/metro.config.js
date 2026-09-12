const { getDefaultConfig } = require("expo/metro-config");
const { withVibecodeMetro } = require("@vibecodeapp/sdk/metro");

/** @type {import('expo/metro-config').MetroConfig} */
const config = getDefaultConfig(__dirname);
const resolveRequest = config.resolver.resolveRequest;
const previewConfig = withVibecodeMetro(config, { enableCache: false });

// Expo SQLite's web worker loads WebAssembly and requires cross-origin isolation.
// Static web hosts must send the same headers when serving an exported preview.
if (!config.resolver.assetExts.includes("wasm")) {
  config.resolver.assetExts.push("wasm");
}
const enhanceMiddleware = config.server.enhanceMiddleware;
config.server.enhanceMiddleware = (middleware, metroServer) => {
  const upstream = enhanceMiddleware
    ? enhanceMiddleware(middleware, metroServer)
    : middleware;
  return (request, response, next) => {
    response.setHeader("Cross-Origin-Embedder-Policy", "credentialless");
    response.setHeader("Cross-Origin-Opener-Policy", "same-origin");
    return upstream(request, response, next);
  };
};

// SDK 0.4.15 injects its web fetch proxy and layout wrapper on every platform.
// Use its web module polyfills while preserving Expo's transformer, serializer,
// cache, and native module resolution (including Metro's async-require runtime).
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (platform === "web") {
    return previewConfig.resolver.resolveRequest(context, moduleName, platform);
  }
  return resolveRequest
    ? resolveRequest(context, moduleName, platform)
    : context.resolveRequest(context, moduleName, platform);
};

module.exports = config;
