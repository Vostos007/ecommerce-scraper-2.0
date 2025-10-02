import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { getProjectRoot, validatePath } from '../paths';

describe('validatePath', () => {
  const root = getProjectRoot();

  it('allows project root with trailing separator', () => {
    expect(validatePath(`${root}${path.sep}`)).toBe(true);
  });

  it('allows nested paths within project root', () => {
    expect(validatePath(path.join(root, 'apps', 'dashboard'))).toBe(true);
  });

  it('rejects paths attempting to escape via .. segments', () => {
    expect(validatePath(path.join(root, '..', '..', 'etc'))).toBe(false);
  });

  it('rejects absolute paths outside project root', () => {
    const outside = path.resolve(root, '..', '..', 'outside-directory');
    expect(validatePath(outside)).toBe(false);
  });

  it('rejects different drive letters on Windows', () => {
    if (process.platform !== 'win32') {
      return;
    }
    const parsed = path.parse(root);
    const currentDrive = parsed.root.slice(0, 2).toLowerCase();
    const alternateDrive = currentDrive === 'c:' ? 'd:' : 'c:';
    const otherDrivePath = `${alternateDrive}\\external`;
    expect(validatePath(otherDrivePath)).toBe(false);
  });
});
