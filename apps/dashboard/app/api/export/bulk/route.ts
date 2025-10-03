import { NextRequest, NextResponse } from 'next/server';

import { getSiteSummaries } from '@/lib/sites.server';
import { withApiMetrics } from '@/lib/metrics';
import { startBulkRun, getBulkRunSnapshot, getActiveBulkRunId } from '@/lib/bulk-runner';

interface BulkExportRequest {
  sites?: string[];
  resume?: boolean;
  concurrency?: Record<string, number>;
}

export const runtime = 'nodejs';

function normalizeSites(input?: string[]): string[] {
  if (!Array.isArray(input) || input.length === 0) {
    return [];
  }
  const unique = new Set<string>();
  for (const candidate of input) {
    if (typeof candidate === 'string' && candidate.trim()) {
      unique.add(candidate.trim().toLowerCase());
    }
  }
  return Array.from(unique);
}

const handler = async (request: NextRequest) => {
  let payload: BulkExportRequest | undefined;
  const contentLength = request.headers.get('content-length');
  const hasBody = contentLength !== null && Number(contentLength) > 0;
  if (hasBody) {
    try {
      payload = (await request.json()) as BulkExportRequest;
    } catch {
      return NextResponse.json({ error: 'Некорректный JSON в запросе' }, { status: 400 });
    }
  }

  const resumeFlag = payload?.resume ?? true;
  const requestedSites = normalizeSites(payload?.sites);

  const availableSites = getSiteSummaries().map((summary) => summary.domain);
  const targets = (requestedSites.length > 0 ? requestedSites : availableSites).filter((domain, index, arr) => {
    return arr.indexOf(domain) === index;
  });

  if (targets.length === 0) {
    return NextResponse.json({ error: 'Нет доступных площадок для запуска' }, { status: 404 });
  }

  try {
    const result = startBulkRun({
      sites: targets,
      resume: resumeFlag,
      concurrencyOverrides: payload?.concurrency ?? {}
    });

    return NextResponse.json({
      ok: true,
      runId: result.id,
      snapshot: result.snapshot
    });
  } catch (error) {
    const activeId = getActiveBulkRunId();
    if (activeId) {
      const snapshot = getBulkRunSnapshot(activeId);
      return NextResponse.json(
        {
          ok: false,
          error: error instanceof Error ? error.message : 'Массовый прогон уже выполняется',
          activeRun: snapshot
        },
        { status: 409 }
      );
    }

    return NextResponse.json(
      { ok: false, error: error instanceof Error ? error.message : 'Не удалось запустить массовый прогон' },
      { status: 500 }
    );
  }
};

export const POST = withApiMetrics('export_bulk', handler);
