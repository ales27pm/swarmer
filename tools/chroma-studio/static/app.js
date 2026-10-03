'use strict';

const $ = (id) => document.getElementById(id);
const ui = {
  form: $('generate-form'), prompt: $('prompt'), negative: $('negative'), steps: $('steps'), seed: $('seed'),
  generate: $('generate'), cancel: $('cancel'), error: $('form-error'), connection: $('connection'),
  dot: $('connection-dot'), caption: $('output-caption'), image: $('result-image'), waiting: $('waiting'),
  phase: $('phase'), elapsed: $('elapsed'), progress: $('progress'), progressDetail: $('progress-detail'),
  download: $('download'), detail: $('result-detail'), history: $('history-list'), empty: $('history-empty'),
};
const phaseNames = {queued: 'En attente…', loading: 'Chargement du modèle…', encoding: 'Lecture du prompt…',
  sampling: 'Création de l’image…', decoding: 'Finalisation de l’image…', cancelling: 'Annulation…',
  completed: 'Image terminée', failed: 'La génération a échoué', cancelled: 'Génération annulée'};
const availabilityNames = {
  image_slot_reserved: 'GPU réservé', ollama_gpu_reserved: 'GPU réservé par Ollama',
  swarmer_busy: 'Swarmer en cours', runtime_unavailable: 'Moteur indisponible',
  model_unavailable: 'Modèle absent', model_invalid: 'Modèle à vérifier',
  cleanup_required: 'Intervention requise', gpu_lock_unavailable: 'Verrou indisponible',
  ollama_unavailable: 'État GPU inconnu', probe_unavailable: 'Vérification indisponible',
};
let state = null;
let selectedId = null;
let sending = false;
let polling = false;
let cancelling = false;
let connected = false;
let historyKey = '';
let timer;
const formatTime = (seconds) => {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

function showError(message) {
  ui.error.textContent = message || '';
  ui.error.hidden = !message;
}

async function request(path, body) {
  const options = {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': state?.csrf_token || ''};
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Connexion interrompue. Le suivi reprendra automatiquement.'); }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('Le serveur a renvoyé une réponse illisible.'); }
  if (!response.ok) {
    const detail = data.detail || data.error || data.message;
    throw new Error(typeof detail === 'string' ? detail : `La demande a été refusée (${response.status}).`);
  }
  return data;
}

function imageURL(job) {
  // Never render an external URL from a result or a prompt.
  return job?.status === 'completed' && typeof job.image_url === 'string'
    && /^\/api\/jobs\/[a-zA-Z0-9-]+\/image(?:\?.*)?$/.test(job.image_url) ? job.image_url : null;
}

function renderHistory(jobs, active) {
  const completed = jobs.filter((job) => imageURL(job));
  const key = JSON.stringify([selectedId, Boolean(active), completed.map((job) => [job.id, job.image_url])]);
  if (key === historyKey) return;
  historyKey = key;
  ui.history.replaceChildren();
  ui.empty.hidden = completed.length > 0;
  for (const job of completed) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'history-item';
    button.classList.toggle('selected', job.id === selectedId);
    button.disabled = Boolean(active);
    button.setAttribute('aria-label', `Revoir : ${job.prompt}`);
    button.setAttribute('aria-pressed', String(job.id === selectedId));
    const image = document.createElement('img');
    image.src = imageURL(job); image.alt = job.prompt; image.loading = 'lazy';
    const label = document.createElement('span'); label.textContent = job.prompt;
    button.append(image, label);
    button.addEventListener('click', () => {
      selectedId = job.id;
      ui.prompt.value = job.prompt; ui.negative.value = job.negative_prompt || '';
      ui.steps.value = job.steps; ui.seed.value = job.seed;
      saveDraft(); render();
    });
    ui.history.append(button);
  }
}

function render() {
  const jobs = Array.isArray(state?.jobs) ? state.jobs : [];
  const active = state?.active_job;
  if (active) selectedId = active.id;
  if (!selectedId && jobs.length) selectedId = jobs[0].id;
  const job = active || jobs.find((item) => item.id === selectedId);
  const busy = Boolean(active);
  ui.generate.disabled = sending || busy || !connected || !state?.ready;
  ui.generate.textContent = sending ? 'Envoi…' : 'Générer l’image';
  ui.cancel.hidden = !busy;
  ui.cancel.disabled = cancelling || !connected;
  ui.cancel.textContent = cancelling ? 'Annulation…' : 'Annuler la génération';
  ui.connection.textContent = !connected ? 'Hors connexion' : busy ? 'Génération en cours'
    : state?.ready ? 'Prêt' : availabilityNames[state?.availability_code] || 'Indisponible';
  ui.dot.className = `dot ${!connected ? 'offline' : busy || !state?.ready ? 'busy' : 'ready'}`;
  $('availability').hidden = !connected || busy || Boolean(state?.ready);
  $('availability').textContent = state?.message || '';
  if (job) {
    const url = imageURL(job);
    ui.image.hidden = !url;
    ui.waiting.hidden = Boolean(url);
    ui.download.hidden = !url;
    ui.caption.textContent = job.status === 'completed' ? `${job.width} × ${job.height}` : phaseNames[job.status] || 'En cours';
    ui.phase.textContent = phaseNames[job.phase] || phaseNames[job.status] || 'Génération en cours…';
    ui.elapsed.textContent = formatTime(job.elapsed_seconds);
    const progress = Number(job.progress);
    const measured = job.progress !== null && job.progress !== undefined && Number.isFinite(progress);
    if (measured) ui.progress.value = Math.max(0, Math.min(1, progress));
    else ui.progress.removeAttribute('value');
    ui.progress.hidden = !busy;
    ui.progressDetail.textContent = measured && job.phase === 'sampling'
      ? `${Math.round(progress * job.steps)} / ${job.steps} étapes`
      : busy ? 'Vous pouvez laisser cet onglet ouvert.' : job.error || phaseNames[job.status] || '';
    if (url) {
      if (ui.image.getAttribute('src') !== url) ui.image.src = url;
      ui.image.alt = job.prompt;
      ui.download.href = `${url}${url.includes('?') ? '&' : '?'}download=1`;
      ui.download.download = `chroma-${job.id}.png`;
      ui.detail.textContent = `${formatTime(job.elapsed_seconds)} · ${job.steps} étapes · Graine ${job.seed}`;
    } else {
      ui.detail.textContent = job.error || (job.status === 'cancelled' ? 'Le calcul a été arrêté. Vous pouvez essayer un autre prompt.' : '');
    }
  } else {
    ui.detail.textContent = connected && !state?.ready ? state.message || 'Le service est indisponible.' : '';
  }
  renderHistory(jobs, active);
}

function saveDraft() {
  $('prompt-count').textContent = `${ui.prompt.value.length.toLocaleString('fr-CA')} / 2 000`;
  try { localStorage.setItem('chroma-studio-draft', JSON.stringify({prompt: ui.prompt.value, negative: ui.negative.value, steps: ui.steps.value, seed: ui.seed.value})); } catch { /* Private browsing can disable storage. */ }
}

async function refresh() {
  if (polling) return;
  polling = true;
  try {
    state = await request('/api/status');
    connected = true;
  } catch (error) {
    connected = false;
    ui.detail.textContent = error.message;
  } finally {
    polling = false; render();
    clearTimeout(timer);
    timer = setTimeout(refresh, state?.active_job || !connected || !state?.ready ? 2000 : 6000);
  }
}

ui.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (sending || state?.active_job || !connected || !state?.ready) return;
  const prompt = ui.prompt.value.trim();
  if (!prompt) { showError('Décris l’image que tu souhaites créer.'); ui.prompt.focus(); return; }
  if (!ui.form.reportValidity()) return;
  const body = {prompt, negative_prompt: ui.negative.value.trim(), steps: Number(ui.steps.value),
    seed: ui.seed.value.trim() === '' ? null : Number(ui.seed.value), width: 512, height: 512};
  sending = true; showError(''); saveDraft(); render();
  try {
    const job = await request('/api/jobs', body);
    selectedId = job.id;
    state.active_job = job;
    state.jobs = [job, ...(state.jobs || []).filter((item) => item.id !== job.id)];
    render();
    if (matchMedia('(max-width:680px)').matches) document.querySelector('.output').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion:reduce)').matches ? 'auto' : 'smooth', block: 'start'});
  } catch (error) {
    showError(error.message);
  } finally {
    sending = false;
    // Read status after an uncertain POST instead of submitting a second job.
    await refresh(); render();
  }
});

ui.cancel.addEventListener('click', async () => {
  const id = state?.active_job?.id;
  if (!id || cancelling) return;
  cancelling = true; showError(''); render();
  try {
    const job = await request(`/api/jobs/${encodeURIComponent(id)}/cancel`, {});
    state.active_job = ['queued', 'running'].includes(job.status) ? job : null;
    state.jobs = [job, ...(state.jobs || []).filter((item) => item.id !== id)];
  } catch (error) { showError(error.message); }
  finally { cancelling = false; await refresh(); render(); }
});

for (const field of [ui.prompt, ui.negative, ui.steps, ui.seed]) field.addEventListener('input', saveDraft);
ui.prompt.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); ui.form.requestSubmit(); }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
window.addEventListener('online', refresh);
ui.image.addEventListener('error', () => { ui.detail.textContent = 'L’image ne peut pas être chargée. Réessayez lorsque la connexion est rétablie.'; });
try {
  const draft = JSON.parse(localStorage.getItem('chroma-studio-draft') || 'null');
  if (draft && typeof draft.prompt === 'string') {
    ui.prompt.value = draft.prompt.slice(0, 2000);
    ui.negative.value = typeof draft.negative === 'string' ? draft.negative.slice(0, 1000) : '';
    ui.steps.value = Number(draft.steps) >= 1 && Number(draft.steps) <= 40 ? draft.steps : '40';
    ui.seed.value = draft.seed === '' || (Number(draft.seed) >= 0 && Number(draft.seed) <= 2147483647) ? draft.seed : '42';
  }
} catch { /* A malformed local draft should not prevent generation. */ }
saveDraft();
refresh();
