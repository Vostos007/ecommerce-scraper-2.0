#!/usr/bin/env node

import net from 'node:net';
import { spawn } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, copyFileSync } from 'node:fs';
import { resolve } from 'node:path';
import process from 'node:process';

const projectRoot = resolve(process.cwd(), '..', '..');
const logDir = resolve(projectRoot, 'logs');
const logFile = resolve(logDir, 'dashboard-bootstrap.log');
const dataDir = resolve(projectRoot, 'data');
const authDbFile = resolve(dataDir, 'auth.db');

if (!existsSync(logDir)) {
  mkdirSync(logDir, { recursive: true });
}

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  appendFileSync(logFile, `${line}\n`);
  console.log(line);
}

function execCommand(command, args, options = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, { stdio: 'inherit', ...options });
    child.on('error', (error) => {
      log(`Команда ${command} завершилась ошибкой: ${error.message}`);
      rejectPromise(error);
    });
    child.on('exit', (code) => {
      if (code === 0) {
        resolvePromise();
      } else {
        const error = new Error(`Команда ${command} завершилась с кодом ${code}`);
        log(error.message);
        rejectPromise(error);
      }
    });
  });
}

async function detectPythonBinary() {
  if (process.env.PYTHON_BIN) {
    log(`Используем PYTHON_BIN из окружения: ${process.env.PYTHON_BIN}`);
    return process.env.PYTHON_BIN;
  }

  const candidates = ['python3', 'python'];
  for (const candidate of candidates) {
    try {
      await execCommand(candidate, ['--version'], { stdio: 'ignore' });
      log(`Обнаружен Python: ${candidate}`);
      return candidate;
    } catch (error) {
      // продолжаем искать
    }
  }

  throw new Error('Python 3.11+ не найден. Установите python3 или задайте PYTHON_BIN');
}

async function ensureEnvFile() {
  const envFile = resolve(process.cwd(), '.env');
  const envExample = resolve(process.cwd(), '.env.example');
  if (!existsSync(envFile) && existsSync(envExample)) {
    copyFileSync(envExample, envFile);
    log('Создан apps/dashboard/.env из .env.example');
  }
}

function ensureDevDirs() {
  if (!existsSync(dataDir)) {
    mkdirSync(dataDir, { recursive: true });
    log('Создана директория data для dashboard dev');
  }
  if (!existsSync(authDbFile)) {
    appendFileSync(authDbFile, '');
    log('Создан пустой data/auth.db для health-check');
  }
}

async function ensureDependencies() {
  if (process.env.SKIP_DASHBOARD_INSTALL === '1') {
    log('Пропускаем pnpm install (SKIP_DASHBOARD_INSTALL=1)');
    return;
  }
  log('Запускаем pnpm install...');
  await execCommand('pnpm', ['install', '--no-frozen-lockfile']);
}

function ensurePortAvailable(port) {
  return new Promise((resolvePromise, rejectPromise) => {
    const tester = net.createServer();
    tester.once('error', (error) => {
      tester.close();
      if (error.code === 'EADDRINUSE') {
        rejectPromise(new Error(`Порт ${port} уже используется. Завершите процесс или выберите другой порт.`));
      } else {
        rejectPromise(error);
      }
    });
    tester.once('listening', () => {
      tester.close(() => resolvePromise());
    });
    tester.listen(port, '0.0.0.0');
  });
}

let nextDevProcess = null;
let terminatingBySignal = false;
let signalsAttached = false;

function cleanupChild() {
  if (nextDevProcess && !nextDevProcess.killed) {
    nextDevProcess.kill('SIGTERM');
  }
}

function attachSignalHandlers() {
  if (signalsAttached) {
    return;
  }
  const handle = (signal, exitCode) => {
    return () => {
      if (!terminatingBySignal) {
        terminatingBySignal = true;
        log(`Получен сигнал ${signal}, останавливаем next dev`);
        cleanupChild();
        process.exitCode = exitCode;
      }
    };
  };
  process.once('SIGINT', handle('SIGINT', 130));
  process.once('SIGTERM', handle('SIGTERM', 143));
  process.once('exit', cleanupChild);
  signalsAttached = true;
}

async function runNextDev(port, pythonBin) {
  log(`Старт dev сервера: порт ${port}, PYTHON_BIN=${pythonBin}`);
  const env = {
    ...process.env,
    PORT: String(port),
    PYTHON_BIN: pythonBin,
    NODE_OPTIONS: process.env.NODE_OPTIONS ?? '--max-old-space-size=4096'
  };

  if (!env.NEXT_PUBLIC_APP_URL) {
    env.NEXT_PUBLIC_APP_URL = `http://localhost:${port}`;
  }
  if (!env.NEXT_PUBLIC_DASHBOARD_ORIGIN) {
    env.NEXT_PUBLIC_DASHBOARD_ORIGIN = env.NEXT_PUBLIC_APP_URL;
  }
  if (!env.DASHBOARD_ORIGIN) {
    env.DASHBOARD_ORIGIN = env.NEXT_PUBLIC_DASHBOARD_ORIGIN;
  }

  terminatingBySignal = false;
  attachSignalHandlers();

  await new Promise((resolvePromise, rejectPromise) => {
    nextDevProcess = spawn('next', ['dev', '--hostname', '0.0.0.0', '--port', String(port)], {
      env,
      stdio: 'inherit'
    });

    nextDevProcess.on('error', (error) => {
      if (terminatingBySignal) {
        resolvePromise();
        return;
      }
      log(`Ошибка запуска next dev: ${error.message}`);
      rejectPromise(error);
    });

    nextDevProcess.on('exit', (code, signal) => {
      nextDevProcess = null;
      if (terminatingBySignal) {
        resolvePromise();
        return;
      }
      if (signal) {
        const error = new Error(`next dev завершился сигналом ${signal}`);
        log(error.message);
        rejectPromise(error);
        return;
      }
      if (code === 0) {
        resolvePromise();
      } else {
        const error = new Error(`next dev завершился с кодом ${code}`);
        log(error.message);
        rejectPromise(error);
      }
    });
  });
}

async function main() {
  try {
    const pythonBin = await detectPythonBinary();
    await ensureEnvFile();
    ensureDevDirs();
    await ensureDependencies();

    const portValue = process.env.PORT ?? '3050';
    const port = Number.parseInt(portValue, 10);
    if (Number.isNaN(port)) {
      throw new Error(`Некорректное значение порта: ${portValue}`);
    }

    await ensurePortAvailable(port);

    const mode = process.env.DASHBOARD_BOOTSTRAP_MODE ?? 'serve';
    if (mode === 'check') {
      log('DASHBOARD_BOOTSTRAP_MODE=check — запуск сервера пропущен');
      return;
    }

    await runNextDev(port, pythonBin);
  } catch (error) {
    log(`Ошибка bootstrap: ${error.message}`);
    if (!process.exitCode) {
      process.exitCode = 1;
    }
  }
}

main();
