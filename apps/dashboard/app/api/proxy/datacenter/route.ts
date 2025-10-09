import { NextRequest, NextResponse } from 'next/server';

import {
  addDatacenterProxies,
  listDatacenterProxies,
  removeDatacenterProxies
} from '@/lib/proxy-admin';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import {
  proxyAddRequestSchema,
  proxyAddResponseSchema,
  proxyDeleteRequestSchema,
  proxyDeleteResponseSchema,
  proxyListSchema
} from '@/lib/validations';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const limiter = new TokenBucketLimiter({ capacity: 20, windowMs: 60_000 });

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

async function ensureQuota(request: NextRequest): Promise<NextResponse | null> {
  const ip = getClientIp(request);
  if (!limiter.take(ip)) {
    return NextResponse.json({ error: 'Слишком много запросов, попробуйте позже' }, { status: 429 });
  }
  return null;
}

async function handleGet() {
  const { proxies, total } = await listDatacenterProxies();
  const payload = {
    proxies,
    total,
    updatedAt: new Date().toISOString()
  };
  // validate before returning to catch serialization regressions
  const validated = proxyListSchema.parse(payload);
  return NextResponse.json(validated);
}

async function handlePost(request: NextRequest) {
  let json: unknown;
  try {
    json = await request.json();
  } catch {
    return NextResponse.json({ error: 'Некорректный JSON' }, { status: 400 });
  }

  const parsed = proxyAddRequestSchema.safeParse(json);
  if (!parsed.success) {
    const issue = parsed.error.issues.at(0);
    return NextResponse.json({ error: issue?.message ?? 'Некорректные данные' }, { status: 400 });
  }

  try {
    const result = await addDatacenterProxies(parsed.data.proxies, parsed.data.replace ?? false);
    const payload = proxyAddResponseSchema.parse(result);
    return NextResponse.json(payload);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Не удалось обновить прокси' },
      { status: 422 }
    );
  }
}

async function handleDelete(request: NextRequest) {
  let json: unknown;
  try {
    json = await request.json();
  } catch {
    return NextResponse.json({ error: 'Некорректный JSON' }, { status: 400 });
  }

  const parsed = proxyDeleteRequestSchema.safeParse(json);
  if (!parsed.success) {
    const issue = parsed.error.issues.at(0);
    return NextResponse.json({ error: issue?.message ?? 'Некорректные данные' }, { status: 400 });
  }

  try {
    const result = await removeDatacenterProxies(parsed.data.proxies);
    const payload = proxyDeleteResponseSchema.parse(result);
    return NextResponse.json(payload);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Не удалось удалить прокси' },
      { status: 422 }
    );
  }
}

export const GET = withApiMetrics('proxy_datacenter_get', async (request: NextRequest) => {
  const limit = await ensureQuota(request);
  if (limit) {
    return limit;
  }
  return handleGet();
});

export const POST = withApiMetrics('proxy_datacenter_post', async (request: NextRequest) => {
  const limit = await ensureQuota(request);
  if (limit) {
    return limit;
  }
  return handlePost(request);
});

export const DELETE = withApiMetrics('proxy_datacenter_delete', async (request: NextRequest) => {
  const limit = await ensureQuota(request);
  if (limit) {
    return limit;
  }
  return handleDelete(request);
});
