/* global __dirname */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function invoke(args = [], extraEnv = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'swarmer-build-wrapper-'));
  const capture = path.join(directory, 'args');
  try {
    fs.writeFileSync(path.join(directory, 'xcodebuild'), '#!/bin/sh\nprintf "%s\\0" "$@" > "$BUILD_CAPTURE"\nexit "${BUILD_EXIT:-0}"\n', { mode: 0o700 });
    const result = spawnSync('/bin/bash', [path.join(__dirname, 'build-automation-iphone.sh'), ...args], {
      cwd: directory,
      env: { ...process.env, XCODE_XCCONFIG_FILE: '', PATH: `${directory}:${process.env.PATH}`, BUILD_CAPTURE: capture, ...extraEnv },
      encoding: 'utf8',
    });
    return { ...result, args: fs.existsSync(capture) ? fs.readFileSync(capture, 'utf8').split('\0').slice(0, -1) : null };
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

test('C/C++ optimized Debug settings preserve Swift debug mode and path boundaries', () => {
  const args = ['-derivedDataPath', '/tmp/build with spaces', '-clonedSourcePackagesDirPath', '/tmp/package cache', 'DEVELOPMENT_TEAM=TEAM', '-allowProvisioningUpdates'];
  const result = invoke(args);
  assert.equal(result.status, 0);
  assert.deepEqual(result.args, [
    '-workspace', path.resolve(__dirname, '../ios/monGARSSwarm.xcworkspace'),
    '-scheme', 'monGARSSwarm', '-destination', 'generic/platform=iOS', ...args,
    '-configuration', 'Debug', 'GCC_OPTIMIZATION_LEVEL=3', 'SWIFT_OPTIMIZATION_LEVEL=-Onone', 'build',
  ]);
});

test('explicit workspace, scheme and physical destination replace defaults once', () => {
  const result = invoke(['-workspace', '/tmp/custom.xcworkspace', '-scheme', 'Custom', '-destination', 'id=PHONE']);
  assert.equal(result.status, 0);
  assert.deepEqual(result.args.slice(0, 6), ['-workspace', '/tmp/custom.xcworkspace', '-scheme', 'Custom', '-destination', 'id=PHONE']);
  assert.equal(result.args.filter((value) => value === '-workspace').length, 1);
});

test('contradictory configuration and optimization fail before invoking Xcode', () => {
  for (const args of [
    ['-configuration', 'Release'], ['CONFIGURATION=Release'], ['-configuration=Release'],
    ['GCC_OPTIMIZATION_LEVEL=0'], ['SWIFT_OPTIMIZATION_LEVEL=-O'], ['GCC_OPTIMIZATION_LEVEL[arch=arm64]=0'], ['SWIFT_OPTIMIZATION_LEVEL[arch=arm64]=-O'], ['-configuration'], ['-scheme'],
  ]) {
    const result = invoke(args);
    assert.equal(result.status, 2, JSON.stringify(args));
    assert.equal(result.args, null);
  }
});

test('matching explicit fixed settings are normalized without duplicates', () => {
  const result = invoke(['-configuration', 'Debug', 'CONFIGURATION=Debug', 'GCC_OPTIMIZATION_LEVEL=3', 'SWIFT_OPTIMIZATION_LEVEL=-Onone']);
  assert.equal(result.status, 0);
  assert.equal(result.args.filter((value) => value === '-configuration').length, 1);
  assert.equal(result.args.filter((value) => value === 'GCC_OPTIMIZATION_LEVEL=3').length, 1);
});

test('Xcode failure is returned unchanged', () => {
  assert.equal(invoke([], { BUILD_EXIT: '71' }).status, 71);
});

test('environment configuration cannot override enforced settings', () => {
  const result = invoke([], { XCODE_XCCONFIG_FILE: '/tmp/other.xcconfig' });
  assert.equal(result.status, 2);
  assert.equal(result.args, null);
});


test('optional build actions are normalized to one final build', () => {
  const result = invoke(['build', '-allowProvisioningUpdates', 'build']);
  assert.equal(result.status, 0);
  assert.equal(result.args.filter((value) => value === 'build').length, 1);
  assert.equal(result.args.at(-1), 'build');
});

test('non-build Xcode actions are refused before invoking Xcode', () => {
  for (const action of [
    'clean', 'test', 'test-without-building', 'build-for-testing', 'analyze',
    'archive', 'install', 'installhdrs', 'installsrc', 'docbuild',
    '-exportArchive', '-exportNotarizedApp', '-create-xcframework', '-runFirstLaunch',
    '-list', '-showBuildSettings', '-version', '-exportLocalizations',
  ]) {
    const result = invoke([action]);
    assert.equal(result.status, 2, action);
    assert.equal(result.args, null, action);
  }
});

test('compile-condition, extra compiler flag and xcconfig overrides are refused', () => {
  for (const args of [
    ['SWIFT_ACTIVE_COMPILATION_CONDITIONS='], ['GCC_PREPROCESSOR_DEFINITIONS='],
    ['SWIFT_ACTIVE_COMPILATION_CONDITIONS[arch=arm64]='],
    ['GCC_PREPROCESSOR_DEFINITIONS[arch=arm64]='],
    ['OTHER_CFLAGS=-O0'], ['OTHER_CPLUSPLUSFLAGS=-O0'], ['OTHER_SWIFT_FLAGS=-O'],
    ['OTHER_CFLAGS[arch=arm64]=-UDEBUG'], ['OTHER_SWIFT_FLAGS[arch=arm64]=-O'],
    ['-xcconfig', '/tmp/other.xcconfig'], ['-xcconfig=/tmp/other.xcconfig'],
  ]) {
    const result = invoke(args);
    assert.equal(result.status, 2, JSON.stringify(args));
    assert.equal(result.args, null);
  }
});


test('cache paths and unrelated build-directory settings retain their values', () => {
  const args = ['-derivedDataPath', 'build', '-resultBundlePath', 'test', 'CONFIGURATION_BUILD_DIR=/tmp/products'];
  const result = invoke(args);
  assert.equal(result.status, 0);
  assert.deepEqual(result.args.slice(6, 11), args);
});
