#!/usr/bin/env bash
set -euo pipefail

DASHBOARDS=(
  "10229:victoriametrics-overview"
  "14950:vmalert"
  "17508:celery-exporter"
  "17509:celery-overview"
  "20076:celery-tasks"
  "763:redis-exporter"
  "9628:postgres"
  "14191:elasticsearch"
  "17613:django-overview"
  "17616:django-requests"
  "17617:django-database"
  "24933:django-models"
)

DATASOURCE_UID="victoriametrics"
OUT="$(dirname "$0")/grafana/provisioning/dashboards/community"
mkdir -p "$OUT"

for entry in "${DASHBOARDS[@]}"; do
  id="${entry%%:*}"
  name="${entry##*:}"
  echo -n "Fetching $name ($id)... "
  curl -sf "https://grafana.com/api/dashboards/${id}/revisions/latest/download" \
    | sed "s/\${DS_PROM}/${DATASOURCE_UID}/g" \
    | sed "s/\${DS_PROMETHEUS}/${DATASOURCE_UID}/g" \
    > "${OUT}/${name}.json"
  echo "ok"
done

echo "Done — dashboards saved to $OUT"
