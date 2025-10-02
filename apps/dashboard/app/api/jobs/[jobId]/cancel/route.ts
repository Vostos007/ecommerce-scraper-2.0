import { NextResponse } from 'next/server';

import { getProcess, stopProcess } from '@/lib/processes';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: Request, context: { params: Promise<{ jobId: string }> }) => {
  const params = await context.params;
  const jobId = params.jobId;
  const record = getProcess(jobId);

  if (!record) {
    return NextResponse.json({ error: 'Job not found' }, { status: 404 });
  }

  if (record.status !== 'running') {
    return NextResponse.json(
      { error: `Job is already ${record.status}`, jobId, status: record.status },
      { status: 409 }
    );
  }

  const killed = stopProcess(jobId);
  if (!killed) {
    return NextResponse.json({ error: 'Не удалось отправить сигнал остановки', jobId }, { status: 500 });
  }

  return NextResponse.json({ ok: true, jobId, status: 'cancelled' as const });
};

export const POST = withApiMetrics('export_cancel', handler);
