import { SignJWT, jwtVerify } from 'jose';
import path from 'node:path';
import { promisify } from 'node:util';
import bcrypt from 'bcryptjs';
import type { JWTPayload } from 'jose';
import { randomBytes, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto';
import * as fs from 'node:fs/promises';

import type { Role } from './permissions';
import { userSchema } from './validations';

const scrypt = promisify(scryptCallback);

export const AUTH_COOKIE_NAME = 'auth-token';
const DEFAULT_JWT_ISSUER = 'scraper-dashboard';
const DEFAULT_JWT_AUDIENCE = 'dashboard-users';
const ROLE_TTL_SECONDS: Record<Role, number> = {
  admin: 8 * 60 * 60,
  operator: 4 * 60 * 60,
  viewer: 4 * 60 * 60
};

export interface User {
  id: string;
  username: string;
  passwordHash: string;
  role: Role;
  createdAt: string;
  lastLogin: string | null;
  active: boolean;
}

interface UserStore {
  users: User[];
  version: string;
  lastModified: string;
}

interface AuthConfig {
  secret: Uint8Array;
  issuer: string;
  audience: string;
}

const STORE_LOCK = createLock();

function shouldUseSecureCookie(): boolean {
  if (process.env.SECURE_COOKIES === 'true') {
    return true;
  }
  if (process.env.SECURE_COOKIES === 'false') {
    return false;
  }

  const appUrl = process.env.NEXT_PUBLIC_APP_URL ?? '';
  const isProduction = process.env.NODE_ENV === 'production';
  const isHttps = appUrl.toLowerCase().startsWith('https://');
  return isProduction || isHttps;
}

function createLock() {
  let mutex = Promise.resolve();
  return {
    async runExclusive<T>(callback: () => Promise<T>): Promise<T> {
      const run = mutex.then(callback, callback);
      mutex = run.then(() => undefined, () => undefined);
      return run;
    }
  };
}

function resolveUserStorePath(): string {
  const target = process.env.USER_STORE_PATH
    ? path.resolve(process.env.USER_STORE_PATH)
    : path.resolve(process.cwd(), '..', '..', 'config', 'users.json');
  return target;
}

async function ensureStore(): Promise<void> {
  const storePath = resolveUserStorePath();
  try {
    await fs.access(storePath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      const emptyStore: UserStore = {
        users: [],
        version: '1.0',
        lastModified: new Date().toISOString()
      };
      await fs.mkdir(path.dirname(storePath), { recursive: true });
      await fs.writeFile(storePath, JSON.stringify(emptyStore, null, 2), 'utf8');
      return;
    }
    throw error;
  }
}

async function readUserStore(): Promise<UserStore> {
  await ensureStore();
  const raw = await fs.readFile(resolveUserStorePath(), 'utf8');
  try {
    const parsed = JSON.parse(raw) as UserStore;
    parsed.users = Array.isArray(parsed.users)
      ? parsed.users.map((user) => {
          const parsedUser = userSchema.parse({ ...user, active: user.active ?? true });
          return { ...parsedUser, lastLogin: parsedUser.lastLogin ?? null };
        })
      : [];
    return parsed;
  } catch (error) {
    throw new Error(`Некорректный формат файла пользователей: ${(error as Error).message}`);
  }
}

async function writeUserStore(store: UserStore): Promise<void> {
  const nextStore: UserStore = {
    ...store,
    lastModified: new Date().toISOString()
  };
  await fs.mkdir(path.dirname(resolveUserStorePath()), { recursive: true });
  await fs.writeFile(resolveUserStorePath(), JSON.stringify(nextStore, null, 2), 'utf8');
}

export async function getUserByUsername(username: string): Promise<User | null> {
  const store = await readUserStore();
  const normalized = username.trim().toLowerCase();
  return store.users.find((user) => user.username.toLowerCase() === normalized && user.active) ?? null;
}

export async function getUserById(userId: string): Promise<User | null> {
  const store = await readUserStore();
  return store.users.find((user) => user.id === userId && user.active) ?? null;
}

export async function updateLastLogin(userId: string): Promise<void> {
  await STORE_LOCK.runExclusive(async () => {
    const store = await readUserStore();
    const target = store.users.find((user) => user.id === userId);
    if (!target) {
      return;
    }
    target.lastLogin = new Date().toISOString();
    await writeUserStore(store);
  });
}

function getAuthConfig(): AuthConfig {
  const secretValue = process.env.JWT_SECRET;
  if (!secretValue || secretValue.length < 32) {
    throw new Error('JWT_SECRET должен быть установлен и иметь длину не менее 32 символов');
  }
  return {
    secret: new TextEncoder().encode(secretValue),
    issuer: process.env.JWT_ISSUER ?? DEFAULT_JWT_ISSUER,
    audience: process.env.JWT_AUDIENCE ?? DEFAULT_JWT_AUDIENCE
  };
}

interface TokenPayload extends JWTPayload {
  userId: string;
  username: string;
  role: Role;
}

export async function generateJWT(user: User): Promise<string> {
  const config = getAuthConfig();
  const expiration = ROLE_TTL_SECONDS[user.role] ?? ROLE_TTL_SECONDS.viewer;

  return await new SignJWT({
    userId: user.id,
    username: user.username,
    role: user.role
  })
    .setProtectedHeader({ alg: 'HS256', typ: 'JWT' })
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.now() / 1000) + expiration)
    .setIssuer(config.issuer)
    .setAudience(config.audience)
    .sign(config.secret);
}

export async function verifyJWT(token: string): Promise<TokenPayload> {
  const config = getAuthConfig();
  const { payload } = await jwtVerify(token, config.secret, {
    issuer: config.issuer,
    audience: config.audience
  });
  const typed = payload as TokenPayload;
  if (!typed.userId || !typed.username || !typed.role) {
    throw new Error('Некорректный payload токена');
  }
  return typed;
}

export async function refreshJWT(token: string): Promise<string> {
  const payload = await verifyJWT(token);
  const user = await getUserById(payload.userId);
  if (!user) {
    throw new Error('Пользователь не найден, обновление токена невозможно');
  }
  return generateJWT(user);
}

const SCRYPT_PREFIX = 'scrypt:';

export async function hashPassword(password: string): Promise<string> {
  if (process.env.PASSWORD_HASH_ALGO === 'scrypt') {
    return hashPasswordWithScrypt(password);
  }
  const rounds = Number(process.env.BCRYPT_ROUNDS ?? 12);
  const salt = await bcrypt.genSalt(rounds);
  return bcrypt.hash(password, salt);
}

async function hashPasswordWithScrypt(password: string): Promise<string> {
  const salt = randomBytes(16);
  const derived = (await scrypt(password, salt, 64)) as Buffer;
  return `${SCRYPT_PREFIX}${salt.toString('base64')}:${derived.toString('base64')}`;
}

export async function comparePassword(password: string, hash: string): Promise<boolean> {
  if (!hash) {
    return false;
  }
  if (hash.startsWith(SCRYPT_PREFIX)) {
    const [, payload] = hash.split(SCRYPT_PREFIX);
    const [saltRaw, digestRaw] = payload.split(':');
    if (!saltRaw || !digestRaw) {
      return false;
    }
    const salt = Buffer.from(saltRaw, 'base64');
    const digest = Buffer.from(digestRaw, 'base64');
    const derived = (await scrypt(password, salt, digest.length)) as Buffer;
    return timingSafeEqual(derived, digest);
  }
  return bcrypt.compare(password, hash);
}

export async function validatePassword(user: User, password: string): Promise<boolean> {
  const isValid = await comparePassword(password, user.passwordHash);
  return isValid;
}

export function createAuthCookie(token: string, role: Role): string {
  const secure = shouldUseSecureCookie();
  const sameSite = 'Strict';
  const maxAge = ROLE_TTL_SECONDS[role] ?? ROLE_TTL_SECONDS.viewer;
  return [
    `${AUTH_COOKIE_NAME}=${token}`,
    'HttpOnly',
    secure ? 'Secure' : undefined,
    'Path=/',
    `Max-Age=${maxAge}`,
    `SameSite=${sameSite}`
  ]
    .filter(Boolean)
    .join('; ');
}

export function createExpiredAuthCookie(): string {
  return [
    `${AUTH_COOKIE_NAME}=deleted`,
    'HttpOnly',
    shouldUseSecureCookie() ? 'Secure' : undefined,
    'Path=/',
    'Max-Age=0',
    `Expires=${new Date(0).toUTCString()}`,
    'SameSite=Strict'
  ]
    .filter(Boolean)
    .join('; ');
}
