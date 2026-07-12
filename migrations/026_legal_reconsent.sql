-- LEG-3 #431 (инкремент 1): версионный сигнал повторного согласия.
-- reconsent_required_since — номер версии, начиная с которой принятие
-- ниже этой версии требует пере-согласия (редакционные правки поверх
-- НЕ затирают сигнал — колонка обновляется только при явном флаге).
-- change_summary — краткое «что изменилось» для будущей модалки.
ALTER TABLE legal_documents
    ADD COLUMN IF NOT EXISTS reconsent_required_since INTEGER,
    ADD COLUMN IF NOT EXISTS change_summary TEXT;
