# Инвентарь корпуса базы знаний

Все файлы корпуса (без служебных `_*.md`). Размер — в символах.

| Файл | Заголовок | Источник | Происхождение | Размер |
|---|---|---|---|---|
| code--avtozakaz.md | Автозаказ: рекомендации к заказу, уровень сервиса, страховой запас и выгрузка для 1С | code | backend:src/api/routers/inference.py (order-recommendations), docs/design/cabinet-v2/README.md | 2418 |
| code--datasety.md | Датасеты: именованные наборы данных, версии, докладка файлов и правило «новый файл побеждает» | code | backend:src/api/routers/datasets.py, src/datasets/merge.py | 2440 |
| code--nastrojki-modeli.md | Настройки модели: что можно менять на каждом тарифе | code | backend:src/plans/plans.py (_START_CONFIG_KEYS), src/features/external_regressors_ru.py | 1834 |
| code--oshibki-podgotovki-fajla.md | Сообщения об ошибках подготовки файла: что означают и как исправить | code | backend:src/storage/upload_pipeline.py | 2614 |
| code--prognozy-mekhanika.md | Как устроены прогнозы: автоматический расчёт после обучения, минимум истории, лимиты запросов | code | backend:src/api/routers/inference.py | 2099 |
| code--razdely-kabineta.md | Разделы кабинета Sprosly: Обзор, Данные, Прогнозы, Автозаказ, Настройки | code | backend:docs/design/cabinet-v2/README.md | 2502 |
| code--tarify-i-limity.md | Тарифы Sprosly: лимиты, модели Notus/Aether/Chronos, что входит в каждый план | code | backend:src/plans/plans.py | 2964 |
| code--uvedomleniya-i-pisma.md | Какие письма и уведомления присылает Sprosly и как ими управлять | code | backend:src/notifications/, src/auth/email_sender.py | 2893 |
| code--zagruzka-tehnicheskie-trebovaniya.md | Технические требования к загрузке файлов: форматы, размер, статусы обработки | code | backend:src/storage/upload_pipeline.py, src/api/uploads.py | 2271 |
| general--bezopasnost-i-vashi-dannye.md | Ваши данные в Sprosly: хранение, изоляция, экспорт и удаление | general | backend:src/api/routers/clients.py (export/close), src/pipeline/pii_purge.py, help:bezopasnost-dannyh | 2097 |
| general--chto-takoe-sprosly.md | Sprosly: что это, какие задачи решает и чем отличается подход | general | frontend:LandingPage.tsx + backend:src/plans/plans.py + docs/design/cabinet-v2 | 2400 |
| general--domeny-i-dostup.md | Адреса Sprosly: сайт, кабинет, новости, база знаний — и как устроен вход | general | frontend:src/shared/hostRouting.ts, backend:src/auth/* | 1171 |
| general--glossarij.md | Глоссарий Sprosly: все термины простыми словами | general | объединение 13 заметок + фронтенд/бэкенд (см. front-matter) | 28795 |
| general--kak-chto-proishodit-posle-zagruzki.md | Что происходит с файлом после загрузки: статусы | general | backend:src/storage/upload_pipeline.py | 1169 |
| general--kak-dokladka-i-pereobuchenie.md | Как докладывать свежие данные и переобучать модель | general | backend:src/datasets/merge.py, datasets.py | 966 |
| general--kak-nastrojka-urovnya-servisa.md | Как настроить уровень сервиса | general | backend:inference.py (order-recommendations), docs/design/cabinet-v2 | 766 |
| general--kak-prosmotr-prognoza.md | Как посмотреть прогноз | general | docs/design/cabinet-v2 (screen_forecast), backend:inference.py | 872 |
| general--kak-registraciya-i-vhod.md | Как зарегистрироваться и войти | general | backend:src/auth/*, frontend:Signup/Login pages | 902 |
| general--kak-sozdanie-dataseta-i-zagruzka-csv.md | Как создать датасет и загрузить первый файл | general | backend:src/api/routers/datasets.py, src/api/uploads.py | 1038 |
| general--kak-vygruzka-zakaza-1c.md | Как выгрузить заказ для 1С | general | backend:inference.py (order-recommendations CSV) | 806 |
| general--kak-zapusk-obucheniya-i-statusy.md | Как запустить обучение и что значат статусы | general | backend:src/api/routers/datasets.py, src/storage/training_runs.py | 1191 |
| general--oshibki-obucheniya-i-limitov.md | Ошибки при обучении и лимитах: что означают и что делать | general | backend:src/api/routers/datasets.py, src/plans/quota.py, inference.py | 2078 |
| help--account-security.md | Безопасность аккаунта: пароль, сессии, API-ключ, что мы храним и как удалить данные | help | db:help_articles/account-security | 5001 |
| help--bezopasnost-dannyh.md | Безопасность ваших данных | help | db:help_articles/bezopasnost-dannyh | 894 |
| help--csv-format.md | Формат CSV: обязательные колонки date/sku/sales, опциональные price/promo/stock | help | db:help_articles/csv-format | 3825 |
| help--data-processing.md | Что происходит с файлом после загрузки: антивирус-скан → кнопка «Подготовить» → автоматическое сопоставление колонок → предпросмотр | help | db:help_articles/data-processing | 4085 |
| help--first-data-upload.md | Первая загрузка данных: пошагово | help | db:help_articles/first-data-upload | 3354 |
| help--forecast-imperfection.md | Почему прогноз по дням никогда не «угадывает» точно — и почему это нормально | help | db:help_articles/forecast-imperfection | 4714 |
| help--history-requirements.md | Требования к истории: минимум строк, что делать при коротких рядах | help | db:help_articles/history-requirements | 4524 |
| help--how-service-works.md | Как устроен сервис: от файла продаж до прогноза | help | db:help_articles/how-service-works | 3206 |
| help--kak-chitat-prognoz.md | Как читать прогноз | help | db:help_articles/kak-chitat-prognoz | 859 |
| help--limity-obucheniya.md | Лимиты обучения по тарифам | help | db:help_articles/limity-obucheniya | 720 |
| help--metriki-kachestva.md | Как мы измеряем точность | help | db:help_articles/metriki-kachestva | 1036 |
| help--model-training.md | Обучение модели: когда нажимать «Обучить», что такое версии данных, гейт качества (новая модель публикуется только если не хуже) | help | db:help_articles/model-training | 5713 |
| help--obnovlenie-dannyh.md | Как обновлять данные | help | db:help_articles/obnovlenie-dannyh | 697 |
| help--order-accuracy.md | «Точность для заказа» и дневная точность: почему заказ на период точнее | help | db:help_articles/order-accuracy | 3698 |
| help--pervye-shagi.md | С чего начать: путь до первого прогноза | help | db:help_articles/pervye-shagi | 908 |
| help--podgotovka-dannyh.md | Подготовка данных: как это работает | help | db:help_articles/podgotovka-dannyh | 1017 |
| help--reading-forecast.md | Как читать прогноз: точечное значение, диапазон p10–p90, что значит «сумма за 7/14 дней | help | db:help_articles/reading-forecast | 3903 |
| help--registration-login.md | Регистрация и вход: пароль, подтверждение почты кодом, сброс по ссылке | help | db:help_articles/registration-login | 2803 |
| help--tipovye-oshibki-zagruzki.md | Типовые ошибки в файле данных | help | db:help_articles/tipovye-oshibki-zagruzki | 876 |
| help--zagruzka-dannyh.md | Как загрузить данные о продажах | help | db:help_articles/zagruzka-dannyh | 737 |
| help--zapusk-obucheniya.md | Запуск обучения модели | help | db:help_articles/zapusk-obucheniya | 894 |
| news--news-section-launch.md | В сервисе появился раздел «Новости» | news | db:news_posts/news-section-launch | 483 |
| ui--o-servise-sprosly.md | Что такое Sprosly и кому он подходит | ui | frontend:src/pages/LandingPage.tsx | 1657 |
| ui--smena-tarifa-faq.md | Смена тарифа: как перейти, что меняется, частые вопросы | ui | frontend:src/pages/PlansPage.tsx, src/pages/client/UpgradePage.tsx | 1494 |

## Исключено из корпуса

| Единица | Причина |
|---|---|
| help: `pricing-tiers` («Чем отличаются тарифы…») | Противоречит src/plans/plans.py почти во всех числах; заменена файлом code--tarify-i-limity.md. См. _gaps.md |
| news: `test-test` («Моя проверка») | Тестовый пост без содержательного текста — шум для поиска |

## Правки при порте (расхождения первоисточника с фактическим поведением продукта)

Правки перечислены в _gaps.md (раздел «Обнаруженные противоречия»); затронуты: help--data-processing, help--first-data-upload, help--how-service-works, help--model-training, help--account-security, help--registration-login, help--reading-forecast, help--history-requirements, help--csv-format.
