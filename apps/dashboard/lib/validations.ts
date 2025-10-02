import { z } from 'zod';

export const urlSchema = z
  .string()
  .trim()
  .min(1, 'URL обязателен')
  .max(2048, 'URL слишком длинный')
  .refine((value) => {
    try {
      // Разрешаем относительные пути и абсолютные HTTP(S) ссылки
      if (value.startsWith('/')) {
        return true;
      }
      const url = new URL(value);
      return url.protocol === 'http:' || url.protocol === 'https:';
    } catch {
      try {
        // Попытка интерпретировать как относительный путь
        new URL(value, 'http://localhost');
        return true;
      } catch {
        return false;
      }
    }
  }, 'Некорректный URL');

export const domainSchema = z
  .string()
  .trim()
  .toLowerCase()
  .min(1, 'Домен обязателен')
  .regex(/^[a-z0-9.-]+$/, 'Некорректный формат домена')
  .max(255, 'Домен слишком длинный');

export const timestampSchema = z
  .string()
  .refine((value) => !Number.isNaN(Date.parse(value)), 'Неверный формат даты');

export const filePathSchema = z
  .string()
  .trim()
  .regex(/^[\w./-]+$/, 'Путь содержит недопустимые символы')
  .refine((value) => !value.includes('..') && !value.includes('\0'), 'Недопустимый путь');

const linkObjectSchema = z
  .object({
    url: urlSchema,
    title: z.string().trim().max(512).optional(),
    priority: z.number().int().min(0).max(100).optional(),
    metadata: z.record(z.unknown()).optional()
  })
  .catchall(z.unknown());

const linkSchema = z.union([
  urlSchema,
  linkObjectSchema
]);

export const uploadJsonSchema = z
  .object({
    title: z.string().max(512, 'Слишком длинное название').optional(),
    created_at: timestampSchema.optional(),
    domain: domainSchema.optional(),
    total_links: z
      .number()
      .int('Значение должно быть целым')
      .min(0, 'total_links не может быть отрицательным')
      .max(100000, 'total_links слишком велико')
      .optional(),
    links: z
      .array(linkSchema, {
        invalid_type_error: 'Поле links должно быть массивом'
      })
      .min(1, 'Необходимо указать хотя бы одну ссылку')
      .max(50000, 'Слишком много ссылок'),
    tags: z.array(z.string().trim().max(64)).optional(),
    version: z.string().max(64).optional(),
    generated_at: timestampSchema.optional()
  })
  .strict();

export const mapStatusSchema = z.enum(['available', 'missing', 'outdated']);

export const mapFileMetadataSchema = z
  .object({
    size: z.number().int().min(0),
    modified: timestampSchema,
    linkCount: z.number().int().min(0).nullable(),
    isValid: z.boolean(),
    filePath: z.string().optional(),
    fileName: z.string().optional(),
    source: z.enum(['canonical', 'uploaded', 'legacy']).optional()
  })
  .strict();

export const exportConfigSchema = z
  .object({
    concurrency: z
      .number({ invalid_type_error: 'Concurrency должен быть числом' })
      .int('Concurrency должен быть целым числом')
      .min(1, 'Минимальное значение concurrency — 1')
      .max(128, 'Максимальное значение concurrency — 128')
      .optional(),
    resume: z.boolean().optional(),
    limit: z
      .number({ invalid_type_error: 'Limit должен быть числом' })
      .int('Limit должен быть целым числом')
      .min(1, 'Минимальный limit — 1')
      .max(100000, 'Максимальный limit — 100000')
      .optional(),
    args: z
      .array(
        z
          .string()
          .trim()
          .min(1, 'Аргумент не может быть пустым')
          .regex(/^[\w.-=\/@]+$/, 'Недопустимый символ в аргументах')
      )
      .max(32, 'Слишком много аргументов')
      .optional()
  })
  .strict();

const premiumStatsSchema = z.object({
  bandwidth: z.number().min(0).optional(),
  active_sessions: z.number().min(0).optional(),
  cost: z.number().min(0).optional(),
  monthly_budget: z.number().min(0).optional(),
  monthly_budget_remaining: z.number().min(0).optional(),
  proxy_countries: z.record(z.number().min(0)).optional(),
  proxy_protocols: z.record(z.number().min(0)).optional(),
  avg_response_time: z.number().min(0).optional(),
  avg_success_rate: z.number().min(0).max(100).optional(),
  auto_purchase_enabled: z.boolean().optional(),
  last_purchase_time: z.string().optional(),
  purchase_cooldown_remaining: z.number().int().min(0).optional(),
  max_purchase_batch_size: z.number().int().min(1).optional(),
  cost_per_proxy: z.number().min(0).optional()
});

export const autoscaleSchema = z
  .object({
    optimal_proxy_count: z.number().int().min(0),
    current_healthy: z.number().int().min(0),
    deficit: z.number().int(),
    status: z.enum(['sufficient', 'warning', 'critical']),
    recommended_purchase: z.number().int().min(0),
    estimated_cost: z.number().min(0),
    can_purchase: z.boolean().optional(),
    budget_remaining: z.number().min(0).optional(),
    cooldown_remaining_minutes: z.number().int().min(0).optional()
  })
  .optional();

export const proxyStatsSchema = z
  .object({
    total_proxies: z.number().min(0),
    active_proxies: z.number().min(0).optional(),
    healthy_proxies: z.number().min(0).optional(),
    failed_proxies: z.number().min(0).optional(),
    burned_proxies: z.number().min(0).optional(),
    total_requests: z.number().min(0),
    successful_requests: z.number().min(0).optional(),
    success_rate: z.number().min(0).max(100).optional(),
    proxy_rotations: z.number().min(0).optional(),
    health_checker_stats: z.number().int().min(0).optional(),
    circuit_breakers_open: z.number().int().min(0).optional(),
    proxy_countries: z.record(z.number().min(0)).optional(),
    proxy_protocols: z.record(z.number().min(0)).optional(),
    top_performing_proxies: z
      .array(
        z.object({
          proxy: z.string(),
          success_rate: z.number().min(0).max(100).optional(),
          latency_ms: z.number().min(0).optional(),
          country: z.string().optional()
        })
      )
      .optional(),
    premium_proxy_stats: premiumStatsSchema.optional(),
    warnings: z.array(z.string()).optional(),
    generated_at: timestampSchema.optional(),
    optimal_proxy_count: z.number().int().min(0).optional(),
    recommended_purchase: z.number().int().min(0).optional(),
    autoscale_status: z.enum(['sufficient', 'warning', 'critical']).optional(),
    purchase_estimate: z.number().min(0).optional(),
    autoscale: autoscaleSchema,
    autoscale_concurrency: z.number().int().min(0).optional()
  })
  .strict();

export const siteSummaryMetricsSchema = z
  .object({
    status: z.enum(['ok', 'error', 'missing']),
    export_file: z.string().optional(),
    products: z.number().int().min(0),
    products_with_price: z.number().int().min(0),
    products_with_stock_field: z.number().int().min(0).optional(),
    products_in_stock_true: z.number().int().min(0).optional(),
    products_total_stock: z.number().min(0).optional(),
    products_with_variations: z.number().int().min(0).optional(),
    total_variations: z.number().int().min(0).optional(),
    variations_total_stock: z.number().min(0).optional(),
    variations_in_stock_true: z.number().int().min(0).optional(),
    success_rate: z.number().min(0).max(100).optional(),
    errors: z.array(z.string()).optional(),
    warnings: z.array(z.string()).optional(),
    updated_at: timestampSchema.optional()
  })
  .strict();

export const summaryResponseSchema = z
  .object({
    sites: z.record(siteSummaryMetricsSchema),
    totals: z.object({
      total_sites: z.number().int().min(0),
      total_products: z.number().int().min(0),
      total_products_with_price: z.number().int().min(0),
      total_stock: z.number().min(0),
      total_variations: z.number().int().min(0),
      total_products_with_variations: z.number().int().min(0).optional(),
      average_success_rate: z.number().min(0).max(100).optional()
    }),
    generated_at: timestampSchema
  })
  .strict();

export const masterWorkbookStatusSchema = z
  .object({
    status: z.enum(['generating', 'ready', 'error']),
    file_size: z.number().min(0).optional(),
    generated_at: timestampSchema.optional(),
    error_message: z.string().optional(),
    phase: z.string().optional(),
    stale: z.boolean().optional()
  })
  .strict();

export type SiteSummaryMetrics = z.infer<typeof siteSummaryMetricsSchema>;
export type SummaryResponse = z.infer<typeof summaryResponseSchema>;
export type MasterWorkbookStatus = z.infer<typeof masterWorkbookStatusSchema>;
export type AutoscaleRecommendations = z.infer<typeof autoscaleSchema>;
export type MapStatus = z.infer<typeof mapStatusSchema>;
export type MapFileMetadata = z.infer<typeof mapFileMetadataSchema>;

export const roleSchema = z.enum(['admin', 'operator', 'viewer']);

export const permissionSchema = z.enum([
  'export:start',
  'export:stop',
  'export:view',
  'files:upload',
  'files:download',
  'files:delete',
  'logs:view',
  'stats:view',
  'proxy:manage',
  'users:create',
  'users:edit',
  'users:delete',
  'config:edit'
]);

export const passwordSchema = z
  .string()
  .min(8, 'Пароль должен содержать минимум 8 символов')
  .max(128, 'Пароль слишком длинный')
  .regex(/[A-Z]/, 'Пароль должен содержать хотя бы одну заглавную букву')
  .regex(/[a-z]/, 'Пароль должен содержать хотя бы одну строчную букву')
  .regex(/[0-9]/, 'Пароль должен содержать хотя бы одну цифру');

export const loginSchema = z
  .object({
    username: z
      .string()
      .trim()
      .min(3, 'Имя пользователя должно содержать минимум 3 символа')
      .max(50, 'Имя пользователя слишком длинное')
      .regex(/^[A-Za-z0-9_]+$/, 'Допускаются только латинские буквы, цифры и подчёркивания'),
    password: passwordSchema
  })
  .strict();

export const loginRequestSchema = loginSchema;

export const userSchema = z
  .object({
    id: z.string().trim().min(1, 'Идентификатор обязателен'),
    username: loginSchema.shape.username,
    passwordHash: z.string().min(20, 'Некорректный хеш пароля'),
    role: roleSchema,
    createdAt: timestampSchema,
    lastLogin: timestampSchema.nullable().optional(),
    active: z.boolean().default(true)
  })
  .strict();

export const authUserSchema = userSchema
  .omit({ passwordHash: true })
  .extend({
    permissions: z.array(permissionSchema).default([]),
    lastLogin: timestampSchema.nullable().optional()
  });

export const tokenSchema = z.string().min(16, 'Некорректный токен').max(4096, 'Токен слишком длинный');

export const jwtPayloadSchema = z
  .object({
    userId: z.string().min(1),
    username: loginSchema.shape.username,
    role: roleSchema,
    iat: z.number().int().min(0),
    exp: z.number().int().min(0),
    iss: z.string().min(1),
    aud: z.union([z.string().min(1), z.array(z.string().min(1)).nonempty()])
  })
  .strict();

export const ipAddressSchema = z
  .string()
  .refine(
    (value) =>
      value === 'unknown' ||
      /^(\d{1,3}\.){3}\d{1,3}$/.test(value) ||
      /^([a-fA-F0-9]{0,4}:){2,7}[a-fA-F0-9]{0,4}$/.test(value),
    'Некорректный IP-адрес'
  );

export const userAgentSchema = z.string().min(1).max(512);

export const auditEventSchema = z
  .object({
    timestamp: timestampSchema,
    userId: z.string().min(1),
    username: z.string().min(1),
    role: roleSchema.optional(),
    action: z.string().min(1),
    resource: z.string().optional(),
    details: z.record(z.unknown()).optional(),
    ip: ipAddressSchema,
    userAgent: userAgentSchema,
    success: z.boolean(),
    error: z.string().optional()
  })
  .strict();
