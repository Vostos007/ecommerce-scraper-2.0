import { spawn } from 'node:child_process';
import { createReadStream, promises as fs } from 'node:fs';
import { Readable } from 'node:stream';
import { NextRequest, NextResponse } from 'next/server';

import { getPythonBinary } from '@/lib/processes';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import { resolveRepoPath } from '@/lib/paths';
import { recordFileDownload, withApiMetrics } from '@/lib/metrics';

import {
  getGenerationPromise,
  getMasterState,
  getWorkbookPath,
  markError,
  markGenerating,
  markReady,
  setGenerationPromise,
  WORKBOOK_CACHE_TTL_MS
} from './state';

export const runtime = 'nodejs';

const WORKBOOK_TIMEOUT_MS = 10 * 60 * 1000;
const limiter = new TokenBucketLimiter({ capacity: 2, windowMs: 60_000 });
const WORKBOOK_PATH = getWorkbookPath();

async function getWorkbookStat() {
  try {
    return await fs.stat(WORKBOOK_PATH);
  } catch {
    return null;
  }
}

async function runWorkbookBuild(): Promise<void> {
  const python = getPythonBinary();
  await new Promise<void>((resolve, reject) => {
    const child = spawn(python, ['-u', '-m', 'build_master_workbook'], {
      cwd: resolveRepoPath('.'),
      stdio: ['ignore', 'inherit', 'inherit'],
      shell: false
    });

    let settled = false;
    const settle = (fn: () => void) => {
      if (settled) return;
      settled = true;
      fn();
    };

    const timeout = setTimeout(() => {
      if (!child.killed) {
        child.kill('SIGKILL');
      }
      const timeoutError = new Error('Время генерации сводного отчёта превышает 10 минут');
      (timeoutError as NodeJS.ErrnoException).code = 'WORKBOOK_TIMEOUT';
      settle(() => reject(timeoutError));
    }, WORKBOOK_TIMEOUT_MS);

    child.on('error', (error) => {
      clearTimeout(timeout);
      settle(() => reject(error));
    });

    child.on('exit', (code, signal) => {
      clearTimeout(timeout);
      if (code === 0) {
        settle(() => resolve());
      } else {
        const message =
          signal === 'SIGKILL'
            ? 'Процесс генерации мастер-отчёта был принудительно завершён'
            : `build_master_workbook завершился с кодом ${code ?? 'null'} (signal: ${signal ?? 'null'})`;
        const error = new Error(message);
        settle(() => reject(error));
      }
    });
  });
}

async function ensureWorkbook(): Promise<Awaited<ReturnType<typeof getWorkbookStat>>> {
  const stat = await getWorkbookStat();
  if (stat && Date.now() - stat.mtimeMs <= WORKBOOK_CACHE_TTL_MS) {
    if (getMasterState().status !== 'ready') {
      markReady(stat.size, stat.mtime.toISOString());
    }
    return stat;
  }

  let promise = getGenerationPromise();
  if (!promise) {
    markGenerating();
    promise = runWorkbookBuild()
      .then(async () => {
        const freshStat = await getWorkbookStat();
        if (!freshStat) {
          throw new Error('Файл history_wide.xlsx не найден после генерации');
        }
        markReady(freshStat.size, freshStat.mtime.toISOString());
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : 'Неизвестная ошибка генерации';
        markError(message);
        throw error;
      })
      .finally(() => {
        setGenerationPromise(null);
      });
    setGenerationPromise(promise);
  }

  try {
    await promise;
  } catch {
    return null;
  }

  return getWorkbookStat();
}

function rateLimit(request: NextRequest): boolean {
  const forwarded = request.headers.get('x-forwarded-for');
  const candidate = forwarded?.split(',')[0]?.trim();
  const ip = candidate && candidate.length > 0 ? candidate : request.headers.get('x-real-ip') ?? '127.0.0.1';
  return limiter.take(ip);
}

const handler = async (request: NextRequest) => {
  if (!rateLimit(request)) {
    return NextResponse.json({ error: 'Слишком много запросов, попробуйте позже' }, { status: 429 });
  }

  const stat = await ensureWorkbook();
  if (!stat) {
    const state = getMasterState();
    const message = state.errorMessage ?? 'Не удалось подготовить сводный отчёт';

    if (state.status === 'error') {
      if (state.errorMessage === 'Сводный отчет ещё не создавался') {
        return NextResponse.json({ error: message }, { status: 404 });
      }
      if (state.errorMessage?.includes('превышает 10 минут')) {
        return NextResponse.json({ error: message }, { status: 504 });
      }
      return NextResponse.json({ error: message }, { status: 502 });
    }

    return NextResponse.json({ error: message }, { status: 502 });
  }

  const stream = Readable.toWeb(createReadStream(WORKBOOK_PATH)) as ReadableStream<Uint8Array>;
  const timestamp = new Date(stat.mtimeMs).toISOString().replace(/[:.]/g, '-');
  recordFileDownload('master', stat.size, 'workbook');

  return new NextResponse(stream, {
    headers: {
      'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'Content-Length': stat.size.toString(),
      'Content-Disposition': `attachment; filename="master-workbook-${timestamp}.xlsx"`,
      'Cache-Control': 'no-store'
    }
  });
};

export const GET = withApiMetrics('download_master', handler);
