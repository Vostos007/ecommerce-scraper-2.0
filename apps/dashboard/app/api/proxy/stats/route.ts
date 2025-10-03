import { NextRequest, NextResponse } from 'next/server';
import { z } from 'zod';

import { TokenBucketLimiter } from '@/lib/rate-limit';
import { proxyStatsSchema } from '@/lib/validations';
import { recordProxySnapshot, withApiMetrics } from '@/lib/metrics';
import { collectProxyStats as collectProxyMetrics } from '@/lib/proxy-stats';

export const runtime = 'nodejs';

type ProxyStats = z.infer<typeof proxyStatsSchema>;

interface CacheEntry {
  data: ProxyStats;
  expiresAt: number;
}

const CACHE_TTL_MS = 30_000;
const limiter = new TokenBucketLimiter({ capacity: 30, windowMs: 60_000 });

let cache: CacheEntry | null = null;

function isCacheValid(): boolean {
  return Boolean(cache && cache.expiresAt > Date.now());
}

async function collectProxyStats(): Promise<ProxyStats> {
  const payload = await collectProxyMetrics();
  return proxyStatsSchema.parse(payload);
}

function getClientIp(request: NextRequest): string {
  const forwarded = request.headers.get('x-forwarded-for');
  if (forwarded) {
    const candidate = forwarded.split(',')[0]?.trim();
    if (candidate) {
      return candidate;
    }
  }
  const real = request.headers.get('x-real-ip');
  return real ?? '127.0.0.1';
}

const handler = async (request: NextRequest) => {
  const ip = getClientIp(request);
  if (!limiter.take(ip)) {
    return NextResponse.json(
      { error: 'Too many requests. Попробуйте повторить позже.' },
      { status: 429 }
    );
  }

  try {
    if (isCacheValid()) {
      const cached = cache!.data;
      const cachedSnapshot: Parameters<typeof recordProxySnapshot>[0] = {};
      if (cached.total_proxies !== undefined) cachedSnapshot.total = cached.total_proxies;
      if (cached.healthy_proxies !== undefined) cachedSnapshot.healthy = cached.healthy_proxies;
      if (cached.active_proxies !== undefined) cachedSnapshot.active = cached.active_proxies;
      if (cached.failed_proxies !== undefined) cachedSnapshot.failed = cached.failed_proxies;
      if (cached.burned_proxies !== undefined) cachedSnapshot.burned = cached.burned_proxies;
      if (cached.premium_proxy_stats?.bandwidth !== undefined) {
        cachedSnapshot.bandwidthBytes = cached.premium_proxy_stats.bandwidth;
        cachedSnapshot.premiumBandwidthBytes = cached.premium_proxy_stats.bandwidth;
      }
      recordProxySnapshot(cachedSnapshot);
      return NextResponse.json(cached);
    }

    const data = await collectProxyStats();
    const snapshot: Parameters<typeof recordProxySnapshot>[0] = {};
    if (data.total_proxies !== undefined) snapshot.total = data.total_proxies;
    if (data.healthy_proxies !== undefined) snapshot.healthy = data.healthy_proxies;
    if (data.active_proxies !== undefined) snapshot.active = data.active_proxies;
    if (data.failed_proxies !== undefined) snapshot.failed = data.failed_proxies;
    if (data.burned_proxies !== undefined) snapshot.burned = data.burned_proxies;
    if (data.premium_proxy_stats?.bandwidth !== undefined) {
      snapshot.bandwidthBytes = data.premium_proxy_stats.bandwidth;
      snapshot.premiumBandwidthBytes = data.premium_proxy_stats.bandwidth;
    }
    recordProxySnapshot(snapshot);
    cache = { data, expiresAt: Date.now() + CACHE_TTL_MS };
    return NextResponse.json(data);
  } catch (error) {
    const status = (error as Error & { statusCode?: number }).statusCode ?? 502;
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Proxy stats unavailable' },
      { status }
    );
  }
};

export const GET = withApiMetrics('proxy_stats', handler);
