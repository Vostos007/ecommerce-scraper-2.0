import { NextResponse } from 'next/server';

import { getSiteSummaries } from '@/lib/sites.server';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async () => {
  const payload = await getSiteSummaries();
  return NextResponse.json(payload);
};

export const GET = withApiMetrics('sites_list', handler);
