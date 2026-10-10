# Інструкція з розгортання (Deployment Guide)

Цей посібник містить практичні інструкції з розгортання системи раннього попередження про реактивні БпЛА на Івано-Франківськ на безкоштовних або умовно-безкоштовних платформах.

---

## 1. Підготовка змінних середовища

Для роботи системи потрібні облікові дані Telegram MTProto (слухач каналів) та Telegram Bot API (сповіщення підписників):

1. **Telegram API ID та Hash** (для Telethon):
   - Отримайте на [my.telegram.org](https://my.telegram.org) у розділі *API development tools*.
2. **Токен бота** (для Aiogram):
   - Створіть бота через [@BotFather](https://t.me/BotFather) і збережіть токен.
3. **Безфайлова сесія Telethon (`StringSession`)**:
   - Щоб не залежати від локальних файлів `.session` на серверах з ефемерними дисками (Koyeb, Render, PaaS), згенеруйте рядок сесії:
     ```bash
     python -m src.telegram.generate_session
     ```
   - Скопіюйте виведений довгий рядок у змінну `TELETHON_STRING_SESSION`.

Приклад готового файлу `.env`:
```env
TELETHON_API_ID=12345678
TELETHON_API_HASH=abcdef0123456789abcdef0123456789
TELETHON_STRING_SESSION=1BVtsOHQBu...
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

API_HOST=0.0.0.0
API_PORT=8080
REFERENCE_DIR=data/reference
LEARNING_DB_PATH=data/learning.db
SUBSCRIBERS_DB_PATH=data/subscribers.db
```

---

## 2. Варіант А: Oracle Cloud (OCI) Always Free (Рекомендовано)

Oracle Cloud надає безкоштовно назавжди:
- До 4 ядер ARM Ampere / 24 ГБ RAM (або 2 x AMD Micro-інстанси).
- Постійні диски (Persistent Block Storage).
- Виділену публічну IP-адресу.

### Кроки налаштування на OCI VM (Ubuntu 22.04 / 24.04):

1. **Встановлення Docker та Compose:**
   ```bash
   sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
   sudo usermod -aG docker $USER
   newgrp docker
   ```

2. **Клонування репозиторію:**
   ```bash
   git clone <URL_РЕПОЗИТОРІЮ> /opt/if-drone-alert
   cd /opt/if-drone-alert
   ```

3. **Створення `.env`:**
   ```bash
   cp .env.example .env
   nano .env # вставте ваші ключі та StringSession
   ```

4. **Запуск через Docker Compose:**
   ```bash
   docker compose up -d
   ```

5. **Перевірка логів та працездатності:**
   ```bash
   docker compose logs -f
   curl http://localhost:8080/health
   ```

---

## 3. Варіант Б: Koyeb (Безкоштовний PaaS без засинання)

[Koyeb](https://www.koyeb.com/) пропонує безкоштовний рівень (Free Eco dyno), який працює без зупинки за наявності веб-порту та healthcheck.

1. Зареєструйтесь на Koyeb і виберіть **Create App**.
2. Оберіть розгортання з GitHub-репозиторію (або Docker Hub).
3. У налаштуваннях:
   - **Port**: `8080`
   - **Health check path**: `/health`
   - **Type**: Web Service
4. Додайте змінні середовища (Environment Variables):
   - `TELETHON_API_ID`
   - `TELETHON_API_HASH`
   - `TELETHON_STRING_SESSION`
   - `TELEGRAM_BOT_TOKEN`
   - `API_PORT=8080`
5. Натисніть **Deploy**.

> **Важливо для PaaS:** Оскільки диски в безкоштовних тарифах PaaS можуть скидатися при перезапуску, `TELETHON_STRING_SESSION` гарантує, що авторизація в Telegram не злетить. Для постійного збереження розмітки та підписників рекомендовано OCI VM або монтування сховища (Koyeb Volumes).

---

## 4. Варіант В: Запуск як systemd-сервіс (без Docker)

Якщо ви розгортаєте на звичайному Linux VPS без Docker:

1. **Створення віртуального оточення:**
   ```bash
   cd /opt/if-drone-alert
   python3 -m venv .venv
   .venv/bin/pip install --upgrade pip
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python -m src.learning.cli seed
   ```

2. **Створення сервісного файлу `/etc/systemd/system/drone-alert.service`:**
   ```ini
   [Unit]
   Description=IF Reactive Drone Early Alert Service
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/opt/if-drone-alert
   EnvironmentFile=/opt/if-drone-alert/.env
   ExecStart=/opt/if-drone-alert/.venv/bin/python -m src.main
   Restart=always
   RestartSec=5
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```

3. **Запуск та увімкнення автозапуску:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now drone-alert
   sudo systemctl status drone-alert
   ```

---

## 5. Робота в режимі тестування (Shadow Mode)

Перед тим як відкривати бота широкій публіці, проведіть 2–3 тижні в режимі калібрування:
1. Запустіть сервіс з окремим тестовим токеном бота або підпишіть на нього лише себе й команду валідації.
2. Щодня або після кожної атаки перевіряйте стан цілей:
   ```bash
   python -m src.learning.cli unlabeled
   ```
3. Розмічайте результат:
   ```bash
   python -m src.learning.cli label T-202603240400-abcd reached_if --arrived-at 2026-03-24T04:22:00Z
   ```
4. Переглядайте Go/No-Go scorecard:
   ```bash
   python -m src.engine.shadow_runner --evaluate
   ```
5. Коли назбирається $\ge 20$ розмічених цілей і всі критерії перейдуть у статус `[✅ PASS]`, система готова до публічного бета-релізу.
