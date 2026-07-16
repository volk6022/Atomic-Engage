from prometheus_client import Counter, Gauge


fleet_accounts_total = Gauge(
    "fleet_accounts_total", "Total accounts by status", ["status"]
)
fleet_tasks_dispatched_total = Counter(
    "fleet_tasks_dispatched_total", "Tasks dispatched by action", ["action"]
)
fleet_tasks_completed_total = Counter(
    "fleet_tasks_completed_total", "Tasks completed by result", ["result"]
)
fleet_webhook_deliveries_total = Counter(
    "fleet_webhook_deliveries_total", "Webhook deliveries by status", ["status"]
)
fleet_watcher_sessions_active = Gauge(
    "fleet_watcher_sessions_active", "Active watcher sessions"
)
fleet_proxy_health_failures_total = Counter(
    "fleet_proxy_health_failures_total", "Proxy health check failures"
)
fleet_geo_rejections_total = Counter(
    "fleet_geo_rejections_total", "Geographic rejections"
)
