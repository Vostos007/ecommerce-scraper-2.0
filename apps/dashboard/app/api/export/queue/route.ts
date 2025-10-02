import { NextRequest, NextResponse } from 'next/server';

import { listQueuedExports } from '@/lib/processes';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: NextRequest) => {
  const queue = listQueuedExports();
  return NextResponse.json({ queue });
};

export const GET = withApiMetrics('export_queue_list', handler);
