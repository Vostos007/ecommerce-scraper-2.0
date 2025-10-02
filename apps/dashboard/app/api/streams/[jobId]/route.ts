import { NextRequest } from 'next/server';

import { getProcess, subscribeToProcess } from '../../../../lib/processes';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const encoder = new TextEncoder();

function formatEvent(event: string, data: unknown) {
  return encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

const handler = async (request: NextRequest, context: { params: Promise<{ jobId: string }> }) => {
  const { jobId } = await context.params;
  const processRecord = getProcess(jobId);

  if (!processRecord) {
    return new Response('job not found', { status: 404 });
  }

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        formatEvent('init', {
          jobId,
          site: processRecord.site,
          script: processRecord.script,
          startedAt: processRecord.createdAt.toISOString()
        })
      );

      const unsubscribe = subscribeToProcess(
        jobId,
        {
          onLog(entry) {
            controller.enqueue(
              formatEvent(entry.k === 'err' ? 'stderr' : 'stdout', {
                message: entry.m,
                ts: entry.t
              })
            );
          },
          onClose(info) {
            controller.enqueue(
              formatEvent('end', {
                code: info.code,
                signal: info.signal,
                ts: Date.now()
              })
            );
            controller.close();
          }
        },
        { signal: request.signal }
      );

      const abortHandler = () => {
        unsubscribe();
        controller.close();
      };

      if (request.signal.aborted) {
        abortHandler();
      } else {
        request.signal.addEventListener('abort', abortHandler, { once: true });
      }
    },
    cancel() {
      // только закрываем поток; процесс завершается отдельно по своей логике
    }
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive'
    }
  });
};

export const GET = withApiMetrics('streams', handler);
