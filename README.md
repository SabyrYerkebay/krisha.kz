# krisha.kz scraper

Парсер объявлений о недвижимости с [krisha.kz](https://krisha.kz/). Обходит страницы поиска (с любыми фильтрами),
при желании открывает каждое объявление и сохраняет результат в CSV, JSON Lines или JSON.

## Установка

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Нужен Python 3.10+.

## Использование

Настройте фильтры на сайте (город, комнаты, цена…), скопируйте ссылку из адресной строки и передайте её парсеру:

```bash
# Первые 3 страницы продажи квартир в Алматы → CSV
python -m krisha_scraper https://krisha.kz/prodazha/kvartiry/almaty/ --pages 3 -o data/almaty.csv

# Аренда в Астане, с открытием каждого объявления (параметры, описание, координаты, фото)
python -m krisha_scraper https://krisha.kz/arenda/kvartiry/astana/ --details -o data/astana.jsonl

# Несколько поисков сразу, не больше 500 объявлений
python -m krisha_scraper https://krisha.kz/prodazha/kvartiry/almaty/ https://krisha.kz/prodazha/kvartiry/astana/ \
    --limit 500 -o data/listings.csv

# Одно объявление
python -m krisha_scraper https://krisha.kz/a/show/123456789 -o data/one.json
```

Все опции: `python -m krisha_scraper --help`.

| Опция | Что делает |
|---|---|
| `-o, --output` | файл результата; формат по расширению: `.csv`, `.jsonl`, `.json` (по умолчанию `data/listings.csv`) |
| `-p, --pages N` | обойти не больше N страниц поиска (по умолчанию — все) |
| `--start-page N` | начать с N-й страницы |
| `-d, --details` | открывать каждое объявление (в ~20 раз больше запросов) |
| `--limit N` | остановиться после N объявлений |
| `--delay S` | пауза между запросами, по умолчанию 1.5 с (±20%) |
| `--retries N` | повторы при сетевых ошибках и HTTP 429/5xx (учитывается `Retry-After`) |

Ctrl+C останавливает обход — уже собранные объявления сохраняются.

## Что собирается

Со страницы поиска:

| Поле | Пример |
|---|---|
| `id`, `url` | `1000001`, `https://krisha.kz/a/show/1000001` |
| `title` | `2-комнатная квартира · 56 м² · 5/9 этаж` |
| `rooms`, `area_m2`, `floor`, `floors_total` | `2`, `56.0`, `5`, `9` — разобраны из заголовка |
| `price`, `price_text` | `25000000`, `25 000 000 〒` |
| `address`, `city`, `published` | `Алмалинский р-н, Абая 10`, `Алматы`, `5 окт.` |
| `description_preview`, `photo` | начало описания, превью фото |

С `--details` дополнительно:

| Поле | Описание |
|---|---|
| `lat`, `lon` | координаты |
| `description` | полное описание |
| `photos` | все фото (в CSV — через ` \| `) |
| параметры | `Тип дома`, `Год постройки`, `Состояние`, `Санузел`… — в CSV отдельными колонками, в JSON — объект `params` |

CSV сохраняется в UTF-8 с BOM, поэтому Excel открывает кириллицу без танцев.

Телефоны и имена продавцов не собираются: это персональные данные.

## Аккуратность

- Парсер соблюдает `robots.txt` сайта (включая шаблоны `*` и `$`): запрещённые адреса не загружаются.
- Между запросами есть пауза; не ставьте `--delay` меньше секунды, чтобы не нагружать сайт и не получить бан.
- Перед использованием данных проверьте пользовательское соглашение krisha.kz.

## Разработка

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Структура:

- `krisha_scraper/parsers.py` — разбор HTML (селекторы `div.a-card`, `.offer__*`, JSON из `<script id="jsdata">`).
  Если krisha.kz поменяет вёрстку, править нужно здесь.
- `krisha_scraper/client.py` — HTTP: пауза, повторы, robots.txt.
- `krisha_scraper/scraper.py` — обход страниц поиска и объявлений.
- `krisha_scraper/storage.py` — запись в CSV / JSONL / JSON.
- `krisha_scraper/cli.py` — командная строка.
