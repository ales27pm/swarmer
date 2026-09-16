const { test } = require("node:test");
const assert = require("node:assert/strict");
const { prepareDelegate, preparePodfile, prepareEnvironment, prepareProject } = require("./prepare-automation-build.cjs");

test("embedded Debug selects bundled JS without changing the Release branch", () => {
  const release = '#else\n    return Bundle.main.url(forResource: "main", withExtension: "jsbundle")\n#endif';
  const source = '#if DEBUG\n    return RCTBundleURLProvider.sharedSettings().jsBundleURL(forBundleRoot: ".expo/.virtual-metro-entry")\n' + release;
  const actual = prepareDelegate(source);
  assert(actual.includes(release));
  assert(!actual.includes("RCTBundleURLProvider"));
  assert.equal(prepareDelegate(actual), actual);
});
test("native launcher exclusion is idempotent and fails on unknown templates", () => {
  const result = preparePodfile("target 'app' do\n  use_expo_modules!\nend");
  assert(result.includes("'expo-dev-launcher'"));
  assert.equal(preparePodfile(result), result);
  assert.throws(() => preparePodfile("new Expo template"));
  assert.throws(() => prepareDelegate("new Expo template"));
});

test("embedded JS disables Metro devtools while the native build stays Debug", () => {
  const old = '# SWARMER_EMBEDDED_DEBUG\nif [[ "$CONFIGURATION" = *Debug* ]]; then\n  unset SKIP_BUNDLING\n  export FORCE_BUNDLING=1\nfi\n';
  const prepared = prepareEnvironment(old);
  assert(prepared.includes('--dev false'));
  assert(!prepared.includes('export CONFIGURATION='));
  assert.equal(prepareEnvironment(prepared), prepared);
  const { execFileSync } = require('node:child_process');
  const value = execFileSync('/bin/bash', ['-c', prepared + '\nprintf "%s" "$EXTRA_PACKAGER_ARGS"'], {
    env: { ...process.env, CONFIGURATION: 'Debug', EXTRA_PACKAGER_ARGS: '--minify false' }, encoding: 'utf8',
  });
  assert.equal(value, '--minify false --dev false');
  const release = execFileSync('/bin/bash', ['-c', prepared + '\nprintf "%s" "$EXTRA_PACKAGER_ARGS"'], {
    env: { ...process.env, CONFIGURATION: 'Release', EXTRA_PACKAGER_ARGS: '--minify false' }, encoding: 'utf8',
  });
  assert.equal(release, '--minify false');
});

const nativeInvocation = '`"$NODE_BINARY" --print "require(\'path\').dirname(require.resolve(\'react-native/package.json\')) + \'/scripts/react-native-xcode.sh\'"`';
// Same final command/environment order as Expo SDK 55's generated PBX phase.
const generatedPhase = `if [[ "$CONFIGURATION" = *Debug* ]]; then
  export SKIP_BUNDLING=1
fi
if [[ -f "$PODS_ROOT/../.xcode.env.updates" ]]; then
  source "$PODS_ROOT/../.xcode.env.updates"
fi
if [[ -f "$PODS_ROOT/../.xcode.env.local" ]]; then
  source "$PODS_ROOT/../.xcode.env.local"
fi

${nativeInvocation}\n\n`;
function project(script = generatedPhase) {
  return `// project\n\t\t\tshellScript = ${JSON.stringify(script)};\n// other phase\n\t\t\tshellScript = "echo untouched";\n`;
}
function bundleScript(source) {
  return JSON.parse(source.match(/^\s*shellScript = ("(?:[^"\\]|\\.)*");$/m)[1]);
}

test("PBX patch is idempotent, preserves other phases, and rejects unknown or ambiguous templates", () => {
  const prepared = prepareProject(project());
  assert.equal(prepareProject(prepared), prepared);
  assert(prepared.includes('shellScript = "echo untouched";'));
  assert(bundleScript(prepared).endsWith(`${nativeInvocation}\n\n`));
  assert.throws(() => prepareProject(project("echo new Expo template\n")));
  assert.throws(() => prepareProject(project() + project()));
  assert.throws(() => prepareProject(project(generatedPhase.replace(nativeInvocation, "react-native-xcode.sh"))));
  assert.throws(() => prepareProject(project(generatedPhase + "source late-override\n")));
  assert.throws(() => prepareProject(prepared.replace("export FORCE_BUNDLING=1", "export FORCE_BUNDLING=0")));
});

test("Debug bundles production JS after pod install deletes updates, even with conflicting final local env", () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const { execFileSync } = require("node:child_process");
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "swarmer-bundle-phase-"));
  try {
    fs.mkdirSync(path.join(temporary, "Pods"));
    const updates = path.join(temporary, ".xcode.env.updates");
    fs.writeFileSync(updates, prepareEnvironment(""));
    fs.writeFileSync(path.join(temporary, ".xcode.env.local"), "export SKIP_BUNDLING=1\nexport FORCE_BUNDLING=0\nexport EXTRA_PACKAGER_ARGS='--minify false --dev true'\n");
    const prepared = prepareProject(project());
    fs.unlinkSync(updates); // Expo autolinking's pod-install behavior.
    const probe = bundleScript(prepared).replace(nativeInvocation,
      'printf "%s\\n" "$CONFIGURATION" "${SKIP_BUNDLING-unset}" "$FORCE_BUNDLING" "$SKIP_BUNDLING_METRO_IP" "$EXTRA_PACKAGER_ARGS"');
    const run = (configuration) => execFileSync("/bin/bash", ["-c", probe], {
      env: { ...process.env, CONFIGURATION: configuration, PODS_ROOT: path.join(temporary, "Pods"), SKIP_BUNDLING_METRO_IP: "before" },
      encoding: "utf8",
    }).trim().split("\n");
    assert.deepEqual(run("Debug"), ["Debug", "unset", "1", "1", "--minify false --dev true --dev false"]);
    assert.deepEqual(run("Release"), ["Release", "1", "0", "before", "--minify false --dev true"]);
    assert.equal(prepareProject(prepared), prepared);
  } finally { fs.rmSync(temporary, { recursive: true, force: true }); }
});
