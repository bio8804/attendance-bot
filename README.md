# Бот учета прихода и ухода сотрудников

Простой Telegram-бот для отметок прихода и ухода сотрудников.

## Возможности

- `/start` - регистрация сотрудника и список команд
- `/in` - отметить приход
- `/out` - отметить уход
- `/status` - показать текущий статус
- `/report` - отчет за сегодня
- `/report YYYY-MM-DD` - отчет за конкретную дату

Данные сохраняются в SQLite-файл `attendance.db`.

## Установка

1. Установите Python 3.10 или новее.
2. Установите зависимости:

```powershell
pip install -r requirements.txt
```

3. Создайте файл `.env` рядом с `.env.example`:

```powershell
Copy-Item .env.example .env
```

4. Вставьте токен Telegram-бота в `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:your_token
```

Токен можно получить у `@BotFather` в Telegram.

Если нужно ограничить отчет только администраторами, добавьте их Telegram ID через запятую:

```env
ADMIN_IDS=123456789,987654321
```

Если `ADMIN_IDS` пустой, команду `/report` смогут использовать все.

## Запуск

```powershell
python bot.py
```

При запуске бот также открывает health-check endpoint:

```text
GET /health
```

По умолчанию используется порт `8080`. В облаке можно задать переменную окружения `PORT`.

## Загрузка в GitHub

Перед загрузкой убедитесь, что файл `.env` не попал в репозиторий. Он уже добавлен в `.gitignore`.

```powershell
git init
git add .
git commit -m "Initial attendance bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

## Деплой в Choreo

1. Откройте https://console.choreo.dev.
2. Создайте Project и Component.
3. Подключите GitHub repository.
4. Выберите Dockerfile build preset.
5. Укажите Dockerfile path: `/Dockerfile`.
6. Укажите component directory: `/`.
7. Укажите port: `8080`.
8. В Configs & Secrets добавьте Secret:

```env
TELEGRAM_BOT_TOKEN=your_real_token
ADMIN_IDS=
```

9. Запустите build/deploy.

Важно: текущая версия использует SQLite-файл `attendance.db`. Для теста этого достаточно, но для постоянной работы в облаке лучше заменить SQLite на PostgreSQL или другую внешнюю базу данных.

## Пример

Сотрудник пишет боту:

```text
/in
/out
/status
```

Администратор получает отчет:

```text
/report
/report 2026-05-03
```
