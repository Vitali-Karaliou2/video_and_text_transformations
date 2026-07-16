# yt-dlp channel tools

Утилиты для работы с коллекциями видео, скачанными с YouTube-каналов
(`yt-dlp` сохраняет файлы вида `Название [VIDEO_ID].mp4`).

## Скрипты в `scripts/`

| Файл | Назначение |
|------|------------|
| `organize_by_playlists.exe` | Раскладывает видео из указанной папки по вложенным папкам плейлистов канала и (по желанию) нумерует файлы так, чтобы сортировка по имени совпадала с порядком на YouTube |

Исходники: `src/organize_by_playlists.py`.  
Сборка exe: `python src/build_exe.py` (нужны Python и пакет `pyinstaller`).

Требование: в `PATH` должен быть доступен `yt-dlp`.

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

**Обязательный параметр:** имя вложенной папки с видео одного канала
(например `Vladilen_Minin`, `Alexander_Lamkov`).

### Что делает скрипт

1. Берёт все `*.mp4` / `*.mkv` / `*.webm` **в корне** указанной папки.
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
| `folder` | **Обязательный.** Имя папки под корневой директорией workspace |
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

- Скрипт обрабатывает только файлы **в корне** целевой папки; уже разложенные
  по подпапкам ролики повторно не трогает.
- Длинные названия плейлистов обрезаются (лимит пути Windows).
- При блокировке YouTube («Sign in to confirm you’re not a bot») добавьте
  `--cookies-from-browser chrome` (или другой браузер, где вы залогинены).
