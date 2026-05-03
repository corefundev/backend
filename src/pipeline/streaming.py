"""
src/pipeline/streaming.py

Kafka streaming pipeline для near-realtime обновления признаков.

Архитектура:
  POS System → Kafka topic "sales-events"
     → SalesStreamProcessor (Kafka consumer)
     → Online Feature Store (Redis) — обновление lag/rolling признаков
     → Trigger retraining if drift detected

Текущая реализация: полный интерфейс + mock для тестирования.
Production: требует Kafka broker (docker compose включает zookeeper + kafka).

Конфиг (.env):
  KAFKA_BOOTSTRAP_SERVERS=kafka:9092
  KAFKA_SALES_TOPIC=sales-events
  KAFKA_GROUP_ID=sku-forecasting

Имеет смысл при:
  - Горизонт прогноза ≤ 7 дней
  - Flash sales / резкие изменения спроса
  - Требование обновления признаков < 5 минут
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SalesEvent:
    """One sale event from POS system."""
    sku:        str
    client_id:  str
    timestamp:  str
    quantity:   float
    price:      float
    store_id:   str = ""
    promo:      int = 0


class KafkaSalesProducer:
    """
    Sends sale events to Kafka topic.
    Falls back to in-memory queue if Kafka not available.
    """

    def __init__(
        self,
        bootstrap_servers: str | None = None,
        topic:             str = "sales-events",
    ):
        self._topic   = topic
        self._servers = bootstrap_servers or os.environ.get(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
        )
        self._producer = None
        self._queue: list[dict] = []   # in-memory fallback

        self._connect()

    def _connect(self) -> None:
        try:
            from kafka import KafkaProducer
            self._producer = KafkaProducer(
                bootstrap_servers = self._servers,
                value_serializer  = lambda v: json.dumps(v).encode("utf-8"),
                acks              = "all",
                retries           = 3,
            )
            logger.info(f"Kafka producer connected: {self._servers}")
        except ImportError:
            logger.info("kafka-python not installed — using in-memory queue")
        except Exception as e:
            logger.warning(f"Kafka unavailable ({e}) — using in-memory fallback")

    def send(self, event: SalesEvent) -> bool:
        payload = {
            "sku":       event.sku,
            "client_id": event.client_id,
            "timestamp": event.timestamp,
            "quantity":  event.quantity,
            "price":     event.price,
            "store_id":  event.store_id,
            "promo":     event.promo,
        }
        if self._producer:
            self._producer.send(self._topic, value=payload)
            return True

        # In-memory fallback
        self._queue.append(payload)
        return True

    def flush(self) -> None:
        if self._producer:
            self._producer.flush()

    @property
    def queued_events(self) -> list[dict]:
        """For testing: events in in-memory queue."""
        return self._queue.copy()


class SalesStreamProcessor:
    """
    Kafka consumer: reads sales events, updates online feature store.
    Updates Redis within < 5 minutes of the sale occurring.
    """

    def __init__(
        self,
        bootstrap_servers: str | None = None,
        topic:             str = "sales-events",
        group_id:          str = "sku-forecasting",
        feature_store=None,
    ):
        self._topic    = topic
        self._servers  = bootstrap_servers or os.environ.get(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
        )
        self._group_id = group_id
        self._fs       = feature_store
        self._running  = False
        self._consumer = None

    def _connect(self) -> bool:
        try:
            from kafka import KafkaConsumer
            self._consumer = KafkaConsumer(
                self._topic,
                bootstrap_servers = self._servers,
                group_id          = self._group_id,
                auto_offset_reset = "latest",
                value_deserializer= lambda v: json.loads(v.decode("utf-8")),
                consumer_timeout_ms = 1000,
            )
            logger.info(f"Kafka consumer connected: topic={self._topic}")
            return True
        except ImportError:
            logger.warning("kafka-python not installed. pip install kafka-python")
            return False
        except Exception as e:
            logger.warning(f"Kafka unavailable: {e}")
            return False

    def process_event(self, event: dict) -> None:
        """Update online feature store with new sale data."""
        sku       = event.get("sku")
        client_id = event.get("client_id")
        quantity  = event.get("quantity", 0)

        if not sku or not client_id:
            return

        if self._fs:
            # Read current features, update lag_1 with new quantity
            current = self._fs.online.read(client_id, sku) or {}
            current["lag_1"]  = float(quantity)
            current["lag_1_updated_at"] = event.get("timestamp", "")
            self._fs.online.write(client_id, sku, current)
            logger.debug(
                f"Streaming: updated {client_id}/{sku} lag_1={quantity}"
            )

    def run(self, max_events: int | None = None) -> int:
        """
        Start consuming. Blocks until stopped or max_events reached.
        Returns number of events processed.
        """
        if not self._connect():
            logger.warning("Cannot start stream processor — Kafka unavailable")
            return 0

        # _connect() returning truthy means _consumer is populated; assert
        # so mypy narrows the Optional and the iteration / .close() below
        # type-check cleanly.
        assert self._consumer is not None

        self._running = True
        count = 0
        logger.info("Stream processor started")

        try:
            for message in self._consumer:
                if not self._running:
                    break
                self.process_event(message.value)
                count += 1
                if max_events and count >= max_events:
                    break
        except Exception as e:
            logger.error(f"Stream processor error: {e}")
        finally:
            self._consumer.close()
            logger.info(f"Stream processor stopped: {count} events processed")

        return count

    def stop(self) -> None:
        self._running = False

    def process_from_queue(self, events: list[dict]) -> int:
        """For testing: process events from an in-memory list."""
        count = 0
        for event in events:
            self.process_event(event)
            count += 1
        return count
