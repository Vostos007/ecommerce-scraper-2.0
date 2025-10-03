import { NextRequest } from 'next/server';

import { subscribeBulkRun, getBulkRunSnapshot } from '@/lib/bulk-runner';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const encoder = new TextEncoder();

function encodeEvent(event: string, payload: unknown): Uint8Array {
  return encoder.encode(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
}

const handler = async (request: NextRequest, context: { params: Promise<{ bulkId: string }> }) => {
  const { bulkId } = await context.params;
  const snapshot = getBulkRunSnapshot(bulkId);
  if (!snapshot) {
    return new Response('bulk-run not found', { status: 404 });
  }

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encodeEvent('snapshot', snapshot));

      const unsubscribe = subscribeBulkRun(
        bulkId,
        (updated) => {
          controller.enqueue(encodeEvent('snapshot', updated));
        },
        request.signal
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

export const GET = withApiMetrics('bulk_run_stream', handler);
