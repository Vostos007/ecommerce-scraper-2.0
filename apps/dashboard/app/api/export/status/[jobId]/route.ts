import { NextResponse } from 'next/server';

import { getProcess } from '@/lib/processes';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: Request, context: { params: Promise<{ jobId: string }> }) => {
  const params = await context.params;
  const jobId = params.jobId;
  const record = getProcess(jobId);

  if (!record) {
    return NextResponse.json(
      {
        jobId,
        status: 'unknown' as const
      },
      { status: 404 }
    );
  }

  const lastLog = record.logs.at(-1);

  return NextResponse.json({
    jobId: record.jobId,
    site: record.site,
    script: record.script,
    status: record.status,
    startedAt: record.createdAt.toISOString(),
    exitCode: record.exitCode,
    exitSignal: record.exitSignal,
    lastEventAt: lastLog ? new Date(lastLog.t).toISOString() : null,
    progressPercent: record.progressPercent,
    processedUrls: record.processedUrls,
    totalUrls: record.totalUrls,
    successUrls: record.successUrls,
    failedUrls: record.failedUrls,
    estimatedSecondsRemaining: record.estimatedSecondsRemaining
  });
};

export const GET = withApiMetrics('export_status', handler);
