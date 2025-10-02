import { NextResponse } from 'next/server';

import { findActiveJobForSite } from '@/lib/processes';
import { assertSiteAllowed } from '@/lib/sites.server';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: Request, context: { params: Promise<{ site: string }> }) => {
  const { site } = await context.params;

  let siteInfo: ReturnType<typeof assertSiteAllowed>;
  try {
    siteInfo = assertSiteAllowed(site);
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : 'Site not allowed' }, { status: 404 });
  }

  const record = findActiveJobForSite(siteInfo.domain);
  if (!record) {
    return NextResponse.json({ error: 'Active job not found' }, { status: 404 });
  }

  return NextResponse.json({
    jobId: record.jobId,
    site: record.site,
    script: record.script,
    python: record.python,
    args: record.args,
    startedAt: record.createdAt.toISOString(),
    status: record.status,
    progress: {
      totalUrls: record.totalUrls,
      processedUrls: record.processedUrls,
      successUrls: record.successUrls,
      failedUrls: record.failedUrls,
      progressPercent: record.progressPercent
    }
  });
};

export const GET = withApiMetrics('export_active', handler);
