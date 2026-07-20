# yt-dlp channel tools

Утилиты для работы с коллекциями видео, скачанными с YouTube-каналов
(`yt-dlp` сохраняет файлы вида `Название [VIDEO_ID].mp4`).

## Структура проекта

```
yt-dlp/
  _channels/           ← папки отдельных YouTube-каналов (не в git)
    _Handle/
      _cache/          ← кэш канала: videos.json, playlists.json, video_playlists.json
      _playlists/      ← папки плейлистов (имена без ведущего _)
      _summaries/      ← summaries по датам: YYYY-MM-DD/_hh_mm_*.txt/xlsx
      ...
  misc/                ← прочие коллекции, не привязанные к каналу (не в git)
  tests/               ← bat-файлы проверки плейлистов
  src/                 ← исходники Python
  scripts/             ← готовые exe для запуска
  readme.md
```

## Пайплайн

1. **`get_summary_for_channel`** — summaries канала в `.txt` / `.xlsx` (+ столбец плейлиста)
2. *(скачивание видео — следующий шаг пайплайна)*
3. **`organize_by_playlists`** — раскладка скачанных файлов по плейлистам

Каждая подпапка в `_channels/` — один YouTube-канал. Имя папки формируется из
пользовательского `@handle` канала с ведущим подчёркиванием, например
`@Ekaterina_Schulmann` → `_Ekaterina_Schulmann`.

## Скрипты в `scripts/`

| Файл | Назначение |
|------|------------|
| `get_summary_for_channel.exe` | Шаг 1: summaries видео канала в TXT/XLSX |
| `refresh_channel_cache.exe` | Обновление кэша видео канала (_cache/videos.json) |
| `organize_by_playlists.exe` | Раскладка видео по плейлистам и нумерация файлов |

Исходники: `src/get_summary_for_channel.py`, `src/refresh_channel_cache.py`, `src/organize_by_playlists.py`.  
Сборка exe: `python src/build_exe.py` (нужен `pyinstaller`).

Требование: в `PATH` должен быть доступен `yt-dlp` (для столбца плейлиста).  
Разрешение `@handle` → `UC…` — через страницу канала (без yt-dlp).

---

## `get_summary_for_channel.exe`

### Минимальный запуск

```bat
scripts\get_summary_for_channel.exe @Ekaterina_Schulmann
```

или:

```bat
python src\get_summary_for_channel.py @Ekaterina_Schulmann
```

**Первый параметр (обязательный):** `UC…`, `@handle` или URL канала.  
**Второй параметр (необязательный, default: `bypls`):** режим или плейлист:

| Значение | Поведение |
|----------|-----------|
| `bypls` | Группировка по плейлистам (default) |
| `allpls` | Общий порядок канала, плейлисты вперемешку |
| `#A`, `#B`, … | Только указанный плейлист (алиас из `playlists.json`) |
| префикс имени | ≥3 символов, совпадает с началом ровно одного плейлиста |

Выходные файлы: `_channels/_Handle/_summaries/YYYY-MM-DD/_hh_mm_*.txt` и `.xlsx`  
Имя файла **без** имени канала; начинается с `_hh_mm_` и отражает режим/флаги.

| Шаблон | Условие |
|--------|---------|
| `_hh_mm_pls` | `--plsonly` |
| `_hh_mm_plsall` | `allpls`, без `--from`/`--to` |
| `_hh_mm_plsgrp` | `bypls` (default), без фильтра |
| `_f_N`, `_t_N` | при `--from N` / `--to N` |
| `_n_N` | при `--next N` (если `--next` задан позже `--to` или вместо него) |
| `_new` | при `--new` |
| `_pls_A` | один плейлист по алиасу `#A` |

### `--plsonly`

Только список плейлистов (остальные аргументы игнорируются).  
Создаёт `_cache/playlists.json`, если его нет. Каждому плейлисту присваивается
алиас `#A`, `#B`, … `#Z`, `#AA`, … (как столбцы Excel).

XLSX: строка 1 — `Playlists for channel <name>  (channel_id = UC… ):`,  
далее столбцы: алиас, имя канала, название плейлиста.

### Столбцы XLSX (summary видео)

| Col | Поле |
|-----|------|
| 1 | № (индекс на канале или в плейлисте — см. режим) |
| 2 | Оценка стоимости транскрибирования, $ |
| 3 | Имя канала |
| 4 | Плейлист |
| 5 | URL |
| 6 | Дата |
| 7 | Длительность |
| 8 | Заголовок |

**Строка TXT (видео):** `{index} {cost} {url} ({channel} | {playlist} : {title} ) {date} {duration}` —
если плейлист известен; иначе `{index} {cost} {url} ({channel} : {title} ) …`.  
`{index}` — номер как в XLSX (min 3 символа; шире при номерах ≥1000); при `--new` для новых видео — `new`.  
`{cost}` — оценка стоимости транскрибирования, например `$0.83`.  
Консоль: `@handle (UC…)`; в XLSX/TXT — display name канала с YouTube.

### Оценка стоимости транскрибирования

Стоимость считается по тарифу OpenAI (`whisper-1` / `gpt-4o-transcribe`,
$0.006/мин ≈ $0.36/час на июль 2026). Тариф кэшируется в **общем** для всех
каналов файле `_cache/transcription_pricing.json` в корне проекта и
перепроверяется по странице тарифов OpenAI не чаще, чем раз в 30 дней
(при недоступности страницы используется кэшированное значение).

Ключ `--lang XX` (по умолчанию `ru`) задаёт язык оригинала: для неанглийских
каналов стоимость считается за **два** прохода транскрибирования
(язык оригинала + английский), для `--lang en` — за один.

**Нумерация:** при `bypls` без `--from`/`--to` — сквозная **внутри каждого плейлиста**
(1, 2, 3… заново для каждого). При `allpls` или с фильтром — номера относительно
**baseline**-списка (`length_old`). Новые видео (после последнего запуска без `--new`)
при `--new` выводятся первыми с номером `new`.

### `--new`

Сохраняет baseline-нумерацию (`length_old`) для запланированной поэтапной загрузки.
`--from` / `--to` считаются относительно baseline, а не текущей длины канала.
При `--new` в начало summary попадают все новые видео (номер `new`), затем диапазон
`--from`…`--to`. Без `--new` после успешного экспорта `length_old` обновляется до
текущей длины.

Пример поэтапной загрузки (шаг 2 пайплайна, 900+ видео, ~200/день):

```bat
python src\get_summary_for_channel.py @Channel allpls --from 1 --to 200
python src\get_summary_for_channel.py @Channel allpls --new --from 201 --next 200
python src\get_summary_for_channel.py @Channel allpls --new --from 401 --next 200
```

`--next N` — альтернатива `--to`: экспорт N видео начиная с `--from`
(`--to = --from + N - 1`). Если указаны оба, учитывается **более поздний** флаг
в командной строке.

### `--from` / `--to` по названию видео

Значение может быть числом или строкой **не короче 3 символов** (при пробелах — в кавычках).
Строка сопоставляется с полным названием или с **единственным** префиксом названия
в кэшированном списке; найденному видео присваивается его baseline-номер.
Если совпадений нет или их несколько — скрипт завершается, в `_summaries` пишется
однострочный notice `.txt`.

### Кэш

| Файл | Содержимое |
|------|------------|
| `videos.json` | Список видео канала, `length_curr`, `length_old`, длины по плейлистам |
| `playlists.json` | Плейлисты канала + алиасы `#A`… |
| `video_playlists.json` | `video_id → playlist title` |

**Smart refresh** (автоматически при каждом обращении):

- `length_curr` уменьшилась → аварийный полный refresh, команда прерывается (в `_summaries` — notice)
- `length_curr` не изменилась → если кэш старше 24 ч, сверка 4 видео; иначе кэш без изменений
- `length_curr` выросла → сверка со сдвигом (новые в начале); при успехе — подкачка без полного refresh

Полный refresh вручную: `python src/refresh_channel_cache.py @Channel --force`  
Инкрементальный (по умолчанию): `python src/refresh_channel_cache.py @Channel`

Флаг `--refresh` **убран**. Флаг `--new` — см. выше.

### Примеры

```bat
:: Summary по плейлистам (default bypls)
python src\get_summary_for_channel.py @Ekaterina_Schulmann

:: Общий список канала
python src\get_summary_for_channel.py @Ekaterina_Schulmann allpls --from 1 --to 50

:: Один плейлист по алиасу
python src\get_summary_for_channel.py @VladilenMinin "#A" --from 1 --to 20

:: Только список плейлистов
python src\get_summary_for_channel.py @Ekaterina_Schulmann --plsonly
```

---

## `organize_by_playlists.exe`

### Минимальный запуск

```bat
scripts\organize_by_playlists.exe _VladilenMinin
```

**Обязательный параметр:** имя подпапки канала в `_channels/`.

Путь к коллекции: `_channels/<folder>/`. Видео раскладываются в `_playlists/<playlist>/`.

### Параметры

| Параметр | Описание |
|----------|----------|
| `folder` | **Обязательный.** Имя подпапки канала в `_channels/` |
| `--output-dir PATH` | Родительская папка каналов |
| `--channel URL` | URL или handle канала |
| `--order-mode courses\|all\|none` | Нумерация файлов |
| `--dry-run` | Только план |
| `--cookies-from-browser BROWSER` | Cookies для yt-dlp |
| `--yt-dlp PATH` | Путь к yt-dlp |

### Проверка плейлистов

```bat
tests\check_playlists.bat
tests\update_playlists.bat
```

Папки `_channels/` и `misc/` исключены из git (см. `.gitignore`).
