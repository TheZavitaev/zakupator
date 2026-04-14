# Zakupator

Telegram-бот, который сравнивает цены на продукты в **ВкусВилле**, **Ашане** и **Metro** и помогает собирать корзину сразу в трёх сервисах.

> Зачем: чтобы не лазить по трём приложениям доставки каждый раз, когда хочется купить молока. Бот даёт топ-3 предложения в каждом сервисе, позволяет добавлять товары в персональную корзину, сравнивать одинаковые товары между сервисами (fuzzy matching по названию и объёму), и открывать корзины сервисов одним тапом для итоговой оплаты.

## Что умеет

- **Поиск** — пишешь `молоко простоквашино` без команды, получаешь топ-3 из каждого сервиса с ценами и ссылками
- **`/compare`** — находит сопоставимые товары в разных сервисах и показывает честного победителя с экономией
- **Корзина** — кнопки «в корзину» под каждым результатом, `+/-` для количества, «📋 Скопировать» и «🛒 Открыть в сервисе» для оформления
- **`/total`** — сводка по сервисам одной строкой
- **`/history`** — список последних уникальных запросов кнопками, клик — повторный поиск
- **Ретраи на транзиентных ошибках**, кэш ответов на 5 минут, человеческие сообщения об ошибках

## Команды

| Команда | Что делает |
|---|---|
| _любое сообщение_ | Поиск по трём сервисам |
| `/search <запрос>` | То же самое, явно |
| `/compare <запрос>` | Сопоставимые товары, лучший по цене |
| `/cart` | Корзина, сгруппированная по сервисам, с подытогами |
| `/total` | Короткая сводка по каждому сервису + общий итог |
| `/history` | Последние 10 уникальных запросов |
| `/clear` | Очистить корзину (с подтверждением) |

## Архитектура

```
Telegram long-polling (aiogram 3)
            ↓
        bot.py         ← handlers, форматтеры, inline keyboards
            ↓
       search.py       ← fan-out по адаптерам, параллельно, с таймаутом
            ↓         ↓
   response_cache   адаптеры
     (TTL 5 min)       ↓
                     net.py (retry + backoff)
                       ↓
              httpx.AsyncClient → сервисы
```

Состояние (пользователи, корзины, история) — в SQLite через async SQLAlchemy. Поисковые результаты для inline-кнопок — в in-memory LRU `search_cache.py` с TTL 30 минут.

Cross-service matching (блок B) живёт в `matching.py` — парсит объёмы/массы из названий, сравнивает по `rapidfuzz.token_set_ratio`, жёстко требует совпадение unit class и ≤12% разницы по значению.

## Как запустить локально

Нужны Python 3.12+ и токен от [BotFather](https://t.me/BotFather).

```bash
git clone <repo> zakupator
cd zakupator

python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

cp .env.example .env
# впиши свой TELEGRAM_BOT_TOKEN=... в .env

./run.sh    # обёртка, которая заодно чинит macOS hidden-флаг на venv
```

Или напрямую:
```bash
.venv/bin/python -m zakupator
```

Тесты:
```bash
.venv/bin/pytest           # 127 тестов без сети
.venv/bin/pytest -m live   # (пока пусто) контрактные тесты с живыми сервисами
```

Обновить фикстуры с живых сервисов:
```bash
.venv/bin/python scripts/capture_fixtures.py
```

## Деплой на ВПС

### Вариант 1 — Docker Compose (проще всего)

Требуется Docker и Docker Compose на хосте.

```bash
git clone <repo> /opt/zakupator
cd /opt/zakupator

# впиши токен в .env
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=<твой токен из BotFather>
EOF

docker compose up -d --build
docker compose logs -f          # следим за логами
```

БД живёт в именованном volume `zakupator-data`. Чтобы сохранить её при пересборке — ничего делать не надо, volume переживёт `docker compose down`. Чтобы снести вместе с данными — `docker compose down -v`.

Обновление:
```bash
cd /opt/zakupator
git pull
docker compose up -d --build
```

### Вариант 2 — systemd (без Docker)

Нужен Python 3.12+ и sudo на хосте.

```bash
# Пользователь под бот
sudo useradd --system --home /opt/zakupator --shell /usr/sbin/nologin zakupator

# Код и venv
sudo git clone <repo> /opt/zakupator
sudo chown -R zakupator:zakupator /opt/zakupator
sudo -u zakupator python3.12 -m venv /opt/zakupator/.venv
sudo -u zakupator /opt/zakupator/.venv/bin/pip install -e /opt/zakupator

# Конфиг
sudo -u zakupator tee /opt/zakupator/.env > /dev/null <<EOF
TELEGRAM_BOT_TOKEN=<твой токен>
EOF

# Unit
sudo cp /opt/zakupator/deploy/zakupator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zakupator

# Проверить
sudo systemctl status zakupator
sudo journalctl -u zakupator -f
```

Обновление:
```bash
cd /opt/zakupator
sudo -u zakupator git pull
sudo systemctl restart zakupator
```

## Конфигурация

Все настройки — через переменные окружения или `.env`-файл.

| Переменная | По умолчанию | Что это |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *обязательно* | Токен от BotFather |
| `DATABASE_URL` | `sqlite+aiosqlite:///./zakupator.db` | URL SQLAlchemy (можно указать postgres) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DEFAULT_ADDRESS_*` | Москва, Красная площадь | Фоллбек-адрес для сервисов до `/address` |

## Почему именно эти три сервиса

Короткий ответ: остальные либо полностью заблокированы антиботом (Озон Fresh, Яндекс Лавка, Перекрёсток Впрок, Магнит — все Tier 3), либо требуют реверса client-side signing (Самокат, Купер). ВкусВилл / Ашан / Metro — единственные сервисы крупного масс-маркет уровня, у которых API достаточно открыт для чистого скрейпа без Playwright и residential proxy.

Полный отчёт разведки — в `docs/recon.md`.

## Добавить новый сервис

1. Написать адаптер в `src/zakupator/adapters/<service>.py`, унаследовавшись от `ServiceAdapter`
2. Добавить в `Service` enum в `models.py`
3. Зарегистрировать в `build_default_adapters()` в `search.py`
4. Добавить лейблы/ссылки в `_SERVICE_LABELS` / `_SERVICE_HOME` / `_SERVICE_CART_LINKS` в `bot.py`
5. Написать тест на парсер — положить фикстуру в `tests/fixtures/`, использовать `mock_client` из `tests/conftest.py`
6. При необходимости — обновить `scripts/capture_fixtures.py`

## Документация

Короткие ссылки для тех, кто копает глубже:

- [docs/SPEC.md](docs/SPEC.md) — авторитетные контракты: данные, адаптеры, матчинг, callback-схема, БД, ошибки
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — компоненты, поток запроса, обоснования решений
- [docs/ADAPTERS.md](docs/ADAPTERS.md) — чек-лист: как добавить новый сервис
- [docs/recon.md](docs/recon.md) — отчёт по разведке всех сервисов на рынке
- [CHANGELOG.md](CHANGELOG.md) — история релизов

## Качество кода

Проект прогоняется через единый локальный гейт, который зеркалит CI:

```bash
scripts/check.sh all       # всё сразу
scripts/check.sh lint      # только ruff
scripts/check.sh type      # только mypy --strict
scripts/check.sh sast      # bandit + semgrep + pip-audit
scripts/check.sh test      # только pytest
```

Пайплайн в GitHub Actions (`.github/workflows/ci.yml`) запускает те же
четыре этапа параллельными джобами на каждый push и PR в `main`.

Pre-commit хуки (ruff, mypy, bandit, базовые санитайзеры) ставятся один раз:

```bash
.venv/bin/pre-commit install
```

## Лицензия / ответственность

Этот проект — для личного использования. Каждый сервис предоставляет данные через свои публичные веб-интерфейсы; бот отправляет ровно те же запросы, что делает обычный браузер, в нормальном темпе. Не предназначен для массовых коммерческих выгрузок — уважай rate limits и ToS сервисов.
