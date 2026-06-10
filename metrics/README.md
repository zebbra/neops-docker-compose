# Metrics stack

Observability stack for neops: VictoriaMetrics (TSDB), Grafana (dashboards), vmalert (alerting), and exporters for Celery, Redis, PostgreSQL, and Elasticsearch.

## Usage

### Dev

Included in `docker-compose.yaml` under the `metrics` profile:

```shell
# base services only
docker compose up

# base + full observability stack
docker compose --profile metrics up
```

### Prod

Add to `COMPOSE_FILE` in your `.env`:

```
COMPOSE_FILE=neops/docker-compose.neops.prod.yml:neops/docker-compose.neops.prod.metrics.yml
```

## Services

| Service | URL | Purpose |
|---|---|---|
| Grafana | http://localhost:3000 | Dashboards (admin / admin) |
| VictoriaMetrics | http://localhost:8428 | TSDB + query UI |
| vmalert | http://localhost:8880/vmalert | Alert rules & state |
| celery-exporter | http://localhost:9808/metrics | Raw Celery metrics |
| redis-exporter | http://localhost:9121/metrics | Raw Redis metrics |
| postgres-exporter | http://localhost:9187/metrics | Raw PostgreSQL metrics |
| elasticsearch-exporter | http://localhost:9114/metrics | Raw Elasticsearch metrics |

**Exporters:**
- `celery-exporter` — Celery task/worker metrics ([danihodovic/celery-exporter](https://github.com/danihodovic/celery-exporter))
- `redis-exporter` — Redis metrics ([oliver006/redis_exporter](https://github.com/oliver006/redis_exporter))
- `postgres-exporter` — PostgreSQL metrics ([prometheuscommunity/postgres_exporter](https://github.com/prometheus-community/postgres_exporter))
- `elasticsearch-exporter` — Elasticsearch cluster/index metrics ([prometheuscommunity/elasticsearch_exporter](https://github.com/prometheus-community/elasticsearch_exporter))
- `victoriametrics` — Prometheus-compatible TSDB, scrapes all exporters + neops backend (`/metrics`); also self-scraped for storage/ingestion metrics
- `vmalert` — Alert rule evaluation; rules live in `vmalert/rules/`; scraped for rule evaluation health and firing alert counts

## Grafana dashboards

Grafana is pre-provisioned with a VictoriaMetrics datasource and two sets of dashboards:

- **`grafana/provisioning/dashboards/neops/`** — checked into the repo (neops overview, custom metrics, per-task metrics)
- **`grafana/provisioning/dashboards/community/`** — gitignored, fetched by script

Fetch or refresh community dashboards (VictoriaMetrics, vmalert, Celery, Redis, PostgreSQL, Elasticsearch, Django):

```shell
bash metrics/fetch-dashboards.sh
```

Re-run on a fresh clone before starting Grafana.

## Scraping the local backend (dev)

VictoriaMetrics uses `host.docker.internal` (wired via `extra_hosts: host-gateway`) to reach a backend running on the host at port 8000. Add to Django's env:

```
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,host.docker.internal
```

For prod, switch the neops scrape target in `scrape_config.yml` to `backend:8000`.

## Pairs well with

The **neops_metrics** enterprise plugin (`neops_modules/enterprise/neops_metrics`) exposes a `/metrics` endpoint on the neops backend with task execution counts, device/scope stats, facts, ES sync lag, Celery queue depth, and a Redis-backed custom metric API for use inside Jinja2 task templates.

## Grafana 12 upgrade checklist

- [ ] Delete `grafana_data` volume before upgrading (avoids mixed-state migration)
- [ ] Re-run `fetch-dashboards.sh`
- [ ] Migrate `grafana/provisioning/datasources/prometheus.yml` to the new Kubernetes-style provisioning format (classic file provisioning broken in Grafana 12)
- [ ] Check for plugin auto-install failures on airgapped deployments
- [ ] Verify `$datasource` template variable resolution still works in provisioned dashboards
