# customsrs — объединённые sing-box rule-set файлы

Автоматическая сборка объединённых `.srs` файлов для sing-box (и Podkop на OpenWrt).  
Вместо нескольких ссылок на отдельные rule-set — одна ссылка, в которой собраны все нужные правила.

## Готовые ссылки для Podkop

Вставьте эти URL в поля **«Внешние списки доменов»** и **«Внешние списки подсетей»** соответственно:

| Тип     | URL |
|---------|-----|
| geosite | `https://raw.githubusercontent.com/<YOUR_USER>/customsrs/main/dist/geosite-merged.srs` |
| geoip   | `https://raw.githubusercontent.com/<YOUR_USER>/customsrs/main/dist/geoip-merged.srs`   |

> Замените `<YOUR_USER>` на ваш GitHub-юзернейм (или организацию).

Ссылки стабильны — при каждой пересборке обновляется только содержимое файлов, пути не меняются.

## Как это работает

1. В `sources/geosite.yaml` и `sources/geoip.yaml` перечислены URL исходных `.srs` файлов.
2. GitHub Actions скачивает все источники, декомпилирует каждый в JSON, мержит правила с дедупликацией, компилирует обратно в `.srs`.
3. Результат коммитится в `dist/` в ветку `main`.

## Как добавить новый источник

Откройте нужный файл (`sources/geosite.yaml` или `sources/geoip.yaml`) и добавьте запись:

```yaml
  - name: имя-для-лога
    url: https://raw.githubusercontent.com/.../something.srs
```

Закоммитьте и запушьте в `main` — сборка запустится автоматически.

## Как убрать источник

Удалите соответствующую запись из YAML-файла, закоммитьте, запушьте.

## Как принудительно пересобрать

Без изменения конфига — зайдите на вкладку **Actions** в репозитории, выберите workflow **Build merged rule-sets**, нажмите **Run workflow**.

Также сборка запускается автоматически раз в сутки (04:00 UTC), чтобы подхватывать обновления upstream-источников.

## Локальная сборка

### Зависимости

- Python 3.10+
- PyYAML (`pip install -r requirements.txt`)
- sing-box CLI (скрипт скачает автоматически, или можно поставить вручную)

### Запуск

```bash
# Установить зависимости
pip install -r requirements.txt

# Запустить сборку (sing-box скачается автоматически)
python scripts/build.py

# Или указать путь к уже установленному sing-box
python scripts/build.py --sing-box-path /usr/local/bin/sing-box

# Зафиксировать конкретную версию sing-box через переменную окружения
SING_BOX_VERSION=1.11.0 python scripts/build.py
```

Результат будет в `dist/`.

### Тесты

```bash
cd scripts
python -m pytest test_build.py -v
```

## Структура репозитория

```
sources/
  geosite.yaml     — список URL для geosite rule-sets
  geoip.yaml       — список URL для geoip rule-sets
scripts/
  build.py         — скрипт сборки
  test_build.py    — тесты merge-логики
dist/
  geosite-merged.srs  — итоговый файл (генерируется)
  geoip-merged.srs    — итоговый файл (генерируется)
  manifest.json       — метаданные сборки (генерируется)
  README.md           — отчёт о сборке (генерируется)
.github/workflows/
  build.yml        — CI workflow
```

## Что если источник недоступен?

Скрипт делает до 3 попыток скачивания каждого источника. Если источник всё равно не отвечает — он пропускается (с предупреждением), остальные источники собираются нормально. Информация о неудавшихся загрузках видна в:
- логе GitHub Actions
- `dist/manifest.json` (поле `failed_sources`)
- `dist/README.md`
- Job Summary на вкладке Actions
