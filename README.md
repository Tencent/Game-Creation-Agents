<div align="center">

<img src="figures/gca-banner.jpeg" alt="Game-Creation-Agents — from idea to verified Phaser game" width="98%">

### 🎮 Specialized agents turn your game idea into a playable, verified experience.

[![arXiv](https://img.shields.io/badge/arXiv-2610.16776-B31B1B?style=flat-square&logo=arxiv&logoColor=white)](https://arxiv.org/abs/2610.16776)
![Status](https://img.shields.io/badge/status-public_preview-2563EB?style=flat-square)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](gca-mcp/pyproject.toml)
[![Node.js](https://img.shields.io/badge/node.js-20%2B-339933?style=flat-square&logo=nodedotjs&logoColor=white)](scripts/preflight-requirements.toml)
[![Phaser](https://img.shields.io/badge/Phaser-4-8A2BE2?style=flat-square)](https://phaser.io/)
[![License](https://img.shields.io/badge/license-Apache--2.0-22C55E?style=flat-square)](LICENSE)

</div>

# 💡 Introduction

Game-Creation-Agents (GCA) is an end-to-end pipeline that turns a
natural-language idea into a playable, verified browser game. 
The creation flow is:

<div align="center">
  <img src="figures/Loop.jpeg" alt="GCA pipeline from input and design through parallel build, verification, and release" width="100%">
</div>

---

# 📰 News

- **[2026-10-14]** We are pleased to announce the initial public release of
  **Game-Creation-Agents (GCA)**, an end-to-end agentic pipeline for designing,
  building, and verifying playable browser games.

---

# 🚀 Quick Start

## 🧰 1. Environment

Requirements:

- macOS or Linux; Windows users should use WSL2;
- Python 3.11 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- Node.js 20 or newer and npm;
- Playwright with Chromium;
- `curl`, `ffmpeg`, and `ffprobe`;
- Claude Code with project MCP support;
- an OpenAI API key for image generation and editing;
- a Volcengine ARK API key for Seedance video generation and Doubao VLM;
- no background-removal model: the default `assets.matting.backend: color_key`
  mattes by pure computation against the generated staging background. An
  optional [RMBG-2.0 matting service](services/README.md) can be opted into for
  soft-alpha edges — see [Background removal](#background-removal).

The current preflight checks provider credentials, the configured RMBG/color-key
matting routes, browser, media, and filesystem dependencies required by the full pipeline.
Seedance sends local input frames inline, so object storage is not required.
Host binary requirements and the minimum Node.js version are declared in
[`scripts/preflight-requirements.toml`](scripts/preflight-requirements.toml).

**Install system prerequisites**

On macOS, install [Homebrew](https://brew.sh/) first, then:

```bash
brew install git uv node ffmpeg python@3.11
npm install -g @anthropic-ai/claude-code playwright
npx playwright install chromium
```

On Ubuntu / Debian:

```bash
sudo apt-get update
sudo apt-get install -y git curl ffmpeg
```

Then install [`uv`](https://docs.astral.sh/uv/getting-started/installation/),
[Node.js 20+](https://nodejs.org/en/download), and
[Python 3.11+](https://www.python.org/downloads/) from their official or
distribution-supported sources. Install the Node tools:

```bash
npm install -g @anthropic-ai/claude-code playwright
npx playwright install --with-deps chromium
```

Confirm the active executables:

```bash
python3 --version  # must be 3.11+
node --version     # must be 20+
uv --version
ffmpeg -version
ffprobe -version
```

The verification runner loads Playwright from the global npm root, so Chromium
must be installed globally as shown above. Pinning Playwright as a versioned
project dependency is tracked as a future improvement.

**Clone and install GCA**

```bash
git clone https://github.com/Tencent/Game-Creation-Agents.git
cd Game-Creation-Agents

cd gca-mcp
uv sync --extra dev
cd ..
```

## 🔑 2. Configure credentials

```bash
cp .env.example .env
```

The root `.env` file is loaded automatically by `gca-mcp`. Replace every
uncommented placeholder:

| Capability | Settings | Purpose |
|---|---|---|
| Image generation | `OPENAI_API_KEY`, `GAME_ASSET_BASE_URL`, `GAME_ASSET_MODEL` | Generate and edit assets with the OpenAI Images API |
| Seedance video | `ARK_API_KEY`, `SEEDANCE_URL`, `SEEDANCE_MODEL_STANDARD`, `SEEDANCE_MODEL_FAST` | Generate videos through Volcengine ARK |
| Video understanding | `DOUBAO_API_ENDPOINT`, `DOUBAO_MODEL` | Route the pinned Doubao 2.0 model through ARK |
| Background removal *(optional)* | `RMBG_BASE_URL`, optional `HF_TOKEN` | Not required: matting defaults to the model-free `color_key` route. Only used after you opt into `assets.matting.backend: auto` or `rmbg2` — see [Background removal](#background-removal) |

The OpenAI and ARK endpoints and model IDs in `.env.example` are provider
defaults, so most users only need to supply their API keys. The VLM policy
accepts only `doubao-seed-2-0-pro-260215`, with one request attempt per call.
`DOUBAO_MODEL` is pinned at runtime and is not user-configurable. Seedance
accepts local frames inline, so no object storage is required.

Store credential files securely and never commit `.env` or service-account JSON.

### Video understanding requires an ARK key

Verification is not optional-degradable: the VLM identity is pinned to Doubao
2.0 and there is no alternate provider chain. Without a working `ARK_API_KEY`,
screenshot and gameplay-video analysis fail rather than skip, which blocks the
verify phase of the pipeline. ARK keys are issued for the Volcengine
`cn-beijing` region; obtain one before running a full pipeline.

Restart the MCP server after changing `.env`, since it is read at startup.

### Background removal

GCA mattes with `assets.matting.backend: color_key` by default — pure
computation against the generated `#FF00FF` / `#00F000` staging background.
It needs no model, no API, and no credentials, and it downloads nothing. This
is the only route the project enables out of the box.

Color-key matting leaves harder edges than a segmentation model. For
soft-alpha edges you may opt into RMBG-2.0 by setting
`assets.matting.backend` to `auto` or `rmbg2` in `gca.config.yaml`, and then
either pointing `RMBG_BASE_URL` at your own deployment or installing the
in-process extra:

```bash
cd gca-mcp && uv sync --extra local-rmbg --extra dev
```

> [!IMPORTANT]
> [RMBG-2.0](https://huggingface.co/briaai/RMBG-2.0) is licensed under
> **CC BY-NC 4.0 and prohibits commercial use**. GCA does not ship, bundle, or
> download it by default — obtaining the model and complying with its license
> terms is your responsibility. See [LICENSE](LICENSE).

## 🤖 3. Generate a game with Claude Code

Start Claude Code from the repository root:

```bash
claude
```

Choose either input style:

### 💬 Option A — Describe the game directly

```console
> Create a compact top-down arena game where a lighthouse keeper defends the beacon from shadow creatures. Use responsive controls and a short boss wave.
```

### 📄 Option B — Use a Markdown game brief

Create `game-brief.md` in the repository root:

```markdown
# Lighthouse Keeper

## Game
A compact top-down arena-defense game.

## Core loop
Protect the beacon from escalating shadow waves with responsive movement and
attacks. End the session with a short boss wave.

## Style
Moody coastal fantasy, readable combat, and a compact play session.
```

Then ask Claude Code:

```text
Create the game described in @game-brief.md.
```

Both options load GCA, run the required environment preflight, generate the
game, and continue through verification automatically.

## 🕹️ 4. Preview the result

### 📁 Generated workspace

Each game is written to its own workspace:

```text
playground/<game-slug>/
├── PROMPT.md                     # Original game request
├── GAME_DESIGN.md                # Design and gameplay contracts
├── index.html                    # Browser entry point
├── package.json
├── src/                          # Phaser scenes and game logic
├── assets/
│   └── asset-manifest.json       # Runtime asset registry
├── levels/                       # Present for authored level models
└── .playtest/
    ├── state.json                # Current pipeline state
    ├── resume.json               # Recovery information
    ├── tests/                    # Test results
    └── verify/                   # Verification evidence and gates
```

### 🌐 Start the preview

Run the server from the repository root:

```bash
python3 scripts/serve_game.py playground/<game-slug> --base 3000
```

The launcher stops this game's previous server, selects the first available port
starting at `3000`, verifies that it is serving the requested workspace, and
prints the exact browser URL:

```text
URL=http://127.0.0.1:<port>
```

Open that URL to play the generated game.

### 🛑 Stop the preview

```bash
python3 scripts/serve_game.py playground/<game-slug> --stop
```

The preview server is for manual inspection. Canonical tests and release
evidence are produced by the GCA verification pipeline.

---

# 🙏 Acknowledgement

GCA builds on the following core platforms:

| Project | Role in GCA |
|---|---|
| [Claude Code](https://code.claude.com/docs) | Loads the game-pipeline Skill and coordinates the specialized agents |
| [Phaser](https://phaser.io/) | Runs each generated game in the browser; its [Skills](https://github.com/phaserjs/phaser/tree/master/skills) (MIT) are bundled as the pipeline's Phaser references |
| [RMBG-2.0](https://huggingface.co/briaai/RMBG-2.0) | Optional, opt-in soft-alpha background removal (CC BY-NC 4.0; not bundled, not enabled by default) |

We thank their maintainers and contributors for the foundations that make this
pipeline possible. We also thank
[OpenGame](https://github.com/leigest519/OpenGame) for serving as a design
reference for GCA.

---

# ⚖️ License

Copyright (C) 2026 Tencent. All rights reserved.

Game-Creation-Agents is licensed under the [Apache License 2.0](LICENSE),
except for the third-party components identified in [LICENSE](LICENSE), which
remain subject to their respective original license terms. This covers the
repository source code, agent definitions, Skills, scripts, configuration, and
documentation unless stated otherwise.

The bundled Phaser reference Skills under
`.claude/skills/game-pipeline/references/phaser/` are derived from
[Phaser](https://github.com/phaserjs/phaser/tree/master/skills) and are licensed
under the MIT License — see [LICENSE](LICENSE) for the full notice.

GCA ships no model weights. Background removal defaults to the model-free
`color_key` backend. [RMBG-2.0](https://huggingface.co/briaai/RMBG-2.0) is an
optional third-party model that is never bundled, downloaded, or enabled by
default; it is licensed under **CC BY-NC 4.0 and prohibits commercial use**.
Users who opt into `assets.matting.backend: auto` or `rmbg2` are responsible for
obtaining that model and complying with its license.

Third-party dependencies, supplied assets, and generated outputs remain subject
to their respective licenses and usage terms.

# 📚 Citation

If you find GCA helpful, please cite:

```bibtex
@article{gca2026,
  title         = {Game-Creation-Agents: Autonomous Game Production},
  author        = {Tencent Multimodal RL Team: Xiangxin Zhou, Jiaqi Tang, Ke Ma, Wenhao Yang, Jiarui Yao, Boye Niu, Haiyang Guo, Yifei Zhao, Huanjin Yao, Pu Jian, Ruihuang Li, Haitao Wu, Yushi Huang, Xiangyan Liu, Yan Sun, Kaixin Li, Tianyu Pang},
  journal       = {arXiv preprint arXiv:2610.16776},
  year          = {2026},
  eprint        = {2610.16776},
  archivePrefix = {arXiv},
  url           = {https://arxiv.org/abs/2610.16776}
}
```
