import { NextRequest, NextResponse } from 'next/server';

import { cancelQueuedExport } from '@/lib/processes';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: NextRequest, context: { params: { queueId?: string } }) => {
  const queueId = context.params.queueId;
  if (!queueId) {
    return NextResponse.json({ error: 'queueId не указан' }, { status: 400 });
  }

  const ok = cancelQueuedExport(queueId);
  if (!ok) {
    return NextResponse.json({ error: 'Элемент очереди не найден' }, { status: 404 });
  }

  return new NextResponse(null, { status: 204 });
};

export const DELETE = withApiMetrics('export_queue_delete', handler);
