# yt-dlp channel tools

Утилиты для работы с коллекциями видео, скачанными с YouTube-каналов
(`yt-dlp` сохраняет файлы вида `Название [VIDEO_ID].mp4`).

## Структура проекта

```
yt-dlp/
  cache/               ← JSON-кэш списков видео каналов (не в git)
  output/              ← все скачанные коллекции (не в git)
    Vladilen_Minin/
    Ekaterina_Schulmann/
    ...
  src/                 ← исходники Python
  scripts/             ← готовые exe для запуска
  Prompts/             ← промпты и заметки (не output)
  readme.md
```

## Пайплайн

1. **`export_channel`** — аннотации канала в `.txt` / `.xlsx` (+ столбец плейлиста)
2. *(скачивание видео — следующий шаг пайплайна)*
3. **`organize_by_playlists`** — раскладка скачанных файлов по плейлистам

Каждая подпапка в `output/` — одна коллекция канала или тематическая выборка.

## Скрипты в `scripts/`

| Файл | Назначение |
|------|------------|
| `export_channel.exe` | Шаг 1: экспорт списка видео канала в TXT/XLSX с кэшем и столбцом плейлиста |
| `organize_by_playlists.exe` | Раскладывает видео из указанной папки по вложенным папкам плейлистов канала и (по желанию) нумерует файлы так, чтобы сортировка по имени совпадала с порядком на YouTube |

Исходники: `src/export_channel.py`, `src/organize_by_playlists.py`.  
Зависимости Python: `pip install -r src/requirements.txt`  
Сборка exe: `python src/build_exe.py` (нужен `pyinstaller`).

Требование: в `PATH` должен быть доступен `yt-dlp` (для столбца плейлиста).  
Разрешение `@handle` → `UC…` выполняется через страницу канала (без yt-dlp).

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
**Второй параметр (необязательный):** имя подпапки в `output/`.  
Если не указан — формируется из `@handle` (`@Ekaterina_Schulmann` → `Ekaterina_Schulmann`).

Выходные файлы: `output/<folder>/<folder>.txt` и `.xlsx`  
(для частичного диапазона — суффикс `_FROM_TO`).

### Столбцы XLSX

| Col | Поле |
|-----|------|
| 1 | № на канале (1 = новейшее) |
| 2 | Имя канала |
| 3 | **Плейлист** (один лучший; та же логика приоритета, что у `organize_by_playlists`) |
| 4 | URL |
| 5 | Дата |
| 6 | Длительность |
| 7 | Заголовок |

### Кэш

Файл `cache/<channel_id>.json` — полный список видео канала (pretty-printed JSON).  
При повторных запусках по умолчанию используется **умное обновление**: проверяются первые 3 записи кэша; если совпадают с YouTube — остальной кэш считается актуальным.

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
| `output` | Имя подпапки в `output/` (обычно не нужно) |
| `--from N` | Первый номер на канале (default: 1) |
| `--to N` | Последний номер включительно (default: 10000) |
| `--new` | Только новые видео с прошлого раза |
| `--refresh` | Игнорировать кэш, загрузить всё заново |
| `--skip-playlists` | Не запрашивать плейлисты (столбец пустой) |
| `--output-dir`, `--workspace`, `--cookies-from-browser`, `--yt-dlp` | Как у `organize_by_playlists` |

### Примеры

```bat
:: Новый канал по @handle, папка output/Ekaterina_Schulmann/
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
scripts\organize_by_playlists.exe Vladilen_Minin
```

или через Python:

```bat
python src\organize_by_playlists.py Vladilen_Minin
```

**Обязательный параметр:** имя подпапки в `output/` с видео одного канала
(например `Vladilen_Minin`, `Alexander_Lamkov`).

Путь к коллекции: `output/<folder>/`.

### Что делает скрипт

1. Берёт все `*.mp4` / `*.mkv` / `*.webm` **в корне** `output/<folder>/`.
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
| `folder` | **Обязательный.** Имя подпапки в `output/` |
| `--output-dir PATH` | Родительская папка коллекций (по умолчанию `<workspace>/output`) |
| `--channel URL` | URL или handle канала (`https://www.youtube.com/@VladilenMinin`, `@Name`, `UC…`). Если не указан — автоопределение |
| `--workspace PATH` | Корень workspace (по умолчанию — родительская папка `src/`) |
| `--order-mode courses\|all\|none` | Нумерация: только курсовые плейлисты (по умолчанию), все, или отключить |
| `--dry-run` | Только план: ничего не перемещать и не переименовывать |
| `--cookies-from-browser BROWSER` | Пробрасывается в `yt-dlp` (`chrome`, `edge`, `firefox`), если YouTube требует вход |
| `--yt-dlp PATH` | Путь к исполняемому `yt-dlp`, если он не в `PATH` |

### Примеры

```bat
:: Автоопределение канала + раскладка + нумерация курсов
scripts\organize_by_playlists.exe Vladilen_Minin

:: Явно указать канал
scripts\organize_by_playlists.exe Vladilen_Minin --channel https://www.youtube.com/@VladilenMinin

:: Только посмотреть план
scripts\organize_by_playlists.exe Vladilen_Minin --dry-run

:: Без переименований (только папки)
scripts\organize_by_playlists.exe Vladilen_Minin --order-mode none

:: Нумерация во всех плейлистах с 2+ локальными файлами
scripts\organize_by_playlists.exe Vladilen_Minin --order-mode all
```

### Замечания

- Скрипт обрабатывает только файлы **в корне** `output/<folder>/`; уже разложенные
  по подпапкам ролики повторно не трогает.
- Длинные названия плейлистов обрезаются (лимит пути Windows).
- При блокировке YouTube («Sign in to confirm you’re not a bot») добавьте
  `--cookies-from-browser chrome` (или другой браузер, где вы залогинены).
- Папка `output/` целиком исключена из git (см. `.gitignore`).
