import { redirect } from 'next/navigation';

export const metadata = {
  title: 'UI Dashboard',
  description: 'Авторизация отключена — переход к дашборду.'
};

export default function LoginPage() {
  redirect('/dashboard');
}
