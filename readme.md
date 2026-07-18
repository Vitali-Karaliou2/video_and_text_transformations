# yt-dlp channel tools

Утилиты для работы с коллекциями видео, скачанными с YouTube-каналов
(`yt-dlp` сохраняет файлы вида `Название [VIDEO_ID].mp4`).

## Структура проекта

```
yt-dlp/
  cache/               ← JSON-кэш списков видео каналов (не в git)
  _channels/           ← папки отдельных YouTube-каналов (не в git)
    _VladilenMinin/
    _Ekaterina_Schulmann/
    _AleksanderLamkov/
    ...
  misc/                ← прочие коллекции, не привязанные к каналу (не в git)
  src/                 ← исходники Python
  scripts/             ← готовые exe для запуска
  Prompts/             ← промпты и заметки
  readme.md
```

## Пайплайн

1. **`export_channel`** — аннотации канала в `.txt` / `.xlsx` (+ столбец плейлиста)
2. *(скачивание видео — следующий шаг пайплайна)*
3. **`organize_by_playlists`** — раскладка скачанных файлов по плейлистам

Каждая подпапка в `_channels/` — один YouTube-канал. Имя папки формируется из
пользовательского `@handle` канала с ведущим подчёркиванием, например
`@Ekaterina_Schulmann` → `_Ekaterina_Schulmann` (внутренние `_` в handle
сохраняются как на странице канала).

## Скрипты в `scripts/`

| Файл | Назначение |
|------|------------|
| `export_channel.exe` | Шаг 1: экспорт списка видео канала в TXT/XLSX с кэшем и столбцом плейлиста |
| `organize_by_playlists.exe` | Раскладывает видео из указанной папки по вложенным папкам плейлистов канала и (по желанию) нумерует файлы так, чтобы сортировка по имени совпадала с порядком на YouTube |

Исходники: `src/export_channel.py`, `src/organize_by_playlists.py`.  
Зависимости Python: `pip install -r src/requirements.txt`  
Сборка exe: `python src/build_exe.py` (нужен `pyinstaller`).

Требование: в `PATH` должен быть доступен `yt-dlp` (для столбца плейлиста, обязателен).  
Разрешение `@handle` → `UC…` выполняется через страницу канала (без yt-dlp).

При каждой загрузке страницы browse API в консоль выводится строка вида  
`Page N: total X (+Y new, Z items from API)` — и при smart refresh, и при `--new`, и при `--refresh`.

---

## `export_channel.exe`

### Минимальный запуск

```bat
scripts\export_channel.exe @Ekaterina_Schulmann
```

или:

```bat
python src\export_channel.py @Ekaterina_Schulmann
```

**Первый параметр (обязательный):** `UC…`, `@handle` или URL канала.  
**Второй параметр (необязательный):** имя подпапки в `_channels/`.  
Если не указан — формируется из `@handle` (`@Ekaterina_Schulmann` → `_Ekaterina_Schulmann`).

Выходные файлы: `_channels/<folder>/<handle>.txt` и `.xlsx`  
(для частичного диапазона — суффикс `_FROM_TO`; имена файлов без ведущего `_`).

### Столбцы XLSX

| Col | Поле |
|-----|------|
| 1 | № на канале (1 = новейшее) |
| 2 | Имя канала |
| 3 | **Плейлист** (обязательный столбец; кэшируется в `cache/*.json`) |
| 4 | URL |
| 5 | Дата |
| 6 | Длительность |
| 7 | Заголовок |

### Кэш

Файл `cache/<channel_id>.json` — список видео канала (pretty-printed JSON).  
При повторных запусках по умолчанию используется **умное постраничное обновление**:
проверяется только первая страница (~30 видео); дальнейшие страницы подгружаются ровно
до `--to`. В кэше сохраняются `count`, `pages_fetched` и `continuation_token`, чтобы при
следующем расширении диапазона не перезапрашивать уже загруженные страницы.

YouTube отдаёт **не ровно 30** элементов на страницу (часто 28–29) — это нормально;
номера видео на канале при этом не «сдвигаются».

| Режим | Поведение |
|-------|-----------|
| *(по умолчанию)* | Умное обновление + `--from` / `--to` |
| `--new` | Только новые видео с прошлого экспорта; с `--from`/`--to` — объединение с диапазоном |
| `--refresh` | Полная перезагрузка списка с YouTube, затем `--from` / `--to` |
| нет кэша | `--new` / `--refresh` игнорируются |

### Параметры

| Параметр | Описание |
|----------|----------|
| `channel` | **Обязательный.** `UC…`, `@handle` или URL |
| `output` | Имя подпапки в `_channels/` (обычно не нужно) |
| `--from N` | Первый номер на канале (default: 1) |
| `--to N` | Последний номер включительно (default: 10000) |
| `--new` | Только новые видео с прошлого раза |
| `--refresh` | Игнорировать кэш, загрузить всё заново |
| `--output-dir`, `--workspace`, `--cookies-from-browser`, `--yt-dlp` | Как у `organize_by_playlists` |

### Примеры

```bat
:: Новый канал по @handle, папка _channels/_Ekaterina_Schulmann/
scripts\export_channel.exe @Ekaterina_Schulmann

:: Только новые видео с прошлого экспорта
scripts\export_channel.exe @Ekaterina_Schulmann --new

:: Новые + видео 10–20 из кэша
scripts\export_channel.exe @Ekaterina_Schulmann --new --from 10 --to 20

:: Полное обновление и экспорт первых 50
scripts\export_channel.exe UCL1rJ0ROIw9V1qFeIN0ZTZQ --refresh --to 50
```

---

## `organize_by_playlists.exe`

### Минимальный запуск

Из корня рабочей папки (`yt-dlp`):

```bat
scripts\organize_by_playlists.exe _VladilenMinin
```

или через Python:

```bat
python src\organize_by_playlists.py _VladilenMinin
```

**Обязательный параметр:** имя подпапки канала в `_channels/`
(например `_VladilenMinin`; можно указать и без ведущего `_` — `VladilenMinin`).

Путь к коллекции: `_channels/<folder>/`.

### Что делает скрипт

1. Берёт все `*.mp4` / `*.mkv` / `*.webm` **в корне** `_channels/<folder>/`.
2. Определяет YouTube-канал по метаданным нескольких роликов (`yt-dlp`),
   либо использует `--channel`.
3. Загружает список плейлистов канала и состав каждого плейлиста.
4. Создаёт вложенные папки с «питоновскими» именами (`слова_через_подчёркивание`)
   и переносит туда файлы. Ролики вне плейлистов → `misc/`.
5. Для курсовых плейлистов применяет **гибридную нумерацию** (как в проекте
   Alexander_Lamkov):
   - `1.` → `01.`, `#1` → `#01`, `Урок 1.` → `Урок 01.` (с `10` и выше без изменений);
   - если номера в начале имени нет — префикс `01_`, `02_`, … по индексу в плейлисте.
6. Пишет отчёты `_organize_report.json` и `_order_renames.json` в целевую папку.

Если ролик входит в несколько плейлистов, предпочтение отдаётся более «курсовым»
и более узким подборкам (а не общим вроде «Курсы» / «Гайды»).

### Параметры

| Параметр | Описание |
|----------|----------|
| `folder` | **Обязательный.** Имя подпапки канала в `_channels/` |
| `--output-dir PATH` | Родительская папка каналов (по умолчанию `<workspace>/_channels`) |
| `--channel URL` | URL или handle канала (`https://www.youtube.com/@VladilenMinin`, `@Name`, `UC…`). Если не указан — автоопределение |
| `--workspace PATH` | Корень workspace (по умолчанию — родительская папка `src/`) |
| `--order-mode courses\|all\|none` | Нумерация: только курсовые плейлисты (по умолчанию), все, или отключить |
| `--dry-run` | Только план: ничего не перемещать и не переименовывать |
| `--cookies-from-browser BROWSER` | Пробрасывается в `yt-dlp` (`chrome`, `edge`, `firefox`), если YouTube требует вход |
| `--yt-dlp PATH` | Путь к исполняемому `yt-dlp`, если он не в `PATH` |

### Примеры

```bat
:: Автоопределение канала + раскладка + нумерация курсов
scripts\organize_by_playlists.exe _VladilenMinin

:: Явно указать канал
scripts\organize_by_playlists.exe _VladilenMinin --channel https://www.youtube.com/@VladilenMinin

:: Только посмотреть план
scripts\organize_by_playlists.exe _VladilenMinin --dry-run

:: Без переименований (только папки)
scripts\organize_by_playlists.exe _VladilenMinin --order-mode none

:: Нумерация во всех плейлистах с 2+ локальными файлами
scripts\organize_by_playlists.exe _VladilenMinin --order-mode all
```

### Замечания

- Скрипт обрабатывает только файлы **в корне** `_channels/<folder>/`; уже разложенные
  по подпапкам ролики повторно не трогает.
- Длинные названия плейлистов обрезаются (лимит пути Windows).
- При блокировке YouTube («Sign in to confirm you’re not a bot») добавьте
  `--cookies-from-browser chrome` (или другой браузер, где вы залогинены).
- Папки `_channels/` и `misc/` исключены из git (см. `.gitignore`).
