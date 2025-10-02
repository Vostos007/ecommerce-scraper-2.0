'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/hooks/useAuth';
import { loginSchema } from '@/lib/validations';

type LoginFormValues = {
  username: string;
  password: string;
};

const defaultRedirect = '/dashboard';

export default function LoginClient() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const redirectTo = useMemo(() => {
    const candidate = searchParams?.get('next');
    if (!candidate || !candidate.startsWith('/')) {
      return defaultRedirect;
    }
    return candidate;
  }, [searchParams]);
  const { login, isAuthenticated, isLoading, error, clearError } = useAuth();
  const [submitting, setSubmitting] = useState(false);

  const {
    register,
    handleSubmit,
    formState: { errors }
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      username: '',
      password: ''
    }
  });

  useEffect(() => {
    if (!isLoading && isAuthenticated) {
      router.replace(redirectTo);
    }
  }, [isAuthenticated, isLoading, redirectTo, router]);

  const onSubmit = handleSubmit(async (values) => {
    setSubmitting(true);
    clearError();
    try {
      await login(values);
      router.replace(redirectTo);
    } catch {
      // Ошибка уже сохранена в контексте auth
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-background via-background to-muted">
      <Card className="w-full max-w-md backdrop-blur">
        <CardHeader>
          <CardTitle>Добро пожаловать</CardTitle>
          <CardDescription>Введите учётные данные для доступа к Scraper Dashboard.</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={onSubmit} autoComplete="off">
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="username">
                Имя пользователя
              </label>
              <Input
                id="username"
                type="text"
                autoComplete="off"
                placeholder="Введите логин"
                aria-invalid={Boolean(errors.username)}
                {...register('username')}
              />
              {errors.username ? (
                <p className="text-xs text-destructive">{errors.username.message}</p>
              ) : null}
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="password">
                Пароль
              </label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                placeholder="Введите пароль"
                aria-invalid={Boolean(errors.password)}
                {...register('password')}
              />
              {errors.password ? (
                <p className="text-xs text-destructive">{errors.password.message}</p>
              ) : null}
            </div>
            {error ? <p className="text-xs text-destructive">{error}</p> : null}
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? 'Авторизация…' : 'Войти'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
