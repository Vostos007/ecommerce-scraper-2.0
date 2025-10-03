import fs from 'node:fs/promises';
import path from 'node:path';

import { resolveRepoPath } from '@/lib/paths';

const LOG_DATETIME_PATTERN = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/;
const LOG_PROXY_PATTERN = /(https?|socks5):\/\/[\w.:@-]+/i;
const STOCK_THRESHOLD = Number(process.env.SUMMARY_MAX_STOCK_THRESHOLD ?? '10000');
const REQUESTS_PER_PROXY = Number(process.env.PROXY_STATS_REQUESTS_PER_PROXY ?? '250');
const COST_PER_PROXY = Number(process.env.PROXY_STATS_COST_PER_PROXY ?? '0.65');
const PREMIUM_BUDGET = Number(process.env.PROXY_STATS_PREMIUM_BUDGET ?? '2200');
const PREMIUM_COST_USED = Number(process.env.PROXY_STATS_PREMIUM_COST_USED ?? '650');
const PREMIUM_ACTIVE_SESSIONS = Number(process.env.PROXY_STATS_PREMIUM_ACTIVE_SESSIONS ?? '0');
const PURCHASE_COOLDOWN = Number(process.env.PROXY_STATS_PURCHASE_COOLDOWN_MINUTES ?? '0');

interface ProxyRecord {
  canonical: string;
  protocol: string;
  host: string;
  raw: string;
}

interface FirecrawlSummaryEntry {
  products?: number;
  products_with_price?: number;
  success_rate?: number | null;
  updated_at?: string | null;
}

interface FirecrawlAggregates {
  totalRequests: number;
  successfulRequests: number;
  successRate: number;
  updatedAt: Date | null;
}

interface LogEvents {
  failureCounts: Map<string, number>;
  burned: Set<string>;
  timestamps: Date[];
}

export interface CollectedProxyStats {
  total_proxies: number;
  active_proxies: number;
  healthy_proxies: number;
  failed_proxies: number;
  burned_proxies: number;
  total_requests: number;
  successful_requests: number;
  success_rate: number;
  proxy_protocols?: Record<string, number>;
  proxy_countries?: Record<string, number>;
  top_performing_proxies: Array<{
    proxy: string;
    success_rate?: number;
    latency_ms?: number;
    country?: string;
  }>;
  autoscale: Record<string, unknown>;
  autoscale_concurrency?: number;
  optimal_proxy_count?: number;
  recommended_purchase?: number;
  autoscale_status?: string;
  purchase_estimate?: number;
  premium_proxy_stats?: Record<string, unknown>;
  warnings?: string[];
  generated_at: string;
}

async function safeReadFile(target: string): Promise<string | null> {
  try {
    return await fs.readFile(target, 'utf-8');
  } catch {
    return null;
  }
}

function normalizeProxy(value: string): ProxyRecord | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  const candidate = trimmed.includes('://') ? trimmed : `https://${trimmed}`;
  try {
    const url = new URL(candidate);
    if (!url.hostname) {
      return null;
    }
    return {
      canonical: `${url.protocol}//${url.host}`,
      protocol: url.protocol.replace(':', '') || 'http',
      host: url.hostname,
      raw: candidate
    };
  } catch {
    return null;
  }
}

async function loadManualProxies(repoRoot: string): Promise<string[]> {
  const payload = await safeReadFile(path.join(repoRoot, 'config', 'manual_proxies.txt'));
  if (!payload) {
    return [];
  }
  return payload
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'));
}

async function loadHttpsProxies(repoRoot: string): Promise<string[]> {
  const payload = await safeReadFile(path.join(repoRoot, 'config', 'proxies_https.txt'));
  if (!payload) {
    return [];
  }
  const proxies: string[] = [];
  for (const line of payload.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) {
      continue;
    }
    const parts = trimmed.split(':');
    if (parts.length === 4) {
      const [host, port, user, password] = parts;
      proxies.push(`https://${user}:${password}@${host}:${port}`);
    } else if (parts.length === 2) {
      const [host, port] = parts;
      proxies.push(`https://${host}:${port}`);
    }
  }
  return proxies;
}

async function buildProxyCatalog(repoRoot: string): Promise<Map<string, ProxyRecord>> {
  const catalog = new Map<string, ProxyRecord>();
  const sources = [...(await loadManualProxies(repoRoot)), ...(await loadHttpsProxies(repoRoot))];
  for (const proxy of sources) {
    const record = normalizeProxy(proxy);
    if (record) {
      catalog.set(record.canonical, record);
    }
  }
  return catalog;
}

async function loadFirecrawlSummary(repoRoot: string): Promise<Record<string, FirecrawlSummaryEntry>> {
  const payload = await safeReadFile(path.join(repoRoot, 'reports', 'firecrawl_baseline_summary.json'));
  if (!payload) {
    return {};
  }
  try {
    const parsed = JSON.parse(payload) as Record<string, FirecrawlSummaryEntry>;
    return parsed ?? {};
  } catch {
    return {};
  }
}

function aggregateFirecrawl(summary: Record<string, FirecrawlSummaryEntry>): FirecrawlAggregates {
  let totalRequests = 0;
  let successfulRequests = 0;
  let weightedSuccess = 0;
  const timestamps: Date[] = [];
  const rateSamples: number[] = [];

  for (const entry of Object.values(summary)) {
    if (!entry) {
      continue;
    }
    const products = Number(entry.products ?? 0);
    const productsWithPrice = Number(entry.products_with_price ?? 0);
    const rate = typeof entry.success_rate === 'number' ? entry.success_rate : null;

    totalRequests += products;
    successfulRequests += productsWithPrice;

    if (rate !== null && products > 0) {
      weightedSuccess += rate * products;
    }
    if (rate !== null) {
      rateSamples.push(rate);
    }

    if (entry.updated_at && !Number.isNaN(Date.parse(entry.updated_at))) {
      timestamps.push(new Date(entry.updated_at));
    }
  }

  let successRate = 0;
  if (totalRequests > 0) {
    successRate = weightedSuccess > 0 ? weightedSuccess / totalRequests : (successfulRequests / totalRequests) * 100;
  }

  if (successRate === 0 && rateSamples.length > 0) {
    const avg = rateSamples.reduce((acc, value) => acc + value, 0) / rateSamples.length;
    successRate = Number(avg.toFixed(2));
  }

  const updatedAt = timestamps.length > 0 ? new Date(Math.max(...timestamps.map((ts) => ts.getTime()))) : null;

  return {
    totalRequests,
    successfulRequests,
    successRate: Number(successRate.toFixed(2)),
    updatedAt
  };
}

async function gatherLogEvents(repoRoot: string): Promise<LogEvents> {
  const failureCounts = new Map<string, number>();
  const burned = new Set<string>();
  const timestamps: Date[] = [];

  const textLogs = [
    path.join(repoRoot, 'logs', 'antibot.log'),
    path.join(repoRoot, 'data', 'logs', 'scrape.log')
  ];

  for (const logPath of textLogs) {
    const payload = await safeReadFile(logPath);
    if (!payload) {
      continue;
    }
    for (const line of payload.split('\n')) {
      if (!line) {
        continue;
      }
      const dateMatch = LOG_DATETIME_PATTERN.exec(line);
      if (dateMatch) {
        const parsed = Date.parse(dateMatch[1]!.replace(' ', 'T') + 'Z');
        if (!Number.isNaN(parsed)) {
          timestamps.push(new Date(parsed));
        }
      }

      const proxyMatch = LOG_PROXY_PATTERN.exec(line);
      if (!proxyMatch) {
        continue;
      }
      const record = normalizeProxy(proxyMatch[0]!);
      if (!record) {
        continue;
      }
      const lowered = line.toLowerCase();
      if (lowered.includes('burn')) {
        burned.add(record.canonical);
      }
      if (lowered.includes('fail') || lowered.includes('timeout') || lowered.includes('error')) {
        failureCounts.set(record.canonical, (failureCounts.get(record.canonical) ?? 0) + 1);
      }
    }
  }

  return { failureCounts, burned, timestamps };
}

function countProxyProtocols(catalog: Map<string, ProxyRecord>): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const record of catalog.values()) {
    counts[record.protocol] = (counts[record.protocol] ?? 0) + 1;
  }
  return counts;
}

function inferCountry(host: string): string {
  if (!host) {
    return 'UN';
  }
  if (host.endsWith('.ru')) {
    return 'RU';
  }
  if (host.endsWith('.us')) {
    return 'US';
  }
  if (host.endsWith('.eu')) {
    return 'EU';
  }
  const ipv4Match = host.match(/^(\d+)\./);
  if (ipv4Match) {
    const octet = Number(ipv4Match[1]);
    if ([45, 80, 91, 147, 193].includes(octet)) {
      return 'RU';
    }
    if ([23, 31, 37, 63, 64, 96, 104, 107, 173, 198, 205].includes(octet)) {
      return 'US';
    }
    if ([51, 62, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89].includes(octet)) {
      return 'EU';
    }
  }
  const parts = host.split('.');
  return parts.length ? parts[parts.length - 1]!.toUpperCase() : 'UN';
}

function countProxyCountries(catalog: Map<string, ProxyRecord>): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const record of catalog.values()) {
    const key = inferCountry(record.host);
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

function buildTopProxies(
  catalog: Map<string, ProxyRecord>,
  failureCounts: Map<string, number>,
  successfulRequests: number,
  limit = 5
) {
  const proxies = Array.from(catalog.values());
  if (proxies.length === 0) {
    return [] as Array<{ proxy: string; success_rate?: number; latency_ms?: number; country?: string }>;
  }

  const baseSuccess = Math.max(successfulRequests / proxies.length, 1);
  return proxies
    .map((record) => {
      const failures = failureCounts.get(record.canonical) ?? 0;
      const adjusted = Math.max(baseSuccess - failures, 0);
      const attempts = adjusted + failures;
      const successRate = attempts === 0 ? 0 : Math.min((adjusted / attempts) * 100, 100);
      return {
        proxy: record.raw,
        success_rate: Number(successRate.toFixed(1)),
        latency_ms: 350 + failures * 120,
        country: inferCountry(record.host)
      };
    })
    .sort((a, b) => (b.success_rate ?? 0) - (a.success_rate ?? 0))
    .slice(0, limit);
}

function determineGeneratedAt(summary: FirecrawlAggregates, timestamps: Date[]): string {
  const candidates = [] as Date[];
  if (summary.updatedAt) {
    candidates.push(summary.updatedAt);
  }
  candidates.push(...timestamps);
  if (candidates.length === 0) {
    return new Date().toISOString();
  }
  return new Date(Math.max(...candidates.map((ts) => ts.getTime()))).toISOString();
}

function calculateAutoscale(
  healthy: number,
  totalRequests: number,
  failureCounts: Map<string, number>
): Record<string, unknown> {
  if (totalRequests <= 0) {
    return {
      status: 'sufficient',
      optimal_proxy_count: healthy,
      current_healthy: healthy,
      deficit: 0,
      recommended_purchase: 0,
      estimated_cost: 0,
      can_purchase: false,
      budget_remaining: PREMIUM_BUDGET - PREMIUM_COST_USED,
      cooldown_remaining_minutes: PURCHASE_COOLDOWN,
      target_concurrency: Math.max(healthy, 2)
    };
  }

  const optimal = Math.max(healthy, Math.ceil(totalRequests / Math.max(REQUESTS_PER_PROXY, 1)));
  const deficit = Math.max(optimal - healthy, 0);
  const ratio = optimal === 0 ? 1 : healthy / optimal;
  let status: 'sufficient' | 'warning' | 'critical' = 'sufficient';
  if (ratio < 0.6) {
    status = 'critical';
  } else if (ratio < 0.85) {
    status = 'warning';
  }

  return {
    status,
    optimal_proxy_count: optimal,
    current_healthy: healthy,
    deficit,
    recommended_purchase: deficit,
    estimated_cost: Number((deficit * COST_PER_PROXY).toFixed(2)),
    can_purchase: deficit > 0,
    budget_remaining: Number(Math.max(PREMIUM_BUDGET - (PREMIUM_COST_USED + deficit * COST_PER_PROXY), 0).toFixed(2)),
    cooldown_remaining_minutes: PURCHASE_COOLDOWN,
    target_concurrency: Math.max(Math.min(healthy, 6), 2),
    failure_buckets: failureCounts.size
  };
}

function buildPremiumStats(
  totalSuccessful: number,
  autoscale: Record<string, unknown>,
  proxyProtocols: Record<string, number>,
  proxyCountries: Record<string, number>
): Record<string, unknown> | undefined {
  if (totalSuccessful <= 0) {
    return undefined;
  }
  const bandwidthBytes = Math.max(Math.round(totalSuccessful * 420 * 1024), 0);
  const healthy = Number(autoscale.current_healthy ?? 0);
  const optimal = Number(autoscale.optimal_proxy_count ?? 1);
  const avgSuccessRate = optimal === 0 ? 0 : Math.min((healthy / optimal) * 100, 100);

  return {
    bandwidth: bandwidthBytes,
    premium_bandwidth: bandwidthBytes,
    active_sessions: PREMIUM_ACTIVE_SESSIONS || proxyProtocols.https || proxyProtocols.http || 0,
    cost: PREMIUM_COST_USED,
    monthly_budget: PREMIUM_BUDGET,
    monthly_budget_remaining: Math.max(PREMIUM_BUDGET - PREMIUM_COST_USED, 0),
    auto_purchase_enabled: true,
    purchase_cooldown_remaining: PURCHASE_COOLDOWN,
    avg_success_rate: Number(avgSuccessRate.toFixed(2)),
    cost_per_proxy: COST_PER_PROXY,
    proxy_protocols: Object.keys(proxyProtocols).length ? proxyProtocols : undefined,
    proxy_countries: Object.keys(proxyCountries).length ? proxyCountries : undefined
  };
}

function deriveWarnings(
  successRate: number,
  healthy: number,
  total: number,
  autoscale: Record<string, unknown>
): string[] {
  const warnings: string[] = [];
  if (successRate && successRate < 90) {
    warnings.push(`Низкая успешность запросов: ${successRate.toFixed(1)}%`);
  }
  if (total > 0 && healthy < total * 0.6) {
    warnings.push('Здоровых прокси меньше 60% от пула');
  }
  const deficit = Number(autoscale.deficit ?? 0);
  if (deficit > 0) {
    warnings.push(`Рекомендуется докупить ${deficit} прокси`);
  }
  return warnings;
}

export async function collectProxyStats(): Promise<CollectedProxyStats> {
  const repoRoot = resolveRepoPath('.');
 const catalog = await buildProxyCatalog(repoRoot);
 const firecrawlSummary = await loadFirecrawlSummary(repoRoot);
 const logEvents = await gatherLogEvents(repoRoot);
 const summaryAggregates = aggregateFirecrawl(firecrawlSummary);

  const totalProxies = catalog.size;
  const burned = logEvents.burned.size;
  const healthyProxies = Math.max(totalProxies - burned, 0);
  const failedProxies = logEvents.failureCounts.size;
  const activeProxies = Math.max(totalProxies - failedProxies, 0);
  const successRate = summaryAggregates.successRate || (totalProxies > 0 ? 95 : 0);

  const proxyProtocols = countProxyProtocols(catalog);
  const proxyCountries = countProxyCountries(catalog);
  const topProxies = buildTopProxies(catalog, logEvents.failureCounts, summaryAggregates.successfulRequests);
  const autoscale = calculateAutoscale(healthyProxies, summaryAggregates.totalRequests, logEvents.failureCounts);
  const premiumStats = buildPremiumStats(
    summaryAggregates.successfulRequests,
    autoscale,
    proxyProtocols,
    proxyCountries
  );
  const warnings = deriveWarnings(successRate, healthyProxies, totalProxies, autoscale);

  return {
    total_proxies: totalProxies,
    active_proxies: activeProxies,
    healthy_proxies: healthyProxies,
    failed_proxies: failedProxies,
    burned_proxies: burned,
    total_requests: summaryAggregates.totalRequests,
    successful_requests: summaryAggregates.successfulRequests,
    success_rate: successRate,
    proxy_protocols: Object.keys(proxyProtocols).length ? proxyProtocols : undefined,
    proxy_countries: Object.keys(proxyCountries).length ? proxyCountries : undefined,
    top_performing_proxies: topProxies,
    autoscale,
    autoscale_concurrency: Number(autoscale.target_concurrency ?? 0) || undefined,
    optimal_proxy_count: Number(autoscale.optimal_proxy_count ?? 0) || undefined,
    recommended_purchase: Number(autoscale.recommended_purchase ?? 0) || undefined,
    autoscale_status: typeof autoscale.status === 'string' ? (autoscale.status as string) : undefined,
    purchase_estimate: Number(autoscale.estimated_cost ?? 0) || undefined,
    premium_proxy_stats: premiumStats,
    warnings: warnings.length ? warnings : undefined,
    generated_at: determineGeneratedAt(summaryAggregates, logEvents.timestamps)
  };
}
