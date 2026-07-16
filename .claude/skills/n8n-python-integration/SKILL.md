---
name: n8n-python-integration
description: "Use Python in n8n workflows — via the Code node (Pyodide or native task runner) or by calling external Python services from n8n. Use this skill whenever the user asks to run Python in n8n, use Python libraries in n8n, call a Python script from n8n, set up a FastAPI microservice for n8n, do machine learning or data processing in n8n using Python, configure n8n task runners for Python, or troubleshoot Python execution in n8n. Also triggers for 'n8n python', 'python code node n8n', 'pandas in n8n', 'numpy n8n', 'python n8n integration'."
---

# Python in n8n

Three distinct approaches — pick based on complexity and library needs.

| Approach | Where Python runs | Library access | Best for |
|---|---|---|---|
| **Code node (Pyodide)** | Inside n8n (WebAssembly) | Pyodide-bundled only | Simple transforms, no heavy deps |
| **Code node (native runner)** | Separate task runner process | Stdlib + allowlisted pip | Self-hosted; controlled deps |
| **External microservice** | Your FastAPI/Flask container | Any pip package | ML, heavy computation, full Python power |

---

## Approach 1: Code node — Pyodide (cloud + self-hosted)

n8n runs Python via **Pyodide** (CPython compiled to WebAssembly, added in v1.0). Available on n8n Cloud and self-hosted, no configuration required.

### Built-in variables (Pyodide mode)

```python
# Run Once for All Items mode
items = _input.all()            # list of all input items
first = _input.first()          # first item
item_data = items[0].json       # dot-access works in Pyodide
return _helpers.returnJsonArray([{"result": "value"}])

# Run Once for Each Item mode  
current = _input.item           # current item
value = current.json.myField    # dot-access
return {"json": {"result": value}}
```

All built-in variables are prefixed with `_`. Type `_` in the Code node editor for autocomplete.

### Available Pyodide packages

Pyodide ships with: `numpy`, `pandas`, `scipy`, `matplotlib`, `scikit-learn`, `Pillow`, `requests` (via js bridge), `regex`, `cryptography`, `lxml`, `beautifulsoup4`, `pyarrow`, `sympy`, and ~100 more. Full list: https://pyodide.org/en/stable/usage/packages-in-pyodide.html

**On n8n Cloud: no `import` statements allowed** (not even stdlib). Self-hosted: import from the Pyodide bundle freely.

```python
# Self-hosted only
import pandas as pd
import json

items = _input.all()
data = [item.json for item in items]
df = pd.DataFrame(data)
summary = df.describe().to_dict()
return _helpers.returnJsonArray([{"summary": summary}])
```

### Limitations of Pyodide

- Slower than JavaScript (extra WASM compilation on first run)
- No filesystem access
- No subprocess, no socket, no threading
- Cannot install arbitrary pip packages
- Dot-access only (not bracket notation): `item.json.field` not `item["json"]["field"]`

---

## Approach 2: Code node — Native Python task runner (self-hosted v1.111+)

Native Python runner executes code in a **separate process** using the real CPython interpreter. Stable as of n8n v2.

### Setup (Docker — recommended)

```yaml
# docker-compose.yml
services:
  n8n:
    image: n8nio/n8n
    environment:
      - N8N_RUNNERS_ENABLED=true
      - N8N_RUNNERS_MODE=internal   # runner in same container; use 'external' for separate container
    # For external mode, also set:
    # N8N_RUNNERS_AUTH_TOKEN=your-secret-token

  # Optional: separate runner container (recommended for production)
  n8n-runner:
    image: n8nio/runners      # ships Python + allowlisted packages
    environment:
      - N8N_RUNNERS_AUTH_TOKEN=your-secret-token
      - N8N_RUNNERS_TASK_TIMEOUT=60
```

### Native runner syntax differences from Pyodide

```python
# Native runner: bracket notation ONLY (not dot access)
# _items (all-items mode) and _item (per-item mode) only — no other built-ins

# All-items mode
results = []
for item in _items:
    val = item["json"]["myField"]        # bracket notation required
    results.append({"json": {"result": val.upper()}})
# return via last expression OR explicit return
results

# Per-item mode
value = _item["json"]["price"]
{"json": {"discounted": value * 0.9}}
```

### Adding pip packages to the runner

```dockerfile
# Dockerfile extending the runners image
FROM n8nio/runners:latest
RUN pip install pandas numpy scikit-learn httpx
```

Or set `N8N_RUNNERS_PYTHON_ALLOW_BUILTIN_MODULES` and `N8N_RUNNERS_PYTHON_ALLOW_EXTERNAL_MODULES` env vars to control what's importable.

**Native runner restrictions:**
- No `exec`, `eval`, `compile`, `__import__` (denied by default for security)
- Only stdlib + explicitly allowlisted third-party packages
- `_item`/`_items` are the only available n8n variables

---

## Approach 3: External Python microservice (full power)

For ML inference, heavy processing, complex libraries (PyTorch, OpenCV, spaCy, etc.) — run Python separately and call it from n8n via **HTTP Request node** or a custom n8n node.

### FastAPI pattern (recommended)

```python
# app.py
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Any
import pandas as pd

app = FastAPI()

class ProcessRequest(BaseModel):
    items: list[dict[str, Any]]
    options: dict[str, Any] = {}

@app.post("/process")
async def process(req: ProcessRequest):
    df = pd.DataFrame([item["json"] for item in req.items])
    # ... heavy processing
    result = df.describe().to_dict()
    return {"items": [{"json": result}]}

@app.get("/health")
async def health():
    return {"status": "ok"}
```

```dockerfile
# Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yml — add alongside n8n
services:
  n8n:
    image: n8nio/n8n
    networks: [automation]
  
  python-api:
    build: ./python-api
    networks: [automation]
    # accessible to n8n as http://python-api:8000

networks:
  automation:
```

### Calling from n8n

**HTTP Request node:**
```
Method: POST
URL: http://python-api:8000/process
Body: JSON
Body Content: {{ { "items": $input.all(), "options": { "param": $json.value } } }}
```

**Or via Code node (JavaScript) for dynamic payloads:**
```js
const response = await $http.request({
  method: 'POST',
  url: 'http://python-api:8000/process',
  body: { items: $input.all(), options: { threshold: $json.threshold } },
  json: true,
});
return response.items;
```

### n8n ↔ Python data contract

n8n items have this shape — match it in your Python API:

```python
# n8n item structure
{
  "json": { "field": "value", ... },   # the main data (always present)
  "binary": { "data": { ... } },       # binary attachments (optional)
  "pairedItem": { "item": 0 }          # index linkage (optional)
}

# Typical Python processing pattern
def process_n8n_items(items: list[dict]) -> list[dict]:
    results = []
    for i, item in enumerate(items):
        data = item["json"]             # extract the payload
        # ... transform data ...
        results.append({
            "json": { "output": transformed },
            "pairedItem": { "item": i }  # preserve item linkage
        })
    return results
```

### Auth between n8n and Python service

```python
# FastAPI with API key auth
from fastapi.security import APIKeyHeader
from fastapi import Security, HTTPException

API_KEY = os.environ["INTERNAL_API_KEY"]
api_key_header = APIKeyHeader(name="X-API-Key")

async def verify_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(403)

@app.post("/process", dependencies=[Depends(verify_key)])
async def process(req: ProcessRequest): ...
```

In n8n: use **Header Auth** credential with `X-API-Key` header on the HTTP Request node.

---

## Pattern cheatsheet

| Task | Best approach |
|---|---|
| JSON transform, string manipulation | Code node (Pyodide) |
| `pandas`, `numpy` with self-hosted n8n | Code node (native runner) |
| PyTorch, scikit-learn inference | FastAPI microservice |
| OpenCV, image processing | FastAPI microservice |
| Web scraping (playwright, selenium) | FastAPI microservice |
| LLM with custom Python lib | FastAPI microservice or n8n AI node |
| Simple stats on Cloud | Code node (Pyodide) — no imports needed |
| ETL with big DataFrames | FastAPI microservice (avoid in-node) |

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` on Cloud | No imports on Cloud | Move to self-hosted or use FastAPI |
| `ModuleNotFoundError` on self-hosted (Pyodide) | Package not in Pyodide bundle | Use native runner or FastAPI |
| `AttributeError: item has no attribute 'json'` | Using native runner with dot access | Switch to `item["json"]` |
| `Timeout executing task` | Long-running Python | Increase `N8N_RUNNERS_TASK_TIMEOUT` |
| `Connection refused` to FastAPI | Container networking | Ensure shared Docker network; use service name as hostname |
| Slow first Code node run | Pyodide WASM download | Expected; subsequent runs faster |
| `exec is not allowed` | Native runner security policy | Use allowed stdlib; no dynamic eval |

---

## Useful env vars (self-hosted)

```bash
# Task runners
N8N_RUNNERS_ENABLED=true
N8N_RUNNERS_MODE=internal            # or 'external'
N8N_RUNNERS_AUTH_TOKEN=secret
N8N_RUNNERS_TASK_TIMEOUT=60          # seconds
N8N_RUNNERS_MAX_PAYLOAD=67108864     # bytes (64MB default)

# Python allowlists (native runner)
N8N_RUNNERS_PYTHON_ALLOW_BUILTIN_MODULES=os,sys,json,math,datetime,re
N8N_RUNNERS_PYTHON_ALLOW_EXTERNAL_MODULES=pandas,numpy,httpx
```

---

## Reference links

- Code node docs: https://docs.n8n.io/code/code-node/
- Pyodide packages: https://pyodide.org/en/stable/usage/packages-in-pyodide.html
- Task runners setup: https://docs.n8n.io/hosting/configuration/task-runners/
- Built-in methods: https://docs.n8n.io/code/builtin/
- Community node (n8n-nodes-python): https://github.com/naskio/n8n-nodes-python
