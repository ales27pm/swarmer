const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { assertIOSApplicationGraph, bootstrapModules } = require("./ios-bundle-guard.cjs");
const { route_keys: routes } = require("./ios-bundle-contract.json");

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "swarmer-graph-test-")));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "node_modules"));
  const files = [...routes.map((key) => path.join("app", key)), ...bootstrapModules];
  for (const file of files) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), "export default function Route() {}\n");
  }
  return {
    root,
    graph: {
      transformOptions: { platform: "ios", dev: false },
      dependencies: new Map(files.map((file) => [path.join(root, file), {}])),
    },
  };
}

test("complete Release application graph is accepted", (t) => {
  const { root, graph } = fixture(t);
  assert.doesNotThrow(() => assertIOSApplicationGraph(root, graph));
});

test("empty router graph is rejected even though every source route exists", (t) => {
  const { root, graph } = fixture(t);
  graph.dependencies = new Map([[path.join(root, "node_modules/expo-router/entry.js"), {}]]);
  assert.throws(() => assertIOSApplicationGraph(root, graph), /incomplete iOS Release bundle.*app\/.*_layout/s);
});

test("partial routes and missing bootstrap each block Release", (t) => {
  for (const file of ["app/(main)/settings.tsx", ...bootstrapModules]) {
    const { root, graph } = fixture(t);
    graph.dependencies.delete(path.join(root, file));
    assert.throws(() => assertIOSApplicationGraph(root, graph), (error) => error.message.includes(file));
  }
});

test("a route in a different checkout does not satisfy this graph", (t) => {
  const { root, graph } = fixture(t);
  graph.dependencies.delete(path.join(root, "app/_layout.tsx"));
  graph.dependencies.set(path.join(root, "other/app/_layout.tsx"), {});
  assert.throws(() => assertIOSApplicationGraph(root, graph), /app\/_layout.tsx/);
});

test("shared dependency-directory symlink is rejected for Release", (t) => {
  const { root, graph } = fixture(t);
  fs.renameSync(path.join(root, "node_modules"), path.join(root, "shared"));
  fs.symlinkSync(path.join(root, "shared"), path.join(root, "node_modules"));
  assert.throws(() => assertIOSApplicationGraph(root, graph), /own node_modules directory/);
});

test("development, web, Android and server graphs keep their existing behavior", () => {
  for (const transformOptions of [
    { platform: "ios", dev: true },
    { platform: "web", dev: false },
    { platform: "android", dev: false },
    { platform: "ios", dev: false, customTransformOptions: { environment: "node" } },
  ]) {
    assert.doesNotThrow(() => assertIOSApplicationGraph("/not-a-project", { transformOptions }));
  }
});

test("release contract tracks all current application routes", () => {
  const root = path.resolve(__dirname, "../app");
  const actual = fs.readdirSync(root, { recursive: true }).filter((file) => file.endsWith(".tsx"));
  assert.deepEqual([...routes].sort(), actual.map((file) => "./" + file).sort());
});
