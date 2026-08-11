# Extracted library

| Distribution | Import | Purpose | Maintainer |
|---|---|---|---|
| `AvyCore` | `avycore` | Hazard scoring, zones, geometry, runout, and grounded Ollama assistance | John Stewart |

The distribution contains two focused namespaces: `avycore.hazard` and
`avycore.assistant`. Backend compatibility modules preserve the application's old
import paths while delegating to AvyCore.

Build from the repository root:

```powershell
python -m build packages\avycore --outdir dist
```

The application, Docker images, and tests install this repository copy directly.
Publishing is optional distribution work and must never be required to test or
deploy a core-model change. Never store a registry token in this repository.
