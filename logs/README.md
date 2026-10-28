# Logs stack

`compose.logs.yaml` adds VictoriaLogs, the log store of the workflow engine and the CMS, and a
VictoriaLogs datasource for Grafana. It requires `compose.grafana.yaml` and not the metrics
overlay: `COMPOSE_FILE=compose.yaml:<routing overlays>:compose.grafana.yaml:compose.logs.yaml`
(see `examples/logs.env`, or `examples/metrics-logs.env` for both).

## What it changes

- The engine and the CMS get `NEOPS_LOG_BACKEND=victorialogs` and
  `NEOPS_LOG_URL=http://victorialogs:9428`. The engine writes its own lines and the lines workers
  push to it; the CMS writes its task logs there instead of its database. The engine's log routes
  (`GET /workflow-execution/:id/logs`, `GET /blackboard/jobs/:jobId/logs`, `GET /workers/:uuid/logs`)
  and the monitor's log panel read from it.
- VictoriaLogs keeps lines for `NEOPS_LOG_RETENTION` (default `30d`; `<n>d` or `<n>w`, the unit is
  required, because a bare number means months to VictoriaLogs and days to the CMS). Its data
  lives in `data/logs/victoria`, which `./neops backup` does not copy: the store is
  retention-bounded and rebuilds from nothing.
- VictoriaLogs publishes no port and sits on a network of its own, `logs`, shared only with the
  engine, the CMS services and Grafana. The worker is not on it: function-block code could
  otherwise write lines for any workflow run, bypassing the engine's per-job lease, and read every
  line, bypassing the `workflow-execution-log:read` permission. A store you run yourself instead
  of this overlay (`NEOPS_LOG_BACKEND` and `NEOPS_LOG_URL` in `.env`) needs the same isolation.
- `./neops doctor` reads the engine's `GET /version` and fails its `engine log store` probe when
  the engine cannot deliver to the store or cannot read from it.

## Grafana

The datasource (`grafana/datasources/victorialogs.yml`) needs the
`victoriametrics-logs-datasource` plugin, which Grafana installs on start
(`GF_PLUGINS_PREINSTALL_SYNC`) and refuses to start without. **The first start of this overlay
therefore needs outbound internet** from the Grafana container. The plugin then lives in
`data/metrics/grafana/plugins`, and later starts work offline.

On a host without internet access, put the plugin there before the first start: download the
`victoriametrics-logs-datasource` release named in `compose.logs.yaml` from the Grafana plugin
catalogue on another machine, unpack it into `data/metrics/grafana/plugins/`, and hand the
directory to uid 472 like the rest of Grafana's data directory.

No log dashboards are provisioned; use Grafana's Explore view with the VictoriaLogs datasource.
