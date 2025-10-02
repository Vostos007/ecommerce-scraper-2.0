import { NextRequest, NextResponse } from 'next/server';

import { assertSiteAllowed, getActiveMapFile, getAvailableMapFiles, getCanonicalMapFileName } from '@/lib/sites.server';
import { sanitizeSite } from '@/lib/sites';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const limiter = new TokenBucketLimiter({ capacity: 10, windowMs: 60_000 });

function getClientIp(request: NextRequest): string {
  const forwarded = request.headers.get('x-forwarded-for');
  if (forwarded) {
    const candidate = forwarded.split(',')[0]?.trim();
    if (candidate) {
      return candidate;
    }
  }
  return request.headers.get('x-real-ip') ?? '127.0.0.1';
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

  try {
    const active = getActiveMapFile(sanitized);
    const available = getAvailableMapFiles(sanitized);
    const canonicalName = getCanonicalMapFileName(sanitized);

    const response = {
      site: sanitized,
      activeMap: active ? active.fileName : null,
      availableMaps: available.map((info) => ({
        filePath: info.filePath,
        fileName: info.fileName,
        size: info.metadata.size,
        modified: info.metadata.modified,
        linkCount: info.metadata.linkCount,
        isValid: info.metadata.isValid,
        isActive: active ? info.filePath === active.filePath : false,
        source: info.source,
        isCanonical: info.fileName === canonicalName
      }))
    };

    return NextResponse.json(response, {
      headers: {
        'Cache-Control': 'private, max-age=60'
      }
    });
  } catch (error) {
    console.error('[dashboard] failed to list map files', { error });
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Не удалось получить список карт' },
      { status: 500 }
    );
  }
};

export const GET = withApiMetrics('site_maps', handler);
