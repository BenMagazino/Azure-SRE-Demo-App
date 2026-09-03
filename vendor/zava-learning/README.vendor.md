# Zava Learning vendored baseline

This directory is a curated snapshot of the healthy Zava Learning lab baseline from:

- Upstream repository: `microsoft/sre-agent`
- Upstream path: `labs/zava-learning`
- Upstream commit: `dc19cf0e773238909c61713880ec23c44571ea0b`

## Included

- `azure.yaml`
- `infra/` deployment infrastructure
- `src/` application runtime
- `chaos/` fault injection, recovery, reset, and probe scripts
- `scripts/` required deployment and SRE Agent configuration scripts
- `sre-config/` runtime manifests, agent configuration, skills, knowledge, templates, and tool definitions
- `simulator/demo.py` and `simulator/requirements.txt` as runtime/reference material

## Excluded

- Azure Developer CLI state in `.azure/`
- Local `.env` files and secrets
- Generated `simulator/config.json` and `simulator/logs/`
- `Zava-Learning-Deployment-Issues-Updated.xlsx`
- `docs/build_architecture_deck.py` and generated architecture decks
- Lock files, temporary files, and all Git metadata
- Upstream-only documentation and contributor instructions not required by this runtime snapshot

The app may modify the vendored scripts later to support portable execution without a Git checkout.
