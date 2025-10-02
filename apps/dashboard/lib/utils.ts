type ClassInput = string | number | null | undefined | false;

export function cn(...classes: ClassInput[]): string {
  return classes.filter(Boolean).join(' ');
}

export function formatBytes(bytes: number, decimals = 2): string {
  if (!Number.isFinite(bytes)) {
    return '0 B';
  }

  const sign = bytes < 0 ? -1 : 1;
  let value = Math.abs(bytes);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  const precision = unitIndex === 0 ? 0 : Math.min(Math.max(decimals, 0), 6);
  const formatted = value.toFixed(precision);
  return `${sign < 0 ? '-' : ''}${formatted} ${units[unitIndex]}`;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

export function sanitizeString(input: string, maxLength = 255): string {
  const trimmed = input.trim();
  const sanitized = trimmed.replace(/[^a-zA-Z0-9_.-]+/g, '_');
  return sanitized.slice(0, maxLength) || '_';
}

export function generateId(): string {
  const cryptoRandomUUID = typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID.bind(globalThis.crypto)
    : null;
  if (cryptoRandomUUID) {
    return cryptoRandomUUID();
  }
  const now = Date.now().toString(36);
  const random = Math.random().toString(36).slice(2);
  return `${now}-${random}`;
}

export function debounce<T extends (...args: any[]) => any>(func: T, wait: number): T {
  let timeout: NodeJS.Timeout | null = null;
  return function debounced(this: ThisParameterType<T>, ...args: Parameters<T>) {
    if (timeout) {
      clearTimeout(timeout);
    }
    timeout = setTimeout(() => {
      timeout = null;
      func.apply(this, args);
    }, Math.max(0, wait));
  } as T;
}

export function throttle<T extends (...args: any[]) => any>(func: T, limit: number): T {
  let lastExecution = 0;
  let trailingTimeout: NodeJS.Timeout | null = null;

  return function throttled(this: ThisParameterType<T>, ...args: Parameters<T>) {
    const now = Date.now();
    const remaining = limit - (now - lastExecution);

    if (remaining <= 0) {
      if (trailingTimeout) {
        clearTimeout(trailingTimeout);
        trailingTimeout = null;
      }
      lastExecution = now;
      func.apply(this, args);
    } else if (!trailingTimeout) {
      trailingTimeout = setTimeout(() => {
        trailingTimeout = null;
        lastExecution = Date.now();
        func.apply(this, args);
      }, remaining);
    }
  } as T;
}

export function isValidUrl(value: string): boolean {
  if (!value) {
    return false;
  }
  try {
    if (value.startsWith('/')) {
      return true;
    }
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    try {
      new URL(value, 'http://localhost');
      return true;
    } catch {
      return false;
    }
  }
}

export function formatDateTime(date: string | Date | null | undefined, fallback = '—'): string {
  if (!date) {
    return fallback;
  }
  try {
    const d = typeof date === 'string' ? new Date(date) : date;
    if (Number.isNaN(d.getTime())) {
      return fallback;
    }
    return d.toLocaleString('ru-RU', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  } catch {
    return fallback;
  }
}
