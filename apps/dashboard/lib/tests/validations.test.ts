import { describe, expect, test } from 'vitest';

import { uploadJsonSchema } from '../validations';

describe('uploadJsonSchema', () => {
  test('принимает карту с неизвестными ключами метаданных', () => {
    const payload = {
      title: 'Manefa sitemap export',
      domain: 'manefa.ru',
      total_links: 2,
      generated_at: '2025-10-09T21:44:00Z',
      links: ['https://manefa.ru/catalog', 'https://manefa.ru/catalog/yarn'],
      id: 'job-123',
      startedAt: '2025-10-09T21:40:00Z',
      endedAt: '2025-10-09T21:44:00Z',
      endpoint: '/api/jobs/manefa.ru',
      formState: { concurrency: 8, resume: false },
      status: 'success'
    };

    const result = uploadJsonSchema.safeParse(payload);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.domain).toBe('manefa.ru');
      expect(result.data.links).toHaveLength(2);
    }
  });

  test('отклоняет карту без обязательного массива links', () => {
    const invalidPayload = {
      title: 'Empty map'
    };

    const result = uploadJsonSchema.safeParse(invalidPayload);
    expect(result.success).toBe(false);
  });
});
