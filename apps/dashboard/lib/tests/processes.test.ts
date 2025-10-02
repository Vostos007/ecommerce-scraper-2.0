import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
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

async function loadProcesses() {
  return await import('../processes');
}

beforeEach(() => {
  vi.resetModules();
  mockProcesses.length = 0;
  vi.clearAllMocks();
  process.env.PYTHON_BIN = 'python3';
  // Reset global active processes
  const globalStore = globalThis as any;
  globalStore.__dashboardActiveProcesses = undefined;
});

describe('processes spawnExport', () => {
  test('запускает процесс и регистрирует его', async () => {
    const { spawnExport, getProcess } = await loadProcesses();
    const result = spawnExport('atmospherestore.ru', { concurrency: 4, resume: true });
    expect(result.site).toBe('atmospherestore.ru');
    expect(result.script).toBeDefined();
    const record = getProcess(result.jobId);
    expect(record).not.toBeNull();
  });

  test('стримит stdout/stderr в подписчиков', async () => {
    const { spawnExport, subscribeToProcess, stopProcess } = await loadProcesses();
    const result = spawnExport('atmospherestore.ru');
    const childProcessModule = (await import('node:child_process')) as unknown as typeof import('node:child_process') & {
      __mockProcesses: FakeProcess[];
    };
    const proc = childProcessModule.__mockProcesses[0];

    const logs: Array<{ k: string; m: string }> = [];
    subscribeToProcess(result.jobId, {
      onLog(entry) {
        logs.push({ k: entry.k, m: entry.m.trim() });
      }
    });

    proc.stdout.write('hello world\n');
    proc.stderr.write('oops\n');

    expect(logs).toEqual(
      expect.arrayContaining([
        { k: 'out', m: 'hello world' },
        { k: 'err', m: 'oops' }
      ])
    );

    stopProcess(result.jobId);
  });

  test('ограничивает количество одновременных процессов', async () => {
    const { spawnExport } = await loadProcesses();
    spawnExport('atmospherestore.ru');
    spawnExport('atmospherestore.ru');
    spawnExport('atmospherestore.ru');
    spawnExport('atmospherestore.ru');
    expect(() => spawnExport('atmospherestore.ru')).toThrowError('Превышен лимит одновременных экспортов');
  });
});
