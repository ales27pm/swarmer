import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import urllib.request

BASE = Path('/home/ales27pm/swarmer-media-qualification/20261002/chroma-inference')
DB = Path('/home/ales27pm/.local/state/swarmer-control-plane/mongars.db')
RUNTIME = Path('/home/ales27pm/.local/share/swarmer/chroma-runtime/3f8527a-sm75/bin')
IMAGE = 'sha256:c55ec3428181057adc0a92a8cf844cb7d3aa029dc2c154bc8baca5fc6c7ede5f'


def command(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def admission():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        db.execute('PRAGMA query_only=ON')
        calls = db.execute("SELECT count(*) FROM goal_model_calls WHERE provider_source='ubuntu_local' AND status='started' AND lease_expires_at>?", (now,)).fetchone()[0]
        jobs = db.execute("SELECT count(*) FROM agent_jobs WHERE required_skill IN ('writing.draft','code.generate_python','code.build_project','image.generate') AND status IN ('claimed','running') AND lease_expires_at>?", (now,)).fetchone()[0]
    with urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=3) as response:
        models = json.load(response)['models']
    return {'active_model_calls': calls, 'active_gpu_jobs': jobs, 'resident_ollama_models': len(models)}


def inspect(name):
    result = command(['docker', 'inspect', '--format', '{{json .State}}', name])
    if result.returncode:
        raise RuntimeError('Cannot inspect qualification container')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True)
    parser.add_argument('--steps', type=int, default=40)
    parser.add_argument('--cancel-after-seconds', type=float)
    parser.add_argument('--direct-weights', action='store_true')
    args = parser.parse_args()
    if not args.run.replace('-', '').isalnum() or not 1 <= args.steps <= 40:
        raise SystemExit('Invalid bounded run arguments')
    work = BASE / args.run
    work.mkdir(mode=0o700)
    before = admission()
    if any(before.values()):
        raise SystemExit('Production or Ollama work active; no generation started')
    models = json.loads((BASE / 'verified-models.json').read_text())
    paths = {}
    for item in models:
        path = Path(item['local_path'])
        if path.stat().st_size != item['verified_bytes']:
            raise SystemExit('Verified model size changed')
        paths[item['repo_id']] = '/models/' + str(path.relative_to(BASE / 'models'))
    prompt = 'A studio product photograph of one small matte red ceramic teapot, centered on a pale blue table, soft window lighting, a plain cream background, realistic ceramic texture, clean composition, no text, no watermark.'
    output = work / 'teapot.png'
    cli = ['/runtime/sd-cli', '--diffusion-model', paths['silveroxides/Chroma1-HD-GGUF'], '--t5xxl', paths['city96/t5-v1_1-xxl-encoder-gguf'], '--vae', paths['lodestones/Chroma'], '--backend', 'diffusion=cuda0,te=cpu,vae=cpu', '--max-vram', 'cuda0=6.5', '--diffusion-fa', '--steps', str(args.steps), '--cfg-scale', '3', '--sampling-method', 'euler', '--scheduler', 'flux', '--extra-sample-args', f'base_shift={math.log(3)},max_shift={math.log(3)}', '-W', '512', '-H', '512', '-b', '1', '-s', '42', '-t', '4', '-p', prompt, '-n', 'blurry, low quality', '-o', '/output/teapot.png', '-v']
    if args.direct_weights:
        cli.extend(['--params-backend', 'diffusion=cuda0,te=cpu,vae=cpu'])
    name = 'swarmer-chroma-' + args.run
    launch = ['docker', 'run', '-d', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}', '--gpus', 'all', '--memory', '12g', '--memory-swap', '12g', '--cpus', '6', '--pids-limit', '256', '--init', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '--mount', f'type=bind,src={RUNTIME},dst=/runtime,readonly', '--mount', f'type=bind,src={BASE}/models,dst=/models,readonly', '--mount', f'type=bind,src={work},dst=/output', '--env', 'HOME=/tmp', '--env', 'CUDA_CACHE_PATH=/tmp/cuda-cache', '--entrypoint', '/usr/bin/timeout', IMAGE, '--signal=TERM', '--kill-after=10s', '900', *cli]
    receipt = {'run': args.run, 'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'command': launch, 'prompt': prompt, 'steps': args.steps, 'seed': 42, 'admission_before': before, 'models': models, 'source_commit': '3f8527a46c54ecf4cb4ed6003da8e8982283c73c', 'runtime_sha256': '3e3f105d52840e06dd133c862abd5c6895c2d974abf92e9487b963b5decfa6cf', 'inference_via_app_api': False, 'direct_diffusion_weights': args.direct_weights, 'cancel_after_seconds': args.cancel_after_seconds}
    (work / 'request.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('START', args.run, 'steps', args.steps, flush=True)
    started = time.monotonic()
    proc = command(launch, 30)
    if proc.returncode:
        (work / 'launch-error.txt').write_text(proc.stderr)
        raise SystemExit('Docker launch failed; inspect launch-error.txt')
    stop_reason = None
    samples = []
    last_report = started
    cgroup = None
    try:
        while True:
            state = inspect(name)
            if not state['Running']:
                break
            now = time.monotonic()
            if cgroup is None:
                lines = (Path('/proc') / str(state['Pid']) / 'cgroup').read_text().splitlines()
                relative = next(line.split('::', 1)[1] for line in lines if line.startswith('0::'))
                cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
            sample = {'elapsed_seconds': round(now - started, 3)}
            for metric in ['memory.current', 'memory.peak', 'memory.events', 'cpu.stat']:
                try:
                    value = (cgroup / metric).read_text().strip()
                    sample[metric] = int(value) if value.isdecimal() else value
                except OSError:
                    pass
            gpu = command(['nvidia-smi', '--query-gpu=memory.used,utilization.gpu,temperature.gpu,power.draw', '--format=csv,noheader,nounits'], 10)
            sample['gpu'] = gpu.stdout.strip() if not gpu.returncode else None
            mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
            sample['host_mem_available_kib'] = int(mem['MemAvailable'].split()[0])
            sample['host_swap_used_kib'] = int(mem['SwapTotal'].split()[0]) - int(mem['SwapFree'].split()[0])
            samples.append(sample)
            current = admission()
            if any(current.values()):
                stop_reason = 'yield_to_production_work'
            elif args.cancel_after_seconds is not None and now - started >= args.cancel_after_seconds:
                stop_reason = 'intentional_cancellation'
            elif now - started > 910:
                stop_reason = 'supervisor_deadline'
            if stop_reason:
                before_stop = time.monotonic()
                command(['docker', 'stop', '--timeout', '5', name], 15)
                receipt['stop_latency_seconds'] = round(time.monotonic() - before_stop, 3)
                break
            if now - last_report >= 25:
                tail = command(['docker', 'logs', '--tail', '3', name])
                print('PROGRESS', json.dumps(sample), (tail.stdout + tail.stderr)[-900:].replace('\r', '\n'), flush=True)
                last_report = now
            time.sleep(2)
    except BaseException as exc:
        stop_reason = 'supervisor_error:' + type(exc).__name__
        command(['docker', 'stop', '--timeout', '5', name], 15)
        receipt['supervisor_error'] = str(exc)
    finally:
        state = inspect(name)
        logs = command(['docker', 'logs', name], 20)
        (work / 'runtime.log').write_text(logs.stdout + logs.stderr)
        receipt.update(elapsed_seconds=round(time.monotonic() - started, 3), exit_code=state['ExitCode'], oom_killed=state['OOMKilled'], stop_reason=stop_reason, output_exists=output.is_file(), samples=samples)
        receipt['peak_container_memory_bytes'] = max((s.get('memory.peak', 0) for s in samples), default=0)
        if output.is_file():
            receipt['output_bytes'] = output.stat().st_size
            with output.open('rb') as source:
                receipt['output_sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        command(['docker', 'rm', name], 15)
    print('FINISHED', json.dumps({key: receipt.get(key) for key in ['run', 'elapsed_seconds', 'exit_code', 'oom_killed', 'stop_reason', 'output_exists', 'output_bytes', 'peak_container_memory_bytes']}), flush=True)
    if receipt['exit_code'] != 0 or not receipt['output_exists']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
