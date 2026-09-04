# 18 — Development Setup

## Repo structure cible

```text
mongars-swarm/
  mobile/                Expo app
  server/                Ubuntu FastAPI control plane
  workers/               Agent workers
  packages/              Shared schemas/types
  docs/                  Project docs
  configs/               YAML config
  prompts/               Prompt files
  scripts/               Local dev scripts
  models/                Manifest only, not model binaries
  data/                  Local ignored runtime data
```

## Mobile bootstrap

```bash
npx create-expo-app mobile --template
cd mobile
npm install
npx expo start
```

Packages probables:

```bash
npx expo install expo-router expo-sqlite expo-secure-store expo-location expo-contacts expo-calendar expo-image-picker expo-media-library expo-camera expo-notifications expo-audio
npm install @tanstack/react-query zod
npm install -D typescript eslint jest jest-expo @testing-library/react-native
```

Note: utiliser `npx expo install` pour les modules Expo afin d'obtenir les versions compatibles.

## Backend bootstrap

```bash
mkdir -p server
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn pydantic pydantic-settings sqlalchemy aiosqlite redis jsonschema
pip install pytest ruff mypy bandit pip-audit
```

## Redis MVP

```bash
sudo apt update
sudo apt install redis-server
sudo systemctl enable --now redis-server
redis-cli ping
```

## llama.cpp server

Installer llama.cpp selon la machine. Exemple générique:

```bash
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
cmake -B build
cmake --build build -j --target llama-server llama-cli
```

Lancer modèle:

```bash
./build/bin/llama-server   -hf mradermacher/Hermes-3-Llama-3.2-3B-abliterated-GGUF:Q4_K_M   --host 127.0.0.1   --port 8711   -c 8192
```

## Environment

```bash
# server/.env
MONGARS_ENV=dev
MONGARS_API_HOST=0.0.0.0
MONGARS_API_PORT=8710
MONGARS_LLM_BASE_URL=http://127.0.0.1:8711/v1
MONGARS_DB_URL=sqlite+aiosqlite:///./data/mongars.db
MONGARS_REDIS_URL=redis://127.0.0.1:6379/0
MONGARS_ARTIFACT_DIR=./data/artifacts
```

## Running

Terminal 1:

```bash
redis-server
```

Terminal 2:

```bash
llama-server ...
```

Terminal 3:

```bash
cd server
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8710
```

Terminal 4:

```bash
cd mobile
npx expo start
```

## Development build

Créer seulement quand requis par modules natifs custom:

```bash
npx expo install expo-dev-client
eas build -p ios --profile development
npx expo start --dev-client
```

## Local network

Options:

- même Wi-Fi;
- Tailscale recommandé;
- hostname local `ubuntu.local`;
- certificat dev plus tard.
