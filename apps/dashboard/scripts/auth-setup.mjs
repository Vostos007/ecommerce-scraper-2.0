#!/usr/bin/env node
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import bcrypt from 'bcryptjs';

const ALLOWED_ROLES = new Set(['admin', 'operator', 'viewer']);

function parseArgs(argv) {
  const result = {};
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) {
      continue;
    }
    const [key, value] = arg.slice(2).split('=');
    if (value === undefined) {
      result[key] = argv[i + 1];
      i += 1;
    } else {
      result[key] = value;
    }
  }
  return result;
}

async function main() {
  const args = parseArgs(process.argv);
  const username = args.username ?? args.user;
  const password = args.password ?? args.pass;
  const role = (args.role ?? 'operator').toLowerCase();
  const userId = args.id ?? `${role}-${Date.now()}`;

  if (!username || !password) {
    console.error('Использование: pnpm auth:setup --username=<имя> --password=<пароль> [--role=admin|operator|viewer] [--id=<ид>]');
    process.exit(1);
  }

  if (!ALLOWED_ROLES.has(role)) {
    console.error(`Недопустимая роль: ${role}. Допустимые значения: admin, operator, viewer.`);
    process.exit(1);
  }

  const rounds = Number(process.env.BCRYPT_ROUNDS ?? 12);
  const passwordHash = await bcrypt.hash(password, rounds);
  const now = new Date().toISOString();

  const cwd = path.dirname(fileURLToPath(import.meta.url));
  const storePath = path.resolve(cwd, '../../../config/users.json');

  let store;
  try {
    const raw = await readFile(storePath, 'utf8');
    store = JSON.parse(raw);
  } catch (error) {
    if (error.code === 'ENOENT') {
      store = {
        users: [],
        version: '1.0',
        lastModified: now
      };
      await mkdir(path.dirname(storePath), { recursive: true });
    } else {
      throw error;
    }
  }

  const existingIndex = store.users.findIndex((user) => user.username.toLowerCase() === username.toLowerCase());
  const userRecord = {
    id: userId,
    username,
    passwordHash,
    role,
    createdAt: now,
    lastLogin: null,
    active: true
  };

  if (existingIndex >= 0) {
    store.users[existingIndex] = { ...store.users[existingIndex], ...userRecord };
    console.log(`Обновлена запись пользователя ${username}.`);
  } else {
    store.users.push(userRecord);
    console.log(`Добавлен новый пользователь ${username}.`);
  }

  store.lastModified = now;
  await writeFile(storePath, JSON.stringify(store, null, 2), 'utf8');
  console.log('Файл пользователей обновлён. Используйте роль и пароль осторожно.');
}

main().catch((error) => {
  console.error('Не удалось выполнить настройку пользователя:', error);
  process.exit(1);
});
