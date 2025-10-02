import { promises as fs } from 'node:fs';
import { NextResponse } from 'next/server';

import {
  getMasterState,
  getWorkbookPath,
  WORKBOOK_CACHE_TTL_MS
} from '../state';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

async function refreshFromDisk() {
  const path = getWorkbookPath();
  try {
    const stat = await fs.stat(path);
    return {
      status: 'ready' as const,
      file_size: stat.size,
      generated_at: stat.mtime.toISOString(),
      error_message: null,
      stale: Date.now() - stat.mtimeMs > WORKBOOK_CACHE_TTL_MS
    };
  } catch {
    return null;
  }
}

const handler = async () => {
  const state = getMasterState();
  const diskInfo = await refreshFromDisk();

  if (diskInfo) {
    const generating = state.status === 'generating';
    return NextResponse.json({
      status: generating ? 'generating' : 'ready',
      file_size: diskInfo.file_size,
      generated_at: diskInfo.generated_at,
      error_message: generating ? state.errorMessage ?? undefined : undefined,
      stale: diskInfo.stale,
      phase: state.phase ?? undefined
    });
  }

  return NextResponse.json({
    status: state.status,
    file_size: state.fileSize ?? undefined,
    generated_at: state.generatedAt ?? undefined,
    error_message: state.errorMessage ?? undefined,
    stale: true,
    phase: state.phase ?? undefined
  });
};

export const GET = withApiMetrics('download_master_status', handler);
