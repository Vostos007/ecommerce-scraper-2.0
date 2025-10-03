import { NextResponse } from 'next/server';

import { getLatestBulkRun } from '@/lib/bulk-runner';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async () => {
  const snapshot = getLatestBulkRun();
  if (!snapshot) {
    return NextResponse.json({ status: 'idle' as const }, { status: 404 });
  }

  return NextResponse.json(snapshot);
};

export const GET = withApiMetrics('bulk_run_latest', handler);
