import fs from 'node:fs';
import fsPromises from 'node:fs/promises';
import path from 'node:path';

let cachedRoot: string | null = null;
let warnedFallback = false;

function detectProjectRoot(): string {
  if (cachedRoot) {
    return cachedRoot;
  }

  // In test environment, path.resolve might not be available
  if (typeof path.resolve !== 'function') {
    cachedRoot = process.cwd();
    return cachedRoot;
  }

  const start = path.resolve(__dirname, '..', '..', '..');
  let current = start;

  while (true) {
    const marker = path.join(current, 'pyproject.toml');
    if (fs.existsSync(marker)) {
      cachedRoot = current;
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) {
      break;
    }
    current = parent;
  }

  const fallback = process.cwd();
  if (!warnedFallback) {
    console.warn('[dashboard] pyproject.toml not found; falling back to process.cwd()', {
      cwd: fallback
    });
    warnedFallback = true;
  }
  cachedRoot = fallback;
  return fallback;
}

export function getProjectRoot(): string {
  return detectProjectRoot();
}

export function validatePath(targetPath: string): boolean {
  if (!targetPath || targetPath.includes('\0')) {
    return false;
  }

  const root = path.normalize(path.resolve(getProjectRoot()));
  const absoluteTarget = path.isAbsolute(targetPath)
    ? path.normalize(targetPath)
    : path.normalize(path.resolve(root, targetPath));

  const relative = path.relative(root, absoluteTarget);
  if (!relative) {
    return true;
  }

  return !relative.startsWith('..') && !path.isAbsolute(relative);
}

export function resolveRepoPath(...segments: string[]): string {
  const root = getProjectRoot();
  const candidate = path.normalize(path.resolve(root, ...segments));
  if (!validatePath(candidate)) {
    throw new Error(`Path ${candidate} выходит за границы репозитория`);
  }
  return candidate;
}

export async function ensureDirectoryExists(dirPath: string): Promise<void> {
  const absolutePath = path.isAbsolute(dirPath) ? path.normalize(dirPath) : resolveRepoPath(dirPath);
  if (!validatePath(absolutePath)) {
    throw new Error(`Невозможен доступ к директории ${absolutePath}`);
  }

  await fsPromises.mkdir(absolutePath, { recursive: true });
  const stat = await fsPromises.stat(absolutePath);
  if (!stat.isDirectory()) {
    throw new Error(`Ожидалась директория, но найден другой тип узла: ${absolutePath}`);
  }
}
