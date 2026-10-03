const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '../static/app.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

async function uiHarness(initial) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', textContent: '', hidden: false, disabled: false,
      addEventListener() {}, replaceChildren() {}, removeAttribute() {},
    });
    return nodes.get(id);
  };
  const harness = {reply: initial, nodes, timer: null, fail: false};
  const context = vm.createContext({
    document: {getElementById: node, addEventListener() {}},
    window: {addEventListener() {}}, localStorage: {getItem() {}, setItem() {}},
    AbortSignal,
    fetch: async (path, options) => {
      assert.equal(path, '/api/status');
      assert.equal(options.cache, 'no-store');
      if (harness.fail) throw new Error('offline');
      return {ok: true, json: async () => harness.reply};
    },
    clearTimeout() {}, setTimeout(callback, delay) {harness.timer = {callback, delay};},
  });
  vm.runInContext(source, context);
  await settle();
  harness.refresh = async (reply) => {
    harness.reply = reply;
    await harness.timer.callback();
  };
  return harness;
}

function status(code, ready = false) {
  return {ready, availability_code: code, message: `Raison : ${code}`, active_job: null, jobs: []};
}

const labels = {
  image_slot_reserved: 'GPU réservé', ollama_gpu_reserved: 'GPU réservé par Ollama',
  swarmer_busy: 'Swarmer en cours', runtime_unavailable: 'Moteur indisponible',
  model_unavailable: 'Modèle absent', model_invalid: 'Modèle à vérifier',
  cleanup_required: 'Intervention requise', gpu_lock_unavailable: 'Verrou indisponible',
  ollama_unavailable: 'État GPU inconnu', probe_unavailable: 'Vérification indisponible',
  future_unknown_reason: 'Indisponible',
};
for (const [code, label] of Object.entries(labels)) {
  test(`renders ${code} accurately, disables generation and polls in two seconds`, async () => {
    const ui = await uiHarness(status(code));
    assert.equal(ui.nodes.get('connection').textContent, label);
    assert.equal(ui.nodes.get('availability').textContent, `Raison : ${code}`);
    assert.equal(ui.nodes.get('availability').hidden, false);
    assert.equal(ui.nodes.get('generate').disabled, true);
    assert.equal(ui.timer.delay, 2000);
  });
}

test('a new ready response clears a reservation and re-enables generation', async () => {
  const ui = await uiHarness(status('ollama_gpu_reserved'));
  await ui.refresh(status('ready', true));
  assert.equal(ui.nodes.get('connection').textContent, 'Prêt');
  assert.equal(ui.nodes.get('availability').hidden, true);
  assert.equal(ui.nodes.get('generate').disabled, false);
  assert.equal(ui.timer.delay, 6000);
  await ui.refresh(status('ollama_unavailable'));
  assert.equal(ui.nodes.get('connection').textContent, 'État GPU inconnu');
  assert.equal(ui.nodes.get('generate').disabled, true);
  assert.equal(ui.timer.delay, 2000);
});

test('lost connectivity is distinct from a stale GPU reservation', async () => {
  const ui = await uiHarness(status('ollama_gpu_reserved'));
  ui.fail = true;
  await ui.refresh(status('ready', true));
  assert.equal(ui.nodes.get('connection').textContent, 'Hors connexion');
  assert.equal(ui.nodes.get('availability').hidden, true);
  assert.equal(ui.nodes.get('generate').disabled, true);
  assert.equal(ui.timer.delay, 2000);
});
