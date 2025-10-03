import { createReadStream } from 'node:fs';
import { Readable } from 'node:stream';

import { NextResponse } from 'next/server';

import { ensureBulkRunArchive, getBulkRunSnapshot } from '@/lib/bulk-runner';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const handler = async (_request: Request, context: { params: Promise<{ bulkId: string }> }) => {
  const { bulkId } = await context.params;
  const snapshot = getBulkRunSnapshot(bulkId);
  if (!snapshot) {
    return NextResponse.json({ error: 'Массовый прогон не найден' }, { status: 404 });
  }
  if (snapshot.status !== 'completed') {
    return NextResponse.json({ error: 'Архив доступен только после завершения прогона' }, { status: 409 });
  }

  try {
    const { path: archivePath, size } = await ensureBulkRunArchive(bulkId);
    const filename = `bulk-run-${bulkId}.zip`;
    const stream = createReadStream(archivePath);
    const webStream = Readable.toWeb(stream) as unknown as ReadableStream<Uint8Array>;

    return new Response(webStream, {
      headers: {
        'Content-Type': 'application/zip',
        'Content-Length': size.toString(),
        'Content-Disposition': `attachment; filename="${filename}"`,
        'Cache-Control': 'no-cache'
      }
    });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : 'Не удалось подготовить архив' }, { status: 500 });
  }
};

export const GET = withApiMetrics('bulk_run_archive', handler);
