import fs from 'node:fs';
import path from 'node:path';
import { beforeEach, describe, expect, test, vi } from 'vitest';

let sites: typeof import('../sites');
let sitesServer: typeof import('../sites.server');

beforeEach(async () => {
  vi.resetModules();
  sites = await import('../sites');
  sitesServer = await import('../sites.server');
});

describe('sites utilities', () => {
  test('sanitizeSite нормализует ввод', () => {
    expect(sites.sanitizeSite(' Www.Example.COM ')).toBe('example.com');
    expect(sites.sanitizeSite('../etc/passwd')).toBeNull();
  });

  test('getSiteSummaries кешируется и возвращает известные сайты', async () => {
    const first = await sitesServer.getSiteSummaries();
    const second = await sitesServer.getSiteSummaries();
    expect(first).toEqual(second);
    expect(first.find((item) => item.domain === 'atmospherestore.ru')).toBeDefined();
  });

  test('assertSiteAllowed возвращает скрипт', () => {
    const site = sitesServer.assertSiteAllowed('atmospherestore.ru');
    expect(site.script).toBe('atmosphere_fast_export');
  });

  test('assertSiteAllowed бросает ошибку для неизвестного сайта', () => {
    expect(() => sitesServer.assertSiteAllowed('unknown-site.test')).toThrow();
  });

  test('getSiteDirectory возвращает путь внутри data/sites', () => {
    const dir = sitesServer.getSiteDirectory('atmospherestore.ru');
    expect(dir).toContain(path.join('data', 'sites', 'atmospherestore.ru'));
  });

  test('getExportPath проверяет существование файла', () => {
    const realExists = fs.existsSync;
    const realStat = fs.statSync;
    const exportsSuffix = path.join('data', 'sites', 'atmospherestore.ru', 'exports', 'latest.xlsx');

    const existsSpy = vi.spyOn(fs, 'existsSync').mockImplementation((target: fs.PathLike) => {
      if (typeof target === 'string' && target.endsWith(exportsSuffix)) {
        return true;
      }
      return realExists(target);
    });

    const baseStats = realStat(__filename);
    const statSpy = vi.spyOn(fs, 'statSync').mockImplementation((target: fs.PathLike) => {
      if (typeof target === 'string' && target.endsWith(exportsSuffix)) {
        const proto = Object.getPrototypeOf(baseStats);
        return Object.assign(Object.create(proto), baseStats, {
          mtime: new Date('2025-09-27T00:00:00Z')
        });
      }
      return realStat(target);
    });

    const exportPath = sitesServer.getExportPath('atmospherestore.ru');
    expect(exportPath).toContain(exportsSuffix);

    existsSpy.mockRestore();
    statSpy.mockRestore();
  });

  test('getExportPath бросает ошибку, если файл отсутствует', () => {
    const existsSpy = vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    expect(() => sitesServer.getExportPath('atmospherestore.ru')).toThrow();
    existsSpy.mockRestore();
  });
});
