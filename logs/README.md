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
- `./neops doctor` reads the engine's `GET /version`. Its `engine log store` probe fails when the
  engine has no store or the store refuses its pushes (`misconfigured`), and warns while the store
  is unreachable (`open`), when the engine spools lines to its database instead.

## Reading a run's logs

- **Workflow manager**: sign in to the web client at `NEOPS_WEB_URL`, then follow its
  **Workflow manager** link in the header; it opens the workflow manager inside the web client,
  with *Open in own tab* to leave the frame. The workflow manager has no sign-in of its own:
  opened directly at `NEOPS_WORKFLOWS_URL` without a web-client session, it asks you to open it
  from the web client. Then open **Runs**, then the run. Its **Logs** section shows the run's
  lines with their source (Engine, Worker or CMS), the step for worker lines, and the level;
  **Minimum level** narrows them to warnings and errors. To see one job's lines, open the job
  from **Jobs**: its page adds **Whose lines** (whole run, its step, the job). A line can arrive
  a few seconds after the event; the panel picks it up on its next poll.
- **Grafana** (`NEOPS_GRAFANA_URL`): **Dashboards**, folder **neops**, **Run logs**. Paste a run
  id into **Run id**, a job id into **Job id**, or an entity into **Entity type** and **Entity
  id** to see its lines, oldest first by time then `seq`; an empty box filters nothing. Each
  line is prefixed with its service, run, step, workflow, function block and entity, and the
  table below shows them as columns; click a run or job id there to filter by it. The workflow
  and function block dropdowns list the values seen in the selected time range, and *All*
  filters nothing; **Service**, **Level** and **Step** narrow further. The panels show at most
  5000 lines: narrow the time range for a busy one. For ad-hoc queries use **Explore** with the
  **VictoriaLogs** datasource, e.g. `workflow_execution_id:"<run id>" level:error`. Grafana
  reads the store directly, without the engine's permission check, so give its accounts only to
  people who may read every run.

  Common searches, ids left empty:
  - one function block across runs and versions: **Function block package** and **name**,
    **Function block version** *All*;
  - one workflow version's warnings: **Workflow package / name / version**, **Level** `warn`;
  - everything that touched one device across workflows: **Entity type** `device`, **Entity id**
    its CMS id (not its hostname).

  **Logs Drilldown** is switched off (`GF_PLUGINS_DISABLE_PLUGINS`): it reads Loki only, so use
  this dashboard or Explore.
- `./neops logs <service>` shows a container's stdout, not a run's lines. Use it when the stack
  itself misbehaves.

While the store is not answering, the Logs section shows *The log store is not answering*: the
engine keeps new lines in its database and they appear once the store is back, so nothing is
lost. *The log store refuses the engine* means new lines are not stored until the configuration
is fixed (see `./neops doctor`). Without this overlay the section is absent.

## Turning logs off

Remove `compose.logs.yaml` from `COMPOSE_FILE` and keep `compose.grafana.yaml`, run `./neops up`,
then `./neops compose -- up -d --remove-orphans` to remove the `victorialogs` container. The engine
and the CMS fall back to `NEOPS_LOG_BACKEND=db`, which migration `0003` wrote to `.env` and this
overlay overrides while it is present, so leave that line as it is. Removing `compose.grafana.yaml`
as well fails `./neops up` when `compose.override.yaml` still has a `grafana` entry, such as a
`ports` override: without the overlay that entry is a service with no image. The lines already in
VictoriaLogs stay in `data/logs/victoria` until you delete it.

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

The **Run logs** dashboard (`grafana/dashboards/run-logs.json`, provider
`grafana/dashboards.yml`) is provisioned read-only; save a copy to change it. An empty id box
must match lines that lack the field too, which no LogsQL filter on that field does, so the box
reads `("service<id>":* or <field>:in(<id>))`: with no id, `"service":*` holds on every line.
