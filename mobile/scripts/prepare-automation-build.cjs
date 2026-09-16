#!/usr/bin/env node
/* global __dirname */
/** Run after expo prebuild for the dedicated, embedded-JS Debug device build. */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

function prepareDelegate(source) {
  const original = 'return RCTBundleURLProvider.sharedSettings().jsBundleURL(forBundleRoot: ".expo/.virtual-metro-entry")';
  const replacement = 'return Bundle.main.url(forResource: "main", withExtension: "jsbundle") // SWARMER_EMBEDDED_DEBUG';
  if (source.includes(replacement)) return source;
  assert.equal(source.split(original).length, 2, "Expo AppDelegate changed; inspect before patching.");
  return source.replace(original, replacement);
}

function preparePodfile(source) {
  const original = "  use_expo_modules!\n";
  const replacement = "  use_expo_modules!(:exclude => ['expo-dev-client', 'expo-dev-launcher', 'expo-dev-menu', 'expo-dev-menu-interface']) # SWARMER_EMBEDDED_DEBUG\n";
  if (source.includes(replacement)) return source;
  assert.equal(source.split(original).length, 2, "Expo Podfile changed; inspect before patching.");
  return source.replace(original, replacement);
}

function prepareEnvironment(source) {
  const marker = "# SWARMER_EMBEDDED_DEBUG";
  const block = `${marker}\nif [[ "$CONFIGURATION" = *Debug* ]]; then\n  unset SKIP_BUNDLING\n  export FORCE_BUNDLING=1\n  export SKIP_BUNDLING_METRO_IP=1\n  export EXTRA_PACKAGER_ARGS=\"$EXTRA_PACKAGER_ARGS --dev false\"\nfi\n`;
  if (!source.includes(marker)) return `${source}\n${block}`;
  const old = /# SWARMER_EMBEDDED_DEBUG\nif \[\[ "\$CONFIGURATION" = \*Debug\* \]\]; then\n[\s\S]*?\nfi\n/;
  assert(old.test(source), "Embedded build environment changed; inspect before patching.");
  return source.replace(old, block);
}

function prepareProject(source) {
  const invocation = '`"$NODE_BINARY" --print "require(\'path\').dirname(require.resolve(\'react-native/package.json\')) + \'/scripts/react-native-xcode.sh\'"`';
  const marker = "# SWARMER_EMBEDDED_DEBUG_BUNDLE";
  const block = `${marker}_BEGIN\n${prepareEnvironment("").trimStart()}${marker}_END\n\n`;
  let phases = 0;
  const result = source.replace(/^(\s*shellScript = )("(?:[^"\\]|\\.)*");$/gm, (line, prefix, encoded) => {
    const script = JSON.parse(encoded);
    if (!script.includes("react-native-xcode.sh")) return line;
    phases += 1;
    assert.equal(script.split(invocation).length, 2, "React Native bundle invocation changed; inspect before patching.");
    assert(script.endsWith(`${invocation}\n\n`), "React Native invocation must remain the final bundle phase command.");
    let before = script.slice(0, -`${invocation}\n\n`.length);
    if (before.includes(marker)) {
      assert(before.endsWith(block), "Embedded bundle phase changed; inspect before patching.");
      before = before.slice(0, -block.length);
      assert(!before.includes(marker), "Duplicate embedded bundle phase marker.");
    }
    // Expo/CocoaPods may delete .xcode.env.updates. Enforce this AFTER every
    // generated environment source, immediately before the native bundler.
    return `${prefix}${JSON.stringify(`${before}${block}${invocation}\n\n`)};`;
  });
  assert.equal(phases, 1, "Expected exactly one known React Native bundle phase.");
  return result;
}

if (require.main === module) {
  const [major, minor] = process.versions.node.split(".").map(Number);
  assert((major === 22 && minor >= 13) || major >= 24, "Use Node ^22.13 or >=24, as required by mobile/package.json.");
  const ios = path.resolve(__dirname, "../ios");
  const preparedFiles = [
    [path.join(ios, "monGARSSwarm/AppDelegate.swift"), prepareDelegate],
    [path.join(ios, "Podfile"), preparePodfile],
    [path.join(ios, "monGARSSwarm.xcodeproj/project.pbxproj"), prepareProject],
  ].map(([file, transform]) => [file, transform(fs.readFileSync(file, "utf8"))]);
  // Validate every known generated template before changing any of them.
  for (const [file, contents] of preparedFiles) fs.writeFileSync(file, contents);
  const localPath = path.join(ios, ".xcode.env.local");
  const local = fs.existsSync(localPath) ? fs.readFileSync(localPath, "utf8") : "";
  const nodeExport = `export NODE_BINARY='${process.execPath.replace(/'/g, "'\\''")}'`;
  fs.writeFileSync(localPath, /^export NODE_BINARY=.*$/m.test(local)
    ? local.replace(/^export NODE_BINARY=.*$/m, nodeExport) : `${local}\n${nodeExport}\n`);
  console.log("Prepared generated iOS project for embedded Debug automation. Run pod install, rerun this script to verify the bundle phase, then build Debug. Regenerate with expo prebuild --clean before a normal release.");
}
module.exports = { prepareDelegate, preparePodfile, prepareEnvironment, prepareProject };
