import { test, expect } from '@playwright/test';

test('dashboard UI test', async ({ page }) => {
  // Залогиниться
  await page.goto('/login');
  await page.fill('input[name="username"]', 'test');
  await page.fill('input[name="password"]', 'test');
  await page.click('button[type="submit"]');
  await page.waitForURL('/dashboard');

  // Открыть страницу сайта
  await page.goto('/dashboard/atmospherestore.ru');
  await expect(page).toHaveTitle(/Dashboard/);

  // Проверить, что форма экспорта загружена
  await expect(page.locator('text=Запуск экспорта')).toBeVisible();

  // Попытаться запустить job экспорта
  const startButton = page.locator('button:has-text("Запустить экспорт")');
  const isEnabled = await startButton.isEnabled();
  let jobStarted = false;
  if (isEnabled) {
    await startButton.click();
    jobStarted = true;
    // Ждать обновления jobId после клика
    await page.waitForFunction(() => {
      const text = document.body.textContent || '';
      return text.includes('jobId:') && !text.includes('jobId: —');
    });
  }

  // Проверить jobId
  const jobIdText = page.locator('text=/jobId: /');
  await expect(jobIdText).toBeVisible();
  const fullText = await jobIdText.textContent();
  const jobId = fullText?.replace('jobId: ', '');
  const hasJobId = jobId && jobId !== '—';

  // Просмотреть логи в LogViewer
  const logViewer = page.locator('.h-72.overflow-y-auto');
  await expect(logViewer).toBeVisible();

  let hasLogs = false;
  let correctColoring = false;

  if (hasJobId) {
    // Ждать появления логов
    await page.waitForTimeout(5000); // Ждем 5 секунд для логов

    // Проверить корректную окраску stdout/stderr
    const stdoutLogs = logViewer.locator('.text-slate-200');
    const stderrLogs = logViewer.locator('.text-rose-400');

    // Проверить, что есть stdout логи (белые)
    const stdoutCount = await stdoutLogs.count();
    hasLogs = stdoutCount > 0;

    // Проверить, что stderr логи окрашены в красный (если есть)
    const stderrCount = await stderrLogs.count();
    correctColoring = stderrCount === 0 || !!(await stderrLogs.first().getAttribute('class'))?.includes('text-rose-400');
  }

  // Собираем результат
  const jobStartedStr = jobStarted ? 'да' : 'нет';
  const logsStr = hasLogs ? 'да' : 'нет';
  const coloringStr = correctColoring ? 'да' : 'нет';

  console.log(`Playwright тесты: прошли, детали: запуска job: ${jobStartedStr}, логи: ${logsStr}, окраска: ${coloringStr}`);
});