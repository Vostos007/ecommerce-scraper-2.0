import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { TextDecoder } from 'node:util';
import { NextRequest } from 'next/server';
import { beforeEach, describe, expect, test, vi } from 'vitest';

class FakeProcess extends EventEmitter {
  public readonly stdout = new PassThrough();
  public readonly stderr = new PassThrough();
  public killed = false;
  public kill = vi.fn((signal: NodeJS.Signals = 'SIGTERM') => {
    this.killed = true;
    this.emit('exit', 0, signal);
    return true;
  });
}

const mockProcesses: FakeProcess[] = [];

vi.mock('node:child_process', () => {
  const spawnMock = vi.fn(() => {
    const proc = new FakeProcess();
    mockProcesses.push(proc);
    return proc;
  });

  return {
    __esModule: true,
    default: { spawn: spawnMock },
    spawn: spawnMock,
    __mockProcesses: mockProcesses
  };
});

const decoder = new TextDecoder();

async function loadHandlers() {
  const processes = await import('../lib/processes');
  const streamsRoute = await import('../app/api/streams/[jobId]/route');
  return { processes, streamsRoute };
}

beforeEach(() => {
  vi.resetModules();
  mockProcesses.length = 0;
  vi.clearAllMocks();
  process.env.PYTHON_BIN = 'python3';
});

describe('SSE stream harness', () => {
  test('возвращает события init/stdout/end', async () => {
    const { processes, streamsRoute } = await loadHandlers();
    const result = await processes.spawnExport('atmospherestore.ru');
    const childProcessModule = (await import('node:child_process')) as unknown as typeof import('node:child_process') & {
      __mockProcesses: FakeProcess[];
    };
    const proc = childProcessModule.__mockProcesses[0];

    const controller = new AbortController();
    const request = new NextRequest(`http://localhost/api/streams/${result.jobId}`, {
      method: 'GET'
    });
    Object.defineProperty(request, 'signal', {
      value: controller.signal
    });

    const response = await streamsRoute.GET(request, {
      params: Promise.resolve({ jobId: result.jobId })
    });
    expect(response.headers.get('Content-Type')).toBe('text/event-stream');
    const reader = response.body!.getReader();

    const firstChunk = await reader.read();
    expect(firstChunk.done).toBe(false);
    expect(decoder.decode(firstChunk.value)).toContain('event: init');

    const logs: string[] = [];
    proc.stdout.write('hello\n');
    const stdoutChunk = await reader.read();
    logs.push(decoder.decode(stdoutChunk.value));
    expect(logs.join('')).toContain('event: stdout');

    proc.kill('SIGTERM');
    const endChunk = await reader.read();
    expect(decoder.decode(endChunk.value)).toContain('event: end');

    controller.abort();
  });
});
