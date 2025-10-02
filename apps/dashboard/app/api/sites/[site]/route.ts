import { NextRequest, NextResponse } from 'next/server';

import { sanitizeSite } from '@/lib/sites';
import { getSiteSummaries } from '@/lib/sites.server';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: NextRequest, context: { params: Promise<{ site: string }> }) => {
  const { site } = await context.params;
  const sanitized = sanitizeSite(site);
  if (!sanitized) {
    return NextResponse.json({ error: 'Invalid site parameter' }, { status: 400 });
  }

  const summaries = await getSiteSummaries();
  const summary = summaries.find((item) => item.domain === sanitized);
  if (!summary) {
    return NextResponse.json({ error: 'Site not found' }, { status: 404 });
  }

  return NextResponse.json(summary);
};

export const GET = withApiMetrics('site_detail', handler);
