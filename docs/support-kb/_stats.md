# Статистика корпуса

- Файлов знаний: **47** (+3 служебных: _inventory.md, _gaps.md, _stats.md)
- Суммарный объём: **124007 символов** (~124 тыс.)
- По источникам: code — 9, general — 13, help — 21, news — 1, ui — 2

## Покрытие по блокам

| Блок | Файлов |
|---|---|
| Опубликованная Помощь (help) | 21 |
| Новости (news) | 1 |
| Из кода продукта (code) | 9 |
| Из интерфейса (ui) | 2 |
| Общие сведения/глоссарий/сценарии (general) | 13 |

## Покрытие по категориям

| Категория | Файлов | Файлы |
|---|---|---|
| Аккаунт и безопасность | 7 | code--tarify-i-limity.md, code--uvedomleniya-i-pisma.md, general--bezopasnost-i-vashi-dannye.md, help--account-security.md, help--bezopasnost-dannyh.md, help--limity-obucheniya.md, ui--smena-tarifa-faq.md |
| Глоссарий | 13 | general--glossarij-dataset.md, general--glossarij-gorizont-prognoza.md, general--glossarij-naivnyj-prognoz.md, general--glossarij-obuchenie-modeli.md, general--glossarij-p10-p90.md, general--glossarij-podgotovka-dannyh.md, general--glossarij-rekomenduemyj-zakaz.md, general--glossarij-sku.md, general--glossarij-sliyanie-fajlov.md, general--glossarij-strahovoj-zapas.md, general--glossarij-tochnost-dlya-zakaza.md, general--glossarij-uroven-servisa.md, general--glossarij-versiya-dannyh.md |
| Данные | 9 | code--datasety.md, code--oshibki-podgotovki-fajla.md, code--zagruzka-tehnicheskie-trebovaniya.md, help--csv-format.md, help--data-processing.md, help--history-requirements.md, help--obnovlenie-dannyh.md, help--podgotovka-dannyh.md, help--tipovye-oshibki-zagruzki.md |
| Начало работы | 8 | code--razdely-kabineta.md, help--first-data-upload.md, help--how-service-works.md, help--pervye-shagi.md, help--registration-login.md, help--zagruzka-dannyh.md, help--zapusk-obucheniya.md, ui--o-servise-sprosly.md |
| Новости | 1 | news--news-section-launch.md |
| О продукте | 2 | general--chto-takoe-sprosly.md, general--domeny-i-dostup.md |
| Обучение и прогнозы | 10 | code--avtozakaz.md, code--nastrojki-modeli.md, code--prognozy-mekhanika.md, general--oshibki-obucheniya-i-limitov.md, help--forecast-imperfection.md, help--kak-chitat-prognoz.md, help--metriki-kachestva.md, help--model-training.md, help--order-accuracy.md, help--reading-forecast.md |
| Сценарии | 8 | general--kak-chto-proishodit-posle-zagruzki.md, general--kak-dokladka-i-pereobuchenie.md, general--kak-nastrojka-urovnya-servisa.md, general--kak-prosmotr-prognoza.md, general--kak-registraciya-i-vhod.md, general--kak-sozdanie-dataseta-i-zagruzka-csv.md, general--kak-vygruzka-zakaza-1c.md, general--kak-zapusk-obucheniya-i-statusy.md |

## Замечания

- Глоссарий: 13 терминов, каждый отдельным файлом (удобно для RAG-чанков).
- Сценарии: 8 пошаговых инструкций по фактическому поведению продукта.
- Слабее всего покрыты: оплата/биллинг, публичная API-документация, интеграции (см. _gaps.md).
- Все числа и лимиты выверены по src/plans/plans.py, src/storage/upload_pipeline.py, src/api/routers/*, src/auth/*, src/notifications/*, src/pipeline/pii_purge.py и фронтенду по состоянию на 2026-07-19.
