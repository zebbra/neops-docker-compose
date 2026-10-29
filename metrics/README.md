# Metrics stack

Observability stack for Neops: VictoriaMetrics (TSDB), vmalert (alerting) and exporters for Celery,
Redis, PostgreSQL and Elasticsearch, shown in Grafana.

## Usage

Add `compose.grafana.yaml` and then `compose.metrics.yaml` to `COMPOSE_FILE` in `.env` (see
`examples/metrics.env`) and run `./neops up`. Grafana has an overlay of its own because the logs
overlay uses it too; an install that named only `compose.metrics.yaml` gets `compose.grafana.yaml`
added by migration `0003` on its next `./neops up`.

Grafana is reachable on the host at `127.0.0.1:${NEOPS_GRAFANA_PORT:-3000}` (or
`NEOPS_BIND_ADDRESS`) and, when `NEOPS_GRAFANA_URL` is set with the Traefik overlay, at that public
URL. The admin user is `admin` with the password `NEOPS_GRAFANA_ADMIN_PASSWORD`. Metrics live in
`data/metrics/victoria` and Grafana's own state in `data/metrics/grafana` (bind mounts, like
everything else); `./neops backup` copies neither.

Core itself exposes no Prometheus `/metrics` endpoint in this stack (that needs the
**neops_metrics** enterprise plugin, which compose does not enable), so this overlay covers only
Celery, Redis, PostgreSQL and Elasticsearch.

## Services

None of these publishes a port except Grafana; the others are reachable on the compose network
only, for example with `./neops compose -- exec victoriametrics wget -qO- http://127.0.0.1:8428/health`.

| Service | Address on the compose network | Purpose |
|---|---|---|
| grafana | http://grafana:3000 | Dashboards (`compose.grafana.yaml`) |
| victoriametrics | http://victoriametrics:8428 | TSDB, scrape and query UI |
| vmalert | http://vmalert:8880/vmalert | Alert rules and their state |
| celery-exporter | http://celery-exporter:9808/metrics | Celery task and worker metrics |
| redis-exporter | http://redis-exporter:9121/metrics | Redis metrics |
| postgres-exporter-cms | http://postgres-exporter-cms:9187/metrics | PostgreSQL metrics for the CMS database |
| postgres-exporter-engine | http://postgres-exporter-engine:9187/metrics | PostgreSQL metrics for the engine database |
| elasticsearch-exporter | http://elasticsearch-exporter:9114/metrics | Elasticsearch cluster and index metrics |

VictoriaMetrics scrapes every exporter (`scrape_config.yml`) and itself; vmalert evaluates the
rules in `vmalert/rules/`.

## Grafana provisioning

Grafana 12 reads classic file provisioning (`apiVersion: 1`). This overlay mounts two things into
it, file by file, so the log datasource `./neops render` writes and the logs overlay's dashboard
can sit next to them:

- `grafana/provisioning/datasources/prometheus.yml`: the VictoriaMetrics datasource (default).
- `grafana/provisioning/dashboards/dashboards.yml`: the dashboard providers, mounted alone, and
  the directory around it at `/etc/grafana/dashboards/metrics`, with the Neops dashboards in
  `neops/` (overview, custom metrics, per-task metrics). A `community/` directory next to it is
  picked up too; it is not shipped, and `bash metrics/fetch-dashboards.sh` fills it with the
  community dashboards for VictoriaMetrics, vmalert, Celery, Redis, PostgreSQL, Elasticsearch and
  Django.

## Upgrading from Grafana 11

The release that split out `compose.grafana.yaml` moved Grafana from 11.5.2 to 12.4.9. Grafana
migrates `data/metrics/grafana` on its first start and does not support going back: copy that
directory before the upgrade if you may need to return to the previous release (`./neops backup`
does not). The community dashboards were written for Grafana 11; check them after the upgrade.
