import { NextRequest, NextResponse } from 'next/server';

import { scheduleExport, findActiveJobForSite, type QueuedExport } from '@/lib/processes';
import { getSiteSummaries, assertSiteAllowed } from '@/lib/sites.server';
import { getSiteExportPreset } from '@/lib/export-presets';
import { withApiMetrics } from '@/lib/metrics';

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

  const jobs: Array<{ site: string; script: string; jobId: string; startedAt: string }> = [];
  const queued: Array<QueuedExport> = [];
  const skipped: Array<{ site: string; jobId: string; reason: string }> = [];
  const errors: Array<{ site: string; error: string }> = [];

  for (const domain of targets) {
    let siteInfo: ReturnType<typeof assertSiteAllowed>;
    try {
      siteInfo = assertSiteAllowed(domain);
    } catch (error) {
      errors.push({
        site: domain,
        error: error instanceof Error ? error.message : 'Site is not allowed'
      });
      continue;
    }

    const running = findActiveJobForSite(siteInfo.domain);
    if (running) {
      skipped.push({ site: siteInfo.domain, jobId: running.jobId, reason: 'already-running' });
      continue;
    }

    const preset = getSiteExportPreset(siteInfo.domain);
    const overrideConcurrency = payload?.concurrency?.[siteInfo.domain];
    const concurrency = (() => {
      const value = overrideConcurrency ?? preset.concurrency;
      if (!Number.isFinite(value)) {
        return preset.concurrency;
      }
      return Math.min(128, Math.max(1, Math.trunc(value)));
    })();

    try {
      const result = scheduleExport(siteInfo.domain, { concurrency, resume: resumeFlag });
      if (result.state === 'started') {
        jobs.push({
          site: siteInfo.domain,
          script: result.job.script,
          jobId: result.job.jobId,
          startedAt: result.job.startedAt
        });
      } else {
        queued.push(result.queued);
      }
    } catch (error) {
      errors.push({
        site: siteInfo.domain,
        error: error instanceof Error ? error.message : 'Не удалось запустить экспорт'
      });
    }
  }

  const ok = errors.length === 0;
  return NextResponse.json({ ok, jobs, queued, skipped, errors });
};

export const POST = withApiMetrics('export_bulk', handler);
