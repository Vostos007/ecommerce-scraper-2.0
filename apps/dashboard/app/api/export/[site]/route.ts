import { NextRequest, NextResponse } from 'next/server';

import { ZodError } from 'zod';

import { spawnExport, type SpawnOptions } from '@/lib/processes';
import { assertSiteAllowed } from '@/lib/sites.server';
import { sanitizeSite } from '@/lib/sites';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import { exportConfigSchema } from '@/lib/validations';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const limiter = new TokenBucketLimiter({ capacity: 5, windowMs: 60_000 });

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

const handler = async (request: NextRequest, context: { params: Promise<{ site: string }> }) => {
  const ip = getClientIp(request);
  if (!limiter.take(ip)) {
    return NextResponse.json({ error: 'Слишком много запросов, попробуйте позже' }, { status: 429 });
  }

  const params = await context.params;
  const sanitized = sanitizeSite(params.site);
  if (!sanitized) {
    return NextResponse.json({ error: 'Некорректный параметр site' }, { status: 400 });
  }

  try {
    assertSiteAllowed(sanitized);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Сайт не поддерживается' },
      { status: 404 }
    );
  }

  let payload: Record<string, unknown> = {};
  try {
    const contentType = request.headers.get('content-type') ?? '';
    if (contentType.includes('application/json')) {
      const raw = await request.text();
      if (raw.trim()) {
        payload = JSON.parse(raw) as Record<string, unknown>;
      }
    }
  } catch {
    return NextResponse.json({ error: 'Некорректный JSON' }, { status: 400 });
  }

  let config: {
    concurrency?: number | undefined;
    resume?: boolean | undefined;
    limit?: number | undefined;
    args?: string[] | undefined;
  };
  try {
    config = exportConfigSchema.parse(payload);
  } catch (error) {
    if (error instanceof ZodError) {
      const issue = error.issues.at(0);
      return NextResponse.json(
        { error: issue?.message ?? 'Некорректные параметры запуска' },
        { status: 422 }
      );
    }
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Некорректные параметры запуска' },
      { status: 422 }
    );
  }

  try {
    const spawnOptions: SpawnOptions = {};
    if (config.args) {
      spawnOptions.extraArgs = config.args;
    }
    if (typeof config.concurrency === 'number') {
      spawnOptions.concurrency = config.concurrency;
    }
    if (typeof config.resume === 'boolean') {
      spawnOptions.resume = config.resume;
    }
    if (typeof config.limit === 'number') {
      spawnOptions.limit = config.limit;
    }

    const result = spawnExport(sanitized, spawnOptions);

    return NextResponse.json({
      jobId: result.jobId,
      site: result.site,
      script: result.script,
      python: result.python,
      args: result.args,
      startedAt: result.startedAt
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Не удалось запустить экспорт' },
      { status: 500 }
    );
  }
};

export const POST = withApiMetrics('export_site_post', handler);
