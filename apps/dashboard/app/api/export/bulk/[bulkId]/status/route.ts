import { NextResponse } from 'next/server';

import { getBulkRunSnapshot } from '@/lib/bulk-runner';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: Request, context: { params: Promise<{ bulkId: string }> }) => {
  const { bulkId } = await context.params;
  const snapshot = getBulkRunSnapshot(bulkId);
  if (!snapshot) {
    return NextResponse.json({ error: 'Массовый прогон не найден' }, { status: 404 });
  }
  return NextResponse.json(snapshot);
};

export const GET = withApiMetrics('bulk_run_status', handler);
