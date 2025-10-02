#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) {
      continue;
    }
    const [key, value] = arg.slice(2).split('=');
    if (value === undefined) {
      args[key] = argv[i + 1];
      i += 1;
    } else {
      args[key] = value;
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv);
  const cwd = path.dirname(fileURLToPath(import.meta.url));
  const baseDir = path.resolve(cwd, '../../../logs');
  const dateSuffix = args.date ? `-${args.date}` : '';
  const filename = args.file ?? `audit${dateSuffix ? `-${dateSuffix}` : ''}.jsonl`;
  const filePath = path.join(baseDir, filename);

  let raw;
  try {
    raw = await readFile(filePath, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') {
      console.error(`Журнал ${filename} не найден. Убедитесь, что в каталоге logs/ присутствуют audit файлы.`);
      process.exit(1);
    }
    throw error;
  }

  const lines = raw.trim().split('\n');
  const tail = lines.slice(-50);
  console.log(tail.join('\n'));
}

main().catch((error) => {
  console.error('Не удалось прочитать audit log:', error);
  process.exit(1);
});
