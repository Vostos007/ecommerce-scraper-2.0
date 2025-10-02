import '@testing-library/jest-dom/vitest';
import React from 'react';
import { cleanup, configure } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { act as reactDomAct } from 'react-dom/test-utils';
import { vi } from 'vitest';

// Fix React.act compatibility issue with React 19
if (!React.act) {
  React.act = reactDomAct;
}

if (!React.act) {
  React.act = (callback: () => void) => {
    callback();
    return Promise.resolve();
  };
}

configure({ testIdAttribute: 'data-testid' });

afterEach(() => {
  cleanup();
});

Object.assign(process.env, { NODE_ENV: 'test' });

if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn()
    }))
  });

  class ResizeObserverMock {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  }
  Object.defineProperty(window, 'ResizeObserver', {
    writable: true,
    value: ResizeObserverMock
  });

  Object.defineProperty(window, 'IntersectionObserver', {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      observe: vi.fn(),
      disconnect: vi.fn(),
      unobserve: vi.fn()
    }))
  });
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn()
  }),
  usePathname: () => '/dashboard',
  useSearchParams: () => new URLSearchParams()
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: ({ children, onClick, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) =>
    React.createElement(
      'a',
      {
        ...props,
        onClick: (event: React.MouseEvent<HTMLAnchorElement>) => {
          onClick?.(event);
          event.preventDefault();
        }
      },
      children
    )
}));

vi.mock('next/image', () => ({
  __esModule: true,
  default: (props: React.ImgHTMLAttributes<HTMLImageElement>) =>
    React.createElement('img', props)
}));

// Mock node:path for tests
vi.mock('node:path', () => {
  const mockPath = {
    resolve: (...paths: string[]) => paths.join('/'),
    join: (...paths: string[]) => paths.join('/'),
    dirname: (path: string) => path.split('/').slice(0, -1).join('/'),
    extname: (path: string) => {
      const lastDot = path.lastIndexOf('.');
      return lastDot === -1 ? '' : path.slice(lastDot);
    },
    basename: (path: string) => path.split('/').pop() || '',
    sep: '/',
    normalize: (path: string) => path.replace(/\/+/g, '/').replace(/\/$/, ''),
    isAbsolute: (path: string) => path.startsWith('/'),
    relative: (from: string, to: string) => {
      const fromNorm = from.replace(/\/+$/, '');
      const toNorm = to.replace(/\/+$/, '');
      if (fromNorm === toNorm) return '';
      if (toNorm.startsWith(fromNorm + '/')) return toNorm.slice(fromNorm.length + 1);
      return toNorm;
    },
    parse: (p: string) => {
      const parts = p.split('/');
      const base = parts[parts.length - 1] || '';
      const extIndex = base.lastIndexOf('.');
      return {
        root: parts[0] === '' ? '/' : '',
        dir: parts.slice(0, -1).join('/') || (parts[0] === '' ? '/' : ''),
        base,
        ext: extIndex > 0 ? base.slice(extIndex) : '',
        name: extIndex > 0 ? base.slice(0, extIndex) : base
      };
    }
  };
  return {
    default: mockPath,
    ...mockPath
  };
});

// Mock node:fs for tests
vi.mock('node:fs', () => {
  const mockFs = {
    existsSync: vi.fn(() => false),
    readdirSync: vi.fn(() => []),
    statSync: vi.fn(() => ({ isDirectory: () => false })),
    readFileSync: vi.fn((path: string) => {
      if (typeof path === 'string' && path.includes('sites.json')) {
        return '{"sites": [{"domain": "atmospherestore.ru", "name": "Atmosphere Store"}]}';
      }
      return '[]';
    }),
    mkdirSync: vi.fn(),
    accessSync: vi.fn(),
    constants: {
      X_OK: 1
    }
  };
  return {
    default: mockFs,
    ...mockFs
  };
});

export function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0
      }
    }
  });
}
