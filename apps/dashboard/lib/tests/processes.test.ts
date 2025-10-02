import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

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

afterEach(() => {
  vi.resetModules();
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

describe('getPythonBinary resolution', () => {
  test('resolves command names via PATH when PYTHON_BIN is not absolute', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');

    const tmpDir = realFs.mkdtempSync(path.join(os.tmpdir(), 'python-mock-'));
    const commandName = process.platform === 'win32' ? 'python-mock.cmd' : 'python-mock';
    const commandPath = path.join(tmpDir, commandName);
    realFs.writeFileSync(commandPath, process.platform === 'win32' ? '@echo off\r\n' : '#!/bin/sh\nexit 0\n', {
      encoding: 'utf-8'
    });
    realFs.chmodSync(commandPath, 0o755);

    const originalPath = process.env.PATH ?? '';
    process.env.PATH = tmpDir;
    process.env.PYTHON_BIN = process.platform === 'win32' ? 'python-mock' : 'python-mock';

    vi.resetModules();
    const processesModule = await import('../processes');
    const resolved = processesModule.getPythonBinary();

    expect(resolved).toBe(commandPath);

    process.env.PATH = originalPath;
    delete process.env.PYTHON_BIN;
    realFs.unlinkSync(commandPath);
    realFs.rmSync(tmpDir, { recursive: true, force: true });
  });
});
