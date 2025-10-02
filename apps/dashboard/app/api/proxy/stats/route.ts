import { spawn } from 'node:child_process';
import { NextRequest, NextResponse } from 'next/server';

import { buildEnv, getPythonBinary } from '@/lib/processes';
import { resolveRepoPath } from '@/lib/paths';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import { proxyStatsSchema } from '@/lib/validations';
import { recordProxySnapshot, withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

interface CacheEntry {
  data: unknown;
  expiresAt: number;
}

const CACHE_TTL_MS = 30_000;
let cache: CacheEntry | null = null;
const limiter = new TokenBucketLimiter({ capacity: 5, windowMs: 60_000 });

function isCacheValid(): boolean {
  return Boolean(cache && cache.expiresAt > Date.now());
}

async function runProxyStatsScript(): Promise<unknown> {
  const python = getPythonBinary();
  const args = ['-u', '-m', 'scripts.proxy_stats_export'];

  return new Promise((resolve, reject) => {
    const child = spawn(python, args, {
      cwd: resolveRepoPath('.'),
      env: buildEnv(),
      shell: false
    });

    let stdout = '';
    let stderr = '';

    child.stdout.setEncoding('utf-8');
    child.stderr.setEncoding('utf-8');

    child.stdout.on('data', (chunk) => {
      stdout += chunk;
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });

    child.on('error', (error) => {
      reject(error);
    });

    child.on('close', (code) => {
      if (code !== 0) {
        const error = new Error(stderr || `proxy_stats_export exited with code ${code}`);
        (error as Error & { statusCode?: number }).statusCode = 502;
        reject(error);
        return;
      }
      try {
        const parsed = JSON.parse(stdout || '{}');
        resolve(parsed);
      } catch (error) {
        reject(error);
      }
    });
  });
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
      const cached = cache!.data as {
        total_proxies?: number;
        healthy_proxies?: number;
        active_proxies?: number;
        failed_proxies?: number;
        burned_proxies?: number;
        premium_proxy_stats?: { bandwidth?: number };
      };
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
      return NextResponse.json(cache!.data);
    }

    const rawData = await runProxyStatsScript();
    const parsed = proxyStatsSchema.parse(rawData);

    if (typeof parsed.success_rate === 'number' && parsed.success_rate <= 1) {
      parsed.success_rate = parsed.success_rate * 100;
    }

    if (parsed.premium_proxy_stats?.avg_success_rate !== undefined && parsed.premium_proxy_stats.avg_success_rate <= 1) {
      parsed.premium_proxy_stats.avg_success_rate = parsed.premium_proxy_stats.avg_success_rate * 100;
    }

    const data = parsed;
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
