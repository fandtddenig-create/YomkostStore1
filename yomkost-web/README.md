# Yomkost Store, вебмагазин

Тільки сайт, без Telegram. Фронтенд на Vercel, бекенд на Render, база Neon (усе безкоштовно)

## Локально
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn web.backend.main:app --reload
python -m http.server 5500 --directory web/frontend
```
Сайт http://127.0.0.1:5500, API http://127.0.0.1:8000/docs

## База даних (Neon)
1. neon.tech, реєстрація, створити проєкт
2. Скопіювати Connection string, це і є DATABASE_URL (будь-який формат підходить)

Render free не зберігає файли, тому SQLite там не використовуй. Free Postgres від Render видаляється через 30 днів, тому беремо Neon

## Render (бекенд)
1. Залити папку на GitHub
2. Render, New, Blueprint, обрати репозиторій
3. Заповнити змінні: DATABASE_URL (з Neon), CORS_ORIGINS (адреса сайту на Vercel без слеша в кінці), WEB_OWNER_EMAILS (твоя пошта), PAYMENT_INSTRUCTIONS
4. Перевірити https://ТВІЙ-СЕРВІС.onrender.com/api/health

## Vercel (фронтенд)
1. New Project, той самий репозиторій
2. Root Directory: web/frontend, Framework Preset: Other, Build Command порожня
3. Перед цим у web/frontend/config.js вкажи адресу Render
4. Після деплою встав адресу Vercel у CORS_ORIGINS на Render

## Власник і товари
Зареєструйся на сайті з поштою зі WEB_OWNER_EMAILS, у профілі з’явиться Панель керування. Демо товари додаються автоматично при першому запуску, їх можна змінити в панелі. Щоб вимкнути демо, постав SEED_DEMO=false

## Безкоштовний Render засинає
Після 15 хвилин без запитів сервер спить, перший запуск займає до хвилини, сайт показує про це повідомлення. Щоб не засинав, додай https://ТВІЙ-СЕРВІС.onrender.com/api/health в uptimerobot.com (безкоштовно, перевірка кожні 5 хвилин)

## Замовлення розробки
Працює як у Telegram боті: заявка з 5 кроків, розробник бере її, називає суму, клієнт платить за QR і тисне «Я оплатив», адмін підтверджує оплату, розробник видає результат (він з’являється в кабінеті клієнта). До 3 активних заявок на клієнта. QR оплати лежить у `web/frontend/pay_qr.jpg`, заміни файл на свій. Розробники (роль developer) бачать чергу в «Кабінеті розробника», адмін і власник у «Панелі керування», вкладка «Розробка». Роль developer видає власник у вкладці «Користувачі» або після заявки «Стати розробником»
