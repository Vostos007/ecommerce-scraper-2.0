import { NextResponse } from 'next/server';
import { access, stat } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import os from 'node:os';
import { createClient } from 'redis';

import { withApiMetrics } from '@/lib/metrics';

interface HealthCache {
  timestamp: number;
  payload: Record<string, unknown>;
}

const CACHE_TTL_MS = 30_000;
let cache: HealthCache | null = null;

async function checkDatabase() {
  const databaseUrl = process.env.DATABASE_URL ?? '';
  if (!databaseUrl.startsWith('sqlite:')) {
    return { status: 'ok', message: 'No SQLite configured' };
  }
  const path = databaseUrl.replace('sqlite:', '').replace(/^\./, '/app');
  try {
    await access(path);
    return { status: 'ok', message: 'SQLite accessible' };
  } catch (error) {
    return { status: 'error', message: `SQLite inaccessible: ${(error as Error).message}` };
  }
}

async function checkPython() {
  const pythonBin = process.env.PYTHON_BIN ?? 'python3';
  return new Promise<{ status: string; version?: string; message?: string }>((resolve) => {
    const proc = spawn(pythonBin, ['--version']);
    let output = '';
    proc.stdout?.on('data', (chunk) => {
      output += chunk.toString();
    });
    proc.stderr?.on('data', (chunk) => {
      output += chunk.toString();
    });
    proc.on('error', (error) => {
      resolve({ status: 'error', message: error.message });
    });
    proc.on('close', (code) => {
      if (code === 0) {
        resolve({ status: 'ok', version: output.trim() });
      } else {
        resolve({ status: 'error', message: output.trim() });
      }
    });
  });
}

async function checkFilesystem() {
  try {
    const dataDir = '/app/data';
    await stat(dataDir);
    const freeSpaceGb = os.freemem() / 1024 / 1024 / 1024;
    const status = freeSpaceGb < 1 ? 'warning' : 'ok';
    return { status, freeSpaceGb: Number(freeSpaceGb.toFixed(2)) };
  } catch (error) {
    return { status: 'error', message: (error as Error).message };
  }
}

async function checkExternal() {
  const result: Record<string, unknown> = {};
  const flaresolverrUrl = process.env.FLARESOLVERR_URL;
  if (flaresolverrUrl) {
    try {
      const response = await fetch(flaresolverrUrl, { method: 'GET' });
      result.flaresolverr = response.ok;
    } catch (error) {
      result.flaresolverr = false;
      result.flaresolverrMessage = (error as Error).message;
    }
  }

  const redisUrl = process.env.REDIS_URL;
  if (redisUrl) {
    try {
      const client = createClient({ url: redisUrl, socket: { reconnectStrategy: () => false } });
      await client.connect();
      const pong = await client.ping();
      result.redis = pong === 'PONG';
      await client.disconnect();
    } catch (error) {
      result.redis = false;
      result.redisMessage = (error as Error).message;
    }
  }
  return result;
}

const handler = async () => {
  const now = Date.now();
  if (cache && now - cache.timestamp < CACHE_TTL_MS) {
    return NextResponse.json(cache.payload);
  }

  const [database, python, filesystem, external] = await Promise.all([
    checkDatabase(),
    checkPython(),
    checkFilesystem(),
    checkExternal()
  ]);

  const statuses = [database.status, python.status, filesystem.status];
  const hasError = statuses.includes('error');
  const hasWarning = statuses.includes('warning');

  const payload = {
    status: hasError ? 'unhealthy' : hasWarning ? 'degraded' : 'healthy',
    timestamp: new Date().toISOString(),
    uptime: process.uptime(),
    version: process.env.VERCEL_GIT_COMMIT_SHA ?? 'local',
    checks: {
      database,
      python,
      filesystem,
      external
    }
  };

  cache = { timestamp: now, payload };

  const statusCode = hasError ? 503 : 200;
  return NextResponse.json(payload, { status: statusCode });
};

export const GET = withApiMetrics('health', handler);
