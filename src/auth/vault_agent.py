"""
src/auth/vault_agent.py

**Имя файла историческое.** В early phase (2026-04) модуль писался
под HashiCorp Vault; 2026-05-06 проект мигрировал на **Yandex
Lockbox**, а имя файла сохранили чтобы не ломать ~30 callers по
коду. По функции это **Lockbox client**.

`bootstrap_secrets()`:

  1. Читает SA-key из `YC_SA_KEY_FILE` (по умолчанию
     `/run/secrets/yc-sa-key.json`).
  2. Подписывает JWT-PS256 → обменивает на IAM token через
     `iam.api.cloud.yandex.net`.
  3. Тянет payload из Lockbox secret (`YC_LOCKBOX_SECRET_ID`).
  4. Подкладывает entries в `os.environ` — preserve-env-wins
     (тесты и dev могут override через монypatch / .env).

Параллельный путь для non-Python контейнеров — это
`scripts/lockbox_bootstrap.sh` (тот же JWT-PS256 → IAM → Lockbox
chain без Python; используется в backup, alertmanager — см.
`project_lockbox_bootstrap.md` в agent memory).

HashiCorp Vault конфиг (vault/, docker-compose.vault.yml,
scripts/vault_*.sh) удалён из репо в 2026-05-20 cleanup-коммите.
Архивный reference на self-hosted Vault — `deploy/DEPLOY.md` §4.2.

Dev fallback: при отсутствии SA-ключа функция log'ает warning и
возвращает False — секреты ожидаются из `.env` (локальная
разработка) или из тестовых фикстур (CI).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from threading import Event, Thread
from typing import Optional

logger = logging.getLogger(__name__)

_RENEW_BEFORE_EXPIRY = 300   # обновить токен за 5 мин до истечения
_SECRETS_FILE        = "/vault/secrets/app.env"   # tmpfs от Vault Agent


# ── Источник 1: Vault Agent файл (production) ─────────────────

def load_from_vault_agent_file(path: str = _SECRETS_FILE) -> dict[str, str]:
    """
    Читает секреты из файла который Vault Agent рендерит в tmpfs.
    Файл существует только в RAM — при остановке контейнера исчезает.
    Формат: KEY=value (одна строка на секрет).
    """
    secrets_path = Path(path)
    if not secrets_path.exists():
        return {}

    secrets: dict[str, str] = {}
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = value.strip()

    if secrets:
        logger.info(
            f"Vault Agent file: loaded {len(secrets)} secrets from {path} (tmpfs)"
        )
    return secrets


# ── Источник 2: AppRole через переменные окружения ────────────

class VaultAppRoleAuth:
    """
    Аутентификация через AppRole.

    role_id  — запечён в Docker образ при сборке (не секрет, не меняется)
    secret_id — одноразовый, инжектируется CI/CD при каждом деплое (TTL 10 мин)

    После успешной аутентификации secret_id уничтожается из памяти.
    Токен хранится только в RAM, авто-обновляется.
    """

    def __init__(self, vault_addr: str, role_id: str, secret_id: str):
        self.vault_addr = vault_addr.rstrip("/")
        self.role_id    = role_id
        self._secret_id = secret_id   # уничтожим после auth
        self._token:    Optional[str] = None
        self._ttl:      int           = 0
        self._renew_at: float         = 0.0
        self._stop      = Event()
        # R4-9 — held so external watchdog / health check can call
        # `auth._renew_thread.is_alive()`. Set in start_auto_renew.
        self._renew_thread: Optional[Thread] = None

    def authenticate(self) -> str:
        """AppRole login → кратковременный токен в RAM."""
        try:
            import hvac
            client = hvac.Client(url=self.vault_addr)
            resp   = client.auth.approle.login(
                role_id   = self.role_id,
                secret_id = self._secret_id,
            )
            self._token    = resp["auth"]["client_token"]
            self._ttl      = resp["auth"]["lease_duration"]
            self._renew_at = time.time() + self._ttl - _RENEW_BEFORE_EXPIRY

            # Уничтожить secret_id из памяти сразу после использования
            self._secret_id = ""

            logger.info(
                f"Vault AppRole: authenticated, token TTL={self._ttl}s"
            )
            return self._token

        except ImportError:
            raise RuntimeError("Установите hvac: pip install hvac")
        except Exception as e:
            raise RuntimeError(f"Vault AppRole auth failed: {e}") from e

    def start_auto_renew(self) -> None:
        """Фоновый поток обновляет токен до истечения.

        Audit R4-9 — outer try/except + retained thread reference. The
        inner per-iteration try/except catches renew failures, but
        anything raised BEFORE the while loop (an import failure,
        attribute miss on self._stop / self._token after a stop+restart
        race, OOM) used to kill the thread silently. Without a held
        reference, the dead thread couldn't be observed; once dead, the
        Vault token would expire and every secret read would fail with
        no operator-visible signal. Now an outer except logs at ERROR
        with stack — Loki picks it up; `self._renew_thread.is_alive()`
        is queryable externally for a watchdog.
        """
        def _loop():
            try:
                while not self._stop.is_set():
                    if time.time() >= self._renew_at and self._token:
                        try:
                            import hvac
                            c    = hvac.Client(url=self.vault_addr, token=self._token)
                            resp = c.auth.renew_self()
                            self._ttl      = resp["auth"]["lease_duration"]
                            self._renew_at = time.time() + self._ttl - _RENEW_BEFORE_EXPIRY
                            logger.info(f"Vault token renewed, new TTL={self._ttl}s")
                        except Exception as e:
                            logger.warning(f"Vault token renew failed: {e}")
                    self._stop.wait(timeout=60)
            except Exception as e:
                # R4-9 — surface unexpected thread death so the
                # observability stack can alert (`vault-renew` thread
                # absence implies secrets will start failing in ~TTL).
                logger.error(
                    "Vault renew thread died unexpectedly: %r", e, exc_info=True,
                )

        self._renew_thread = Thread(target=_loop, daemon=True, name="vault-renew")
        self._renew_thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def token(self) -> Optional[str]:
        return self._token


class VaultKVSecrets:
    """Читает секреты из Vault KV v2 → инжектирует в os.environ."""

    def __init__(self, vault_addr: str, token: str, kv_path: str):
        self.vault_addr = vault_addr
        self._token     = token
        self.kv_path    = kv_path

    def load_and_inject(self) -> int:
        """
        Загружает все секреты из Vault KV v2 в os.environ.
        Возвращает количество инжектированных переменных.
        """
        try:
            import hvac
            client = hvac.Client(url=self.vault_addr, token=self._token)
            resp   = client.secrets.kv.v2.read_secret_version(
                path        = self.kv_path.replace("secret/data/", ""),
                mount_point = "secret",
            )
            data    = resp["data"]["data"]
            count   = 0
            for key, value in data.items():
                env_key = key.upper()
                if not os.environ.get(env_key):   # не перезаписывать явно заданные
                    os.environ[env_key] = str(value)
                    count += 1

            logger.info(f"Vault KV: injected {count} secrets into os.environ (RAM only)")
            return count

        except Exception as e:
            logger.error(f"Vault KV load failed: {e}")
            return 0

    def get_dynamic_db_url(
        self,
        role:        str = "sku-forecasting-role",
        mount_point: str = "database",
    ) -> Optional[str]:
        """
        Получает динамические credentials PostgreSQL (TTL=1h).
        Каждый вызов создаёт нового пользователя в БД.
        """
        try:
            import hvac
            client = hvac.Client(url=self.vault_addr, token=self._token)
            resp   = client.secrets.database.generate_credentials(
                name=role, mount_point=mount_point
            )
            user     = resp["data"]["username"]
            password = resp["data"]["password"]
            ttl      = resp["lease_duration"]
            host     = os.environ.get("DB_HOST", "postgres")
            port     = os.environ.get("DB_PORT", "5432")
            dbname   = os.environ.get("DB_NAME", "sku_db")
            db_url   = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
            logger.info(f"Vault DB: dynamic credentials issued, TTL={ttl}s")
            return db_url
        except Exception as e:
            logger.debug(f"Vault dynamic DB creds not available: {e}")
            return None


# ── Главная функция bootstrap ─────────────────────────────────

def bootstrap_secrets() -> bool:
    """
    Загружает секреты из доступного источника.
    Вызывается ОДИН РАЗ при старте приложения, ДО любого чтения конфига.

    Порядок:
      0. Yandex Lockbox            (если YC_SA_KEY_FILE + YC_LOCKBOX_SECRET_ID)
      1. /vault/secrets/app.env    (Vault Agent sidecar, production)
      2. AppRole через env vars    (Docker без sidecar)
      3. VAULT_TOKEN static        (Level 2, переходный)
      4. Env vars as-is            (dev/test, .env через docker)

    Lockbox идёт первым — он самый простой и работает в YC-экосистеме
    без отдельного Vault. Если и Lockbox, и Vault сконфигурены, Vault
    не будет пройден (Lockbox достаточно).
    """
    # ── Источник 0: Yandex Lockbox ────────────────────────────
    try:
        from src.auth.lockbox_agent import load_from_lockbox
        if load_from_lockbox():
            logger.info("Bootstrap: secrets from Yandex Lockbox")
            _apply_db_host_override()
            return True
    except ImportError:
        pass       # lockbox_agent not deployed
    except Exception as e:       # noqa: BLE001 — includes LockboxError
        logger.warning("Lockbox bootstrap failed, falling back: %s", e)

    # ── Источник 1: Vault Agent файл (tmpfs) ──────────────────
    agent_secrets = load_from_vault_agent_file()
    if agent_secrets:
        for key, value in agent_secrets.items():
            if not os.environ.get(key):
                os.environ[key] = value
        logger.info("Bootstrap: secrets from Vault Agent tmpfs file (Level 3)")
        return True

    # ── Источник 2: AppRole (role_id + secret_id) ─────────────
    vault_addr = os.environ.get("VAULT_ADDR")
    role_id    = os.environ.get("VAULT_ROLE_ID")
    secret_id  = os.environ.get("VAULT_SECRET_ID")

    if vault_addr and role_id and secret_id:
        try:
            auth = VaultAppRoleAuth(vault_addr, role_id, secret_id)
            token = auth.authenticate()
            auth.start_auto_renew()

            kv_path  = os.environ.get("VAULT_PATH", "sku-forecasting")
            kv       = VaultKVSecrets(vault_addr, token, kv_path)
            kv.load_and_inject()

            # Динамические DB credentials
            db_url = kv.get_dynamic_db_url()
            if db_url:
                os.environ["DATABASE_URL"] = db_url

            # Удалить secret_id из env (одноразовый)
            os.environ.pop("VAULT_SECRET_ID", None)
            logger.info("Bootstrap: secrets from Vault AppRole (Level 3 via env)")
            return True

        except Exception as e:
            logger.warning(f"Bootstrap AppRole failed: {e}")

    # ── Источник 3: Static Vault token (Level 2) ──────────────
    static_token = os.environ.get("VAULT_TOKEN")
    if vault_addr and static_token:
        try:
            kv = VaultKVSecrets(vault_addr, static_token, "sku-forecasting")
            n  = kv.load_and_inject()
            if n > 0:
                logger.info(f"Bootstrap: {n} secrets from Vault static token (Level 2)")
                return True
        except Exception as e:
            logger.warning(f"Bootstrap static token failed: {e}")

    # ── Источник 4: env vars as-is (Level 1 / dev) ────────────
    has_jwt = bool(os.environ.get("JWT_SECRET_KEY"))
    has_api = bool(os.environ.get("API_KEY"))
    if has_jwt and has_api:
        logger.info("Bootstrap: using environment variables directly (Level 1 / dev)")
        return True

    env = os.environ.get("APP_ENV", "production")
    if env in ("development", "dev", "test"):
        logger.warning(
            "Bootstrap: no secrets configured — dev mode defaults active"
        )
        return False

    # Production без секретов — критическая ошибка
    raise RuntimeError(
        "FATAL: No secrets configured for production!\n"
        "Configure one of:\n"
        "  Level 3: Vault Agent sidecar → /vault/secrets/app.env\n"
        "  Level 3: VAULT_ADDR + VAULT_ROLE_ID + VAULT_SECRET_ID\n"
        "  Level 2: VAULT_ADDR + VAULT_TOKEN\n"
        "  Level 1: JWT_SECRET_KEY + API_KEY (dev only)\n"
        "See README.md section 'Безопасность: три уровня'"
    )


# ── DB host override (PgBouncer cutover, #186) ───────────────
def _apply_db_host_override() -> None:
    """
    Rewrite DATABASE_URL's host:port if DB_HOST_OVERRIDE is set in env.

    Used to route a specific service through PgBouncer
    (DB_HOST_OVERRIDE=pgbouncer:6432) without modifying the Lockbox
    DATABASE_URL value, which is shared across all services. Per-service
    cutover is wired in compose env. If unset → no-op.
    """
    override = os.environ.get("DB_HOST_OVERRIDE", "").strip()
    if not override:
        return
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        logger.warning("DB_HOST_OVERRIDE=%s but DATABASE_URL is empty — no-op", override)
        return
    try:
        from urllib.parse import urlparse, urlunparse
        parts = urlparse(db_url)
        # urlparse keeps username/password percent-encoded inside netloc,
        # so we preserve them as-is by splitting netloc on '@'. parts.hostname
        # is for logging only.
        userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else ""
        netloc = f"{userinfo}@{override}" if userinfo else override
        new_url = urlunparse(parts._replace(netloc=netloc))
        os.environ["DATABASE_URL"] = new_url
        logger.info("DATABASE_URL host overridden: %s → %s", parts.hostname, override)
    except Exception as e:           # noqa: BLE001
        logger.error("DB_HOST_OVERRIDE rewrite failed: %s — leaving DATABASE_URL unchanged", e)


# ── Singleton guard ───────────────────────────────────────────
_bootstrapped = False


def ensure_bootstrapped() -> None:
    """Идемпотентный вызов bootstrap — выполняется только один раз."""
    global _bootstrapped
    if not _bootstrapped:
        bootstrap_secrets()
        _bootstrapped = True
