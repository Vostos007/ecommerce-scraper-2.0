import type { NextRequest } from 'next/server';
import { collectDefaultMetrics, Counter, Gauge, Histogram, Registry } from 'prom-client';

const metricsRegistry = new Registry();
collectDefaultMetrics({ register: metricsRegistry });

const metrics = {
  httpRequestsTotal: new Counter({
    name: 'http_requests_total',
    help: 'Total HTTP requests handled by the dashboard.',
    labelNames: ['method', 'route', 'status'],
    registers: [metricsRegistry]
  }),
  httpRequestDuration: new Histogram({
    name: 'http_request_duration_seconds',
    help: 'Histogram of HTTP request durations.',
    labelNames: ['method', 'route'],
    buckets: [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
    registers: [metricsRegistry]
  }),
  httpInFlight: new Gauge({
    name: 'http_requests_in_flight',
    help: 'Current number of API requests being processed.',
    registers: [metricsRegistry]
  }),
  exportJobsTotal: new Counter({
    name: 'export_jobs_total',
    help: 'Total export jobs by status.',
    labelNames: ['status', 'site'],
    registers: [metricsRegistry]
  }),
  exportJobDuration: new Histogram({
    name: 'export_job_duration_seconds',
    help: 'Export job duration segmented by site.',
    labelNames: ['site'],
    buckets: [30, 60, 120, 300, 600, 1200, 1800, 3600],
    registers: [metricsRegistry]
  }),
  exportItemsScraped: new Counter({
    name: 'export_items_scraped_total',
    help: 'Total scraped items reported by export jobs.',
    labelNames: ['site'],
    registers: [metricsRegistry]
  }),
  proxyTotal: new Gauge({
    name: 'proxy_total',
    help: 'Current number of proxies by status.',
    labelNames: ['status'],
    registers: [metricsRegistry]
  }),
  proxyBandwidth: new Gauge({
    name: 'proxy_bandwidth_bytes',
    help: 'Proxy bandwidth usage in bytes.',
    labelNames: ['type'],
    registers: [metricsRegistry]
  }),
  authLoginAttempts: new Counter({
    name: 'auth_login_attempts_total',
    help: 'Authentication attempts grouped by result.',
    labelNames: ['status'],
    registers: [metricsRegistry]
  }),
  authActiveSessions: new Gauge({
    name: 'auth_active_sessions',
    help: 'Number of active authenticated sessions.',
    registers: [metricsRegistry]
  }),
  fileUploads: new Counter({
    name: 'file_uploads_total',
    help: 'Total file upload attempts.',
    labelNames: ['status', 'site'],
    registers: [metricsRegistry]
  }),
  fileDownloads: new Counter({
    name: 'file_downloads_total',
    help: 'Total file downloads by site and type.',
    labelNames: ['site', 'type'],
    registers: [metricsRegistry]
  }),
  fileTransferBytes: new Counter({
    name: 'file_transfer_bytes_total',
    help: 'Total bytes transferred in uploads/downloads.',
    labelNames: ['operation', 'site'],
    registers: [metricsRegistry]
  }),
  processCpuUsage: new Gauge({
    name: 'process_cpu_usage_percent',
    help: 'CPU usage percentage of the Next.js process.',
    registers: [metricsRegistry]
  }),
  nodeMemoryUsage: new Gauge({
    name: 'nodejs_memory_usage_bytes',
    help: 'Node.js memory usage by type.',
    labelNames: ['type'],
    registers: [metricsRegistry]
  })
};

let activeSessions = 0;

function clampSessionGauge() {
  if (activeSessions < 0) {
    activeSessions = 0;
  }
  metrics.authActiveSessions.set(activeSessions);
}

clampSessionGauge();

export function incrementActiveSessions(): void {
  activeSessions += 1;
  clampSessionGauge();
}

export function decrementActiveSessions(): void {
  activeSessions -= 1;
  clampSessionGauge();
}

export function recordLoginAttempt(status: 'success' | 'failure' | 'rate_limited'): void {
  metrics.authLoginAttempts.labels(status).inc();
}

export function recordExportStart(site: string): void {
  metrics.exportJobsTotal.labels('started', site).inc();
}

export function recordExportResult(site: string, result: 'success' | 'failure', durationMs: number): void {
  metrics.exportJobsTotal.labels(result, site).inc();
  metrics.exportJobDuration.labels(site).observe(Math.max(durationMs / 1000, 0));
}

export function recordExportItems(site: string, count: number): void {
  if (Number.isFinite(count) && count > 0) {
    metrics.exportItemsScraped.labels(site).inc(count);
  }
}

export function recordProxySnapshot(data: {
  total?: number;
  healthy?: number;
  active?: number;
  failed?: number;
  burned?: number;
  bandwidthBytes?: number;
  premiumBandwidthBytes?: number;
}): void {
  metrics.proxyTotal.labels('total').set(data.total ?? 0);
  metrics.proxyTotal.labels('healthy').set(data.healthy ?? 0);
  metrics.proxyTotal.labels('active').set(data.active ?? 0);
  metrics.proxyTotal.labels('failed').set(data.failed ?? 0);
  metrics.proxyTotal.labels('burned').set(data.burned ?? 0);
  if (data.bandwidthBytes !== undefined) {
    metrics.proxyBandwidth.labels('total').set(data.bandwidthBytes);
  }
  if (data.premiumBandwidthBytes !== undefined) {
    metrics.proxyBandwidth.labels('premium').set(data.premiumBandwidthBytes);
  }
}

export function recordFileUpload(site: string, bytes: number, status: 'success' | 'failure'): void {
  metrics.fileUploads.labels(status, site).inc();
  if (bytes > 0) {
    metrics.fileTransferBytes.labels('upload', site).inc(bytes);
  }
}

export function recordFileDownload(site: string, bytes: number, type: string): void {
  metrics.fileDownloads.labels(site, type).inc();
  if (bytes > 0) {
    metrics.fileTransferBytes.labels('download', site).inc(bytes);
  }
}

export function updateProcessMetrics(): void {
  const cpuUserMicro = process.cpuUsage().user;
  metrics.processCpuUsage.set(Number.isFinite(cpuUserMicro) ? cpuUserMicro / 10_000 : 0);
  const mem = process.memoryUsage();
  metrics.nodeMemoryUsage.labels('rss').set(mem.rss);
  metrics.nodeMemoryUsage.labels('heapTotal').set(mem.heapTotal);
  metrics.nodeMemoryUsage.labels('heapUsed').set(mem.heapUsed);
}

type ApiHandler = (request: NextRequest, ...args: any[]) => Promise<Response> | Response;

export function withApiMetrics<T extends ApiHandler>(route: string, handler: T): T {
  const wrapped = (async (request: NextRequest, ...args: any[]) => {
    const method = request.method?.toUpperCase() ?? 'UNKNOWN';
    metrics.httpInFlight.inc();
    const stopTimer = metrics.httpRequestDuration.labels(method, route).startTimer();
    try {
      const response = await handler(request, ...args);
      const status = response instanceof Response ? response.status : 200;
      metrics.httpRequestsTotal.labels(method, route, String(status)).inc();
      return response;
    } catch (error) {
      metrics.httpRequestsTotal.labels(method, route, '500').inc();
      throw error;
    } finally {
      metrics.httpInFlight.dec();
      stopTimer();
    }
  }) as T;
  return wrapped;
}

export { metrics, metricsRegistry };
