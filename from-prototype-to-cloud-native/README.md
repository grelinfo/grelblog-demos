# From prototype to cloud-native app with grelmicro

Runnable code for the blog series of the same name on [blog.grel.info](https://blog.grel.info). It covers the published parts so far, and every code block in them comes from here.

The app is a small solar home: a FastAPI backend and a Svelte single-page app. Every 10 seconds, it reads an inverter and switches a water heater on solar surplus. The device calls are stubs: they return a fixed reading and log the switch.

## Days

| Day | File | Adds |
|---|---|---|
| 1 | `steps/1_prototype.py` | One container, SQLite, a task that runs every 10 s |
| 2 | `steps/2_scalable.py` | Postgres, health checks and a private ops port, so several replicas can run |

`main.py` is day two.

## Run one container

Day one runs alone, with a volume for SQLite:

```bash
cp steps/1_prototype.py main.py
podman build -t myapp .
podman run -d --name myapp -p 8000:8000 -v myapp-data:/data myapp
curl localhost:8000/api/solar
```

Run `git checkout main.py` to get day two back.

## Run two replicas

From day two, the app needs Postgres. `compose.yaml` starts Postgres, two replicas and Caddy. The Caddyfile serves `myapp.example.com` from blue and `preview.myapp.example.com` from green, with real certificates. To try it on a laptop, serve plain HTTP on localhost:

```bash
sed -i.bak -e 's/^preview.myapp.example.com {/http:\/\/preview.localhost {/' \
  -e 's/^myapp.example.com {/http:\/\/localhost {/' Caddyfile
podman build -t myapp .
podman compose up -d               # or: docker compose up -d --build
curl localhost/api/solar                               # blue
curl -H 'Host: preview.localhost' localhost/api/solar  # green
```

Port 80 needs root on some systems. If it fails, change `"80:80"` to `"8080:80"` in `compose.yaml`.

## Run the checks

```bash
(cd frontend && npm ci && npm run build)
uv run pytest              # both days run, probes, SPA, task
uv run pytest -m compose   # compose.yaml with Podman: Caddy, private /metrics, one run per tick, preview and switch
uv run ruff check . && uv run ty check
```

## Requirements

Python 3.14, [uv](https://docs.astral.sh/uv/), Node 22 for the frontend, Podman or Docker. grelmicro 0.44.2 or later.
