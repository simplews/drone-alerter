"""Утиліта для запуску симуляції та роботи в тіньовому режимі (Shadow Mode).

Підтримує:
1. Завантаження історичних повідомлень з JSON (як спрощений формат, так і Telegram Desktop export).
2. Прогін через Normalizer та Tracker із збереженням у LearningStore.
3. Оцінку критеріїв Go/No-Go (PRD v1.1 розділ 7).
4. Звіт про поточний стан розмічених цілей та готовність до релізу.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.engine.models import AlertEvent, AlertLevel, RawPost
from src.engine.simulation import (
    EvaluationMetrics,
    ShadowModeSimulator,
    SimulationEpisode,
)
from src.engine.tracker import TargetTracker
from src.learning.reference import ActiveReferenceCache, ReferenceRepository
from src.learning.store import LearningStore, parse_utc
from src.nlp.normalizer import MessageNormalizer


def _extract_text(raw_text: Any) -> str:
    """Витягує текст з формату експорту Telegram (може бути str або list фрагментів)."""
    if isinstance(raw_text, str):
        return raw_text
    if isinstance(raw_text, list):
        parts = []
        for item in raw_text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
        return "".join(parts)
    return str(raw_text)


def parse_posts_from_json(path: Path, default_channel: str = "imported") -> list[RawPost]:
    """Парсить повідомлення з JSON-файлу."""
    data = json.loads(path.read_text(encoding="utf-8"))
    posts: list[RawPost] = []

    if isinstance(data, dict) and "messages" in data:
        # Telegram Desktop export (result.json)
        channel_name = data.get("name", default_channel)
        for msg in data["messages"]:
            if msg.get("type") != "message":
                continue
            text = _extract_text(msg.get("text", ""))
            if not text.strip():
                continue
            date_str = msg.get("date", "")
            # Telegram export зазвичай "YYYY-MM-DDTHH:MM:SS"
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            else:
                dt = dt.astimezone(UTC)

            posts.append(
                RawPost(
                    channel=channel_name,
                    message_id=int(msg.get("id", 0)),
                    posted_at_utc=dt,
                    text=text,
                )
            )
    elif isinstance(data, list):
        # Простий список об'єктів поста
        for item in data:
            dt = parse_utc(item["posted_at_utc"])
            posts.append(
                RawPost(
                    channel=item.get("channel", default_channel),
                    message_id=int(item.get("message_id", 0)),
                    posted_at_utc=dt,
                    text=item.get("text", ""),
                )
            )
    else:
        raise ValueError(f"Невідомий формат JSON у {path}")

    # Сортування за часом публікації
    posts.sort(key=lambda p: p.posted_at_utc)
    return posts


def run_historical_replay(
    posts: list[RawPost],
    ref_cache: ActiveReferenceCache,
    store: LearningStore | None = None,
) -> tuple[list[AlertEvent], TargetTracker]:
    """Проганяє історичний потік повідомлень через нормалізатор і трекер."""
    tracker = TargetTracker(ref_cache, store=store)
    normalizer = MessageNormalizer(ref_cache)
    all_alerts: list[AlertEvent] = []

    for post in posts:
        parsed = normalizer.parse(post)
        alerts = tracker.process_message(post, parsed, now=post.posted_at_utc)
        all_alerts.extend(alerts)

    return all_alerts, tracker


def format_scorecard_report(metrics: EvaluationMetrics) -> str:
    """Форматує текстовий звіт оцінки за критеріями Go/No-Go."""
    lines = [
        "============================================================",
        "          SHADOW MODE EVALUATION SCORECARD (PRD v1.1)        ",
        "============================================================",
        f"Усього епізодів/прольотів : {metrics.total_episodes}",
        f"Усього зафіксовано цілей  : {metrics.total_targets}",
        f"Сповіщень Рівень 2       : {metrics.level_2_alerts_count}",
        f"Сповіщень Рівень 3       : {metrics.level_3_alerts_count}",
        "------------------------------------------------------------",
        (
            f"Покриття вікном ETA (>=80%): {metrics.eta_coverage_percent:.1f}% "
            f"({metrics.eta_within_range_count}/{metrics.eta_evaluable_count})"
        ),
    ]

    med_official = (
        sorted(metrics.lead_time_over_official_minutes)[len(metrics.lead_time_over_official_minutes) // 2]
        if metrics.lead_time_over_official_minutes else None
    )
    med_manual = (
        sorted(metrics.lead_time_over_manual_minutes)[len(metrics.lead_time_over_manual_minutes) // 2]
        if metrics.lead_time_over_manual_minutes else None
    )

    lines.append(
        "Випередження оф. тривоги (>=7хв): "
        + (f"{med_official:.1f} хв (медіана)" if med_official is not None else "Немає даних")
    )
    lines.append(
        "Випередження ручних ОСІНТ (>=5хв): "
        + (f"{med_manual:.1f} хв (медіана)" if med_manual is not None else "Немає даних")
    )
    lines.append(f"Хибні тривоги Рівень 2 (<=25%): {metrics.false_level_2_rate_percent:.1f}%")
    lines.append(f"Хибні закриття активних цілей (==0%): {metrics.false_closures_count}")
    lines.append(f"Пропущені цілі (<=5%): {metrics.missed_targets_count}")
    lines.append("------------------------------------------------------------")
    lines.append("Критерії Go/No-Go:")
    all_passed = True
    for criterion, passed in metrics.go_no_go_checklist.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        if not passed:
            all_passed = False
        lines.append(f"  [{status}] {criterion}")
    lines.append("------------------------------------------------------------")
    lines.append(f"Підсумок: {'🚀 ГОТОВО ДО БЕТА-РЕЛІЗУ (GO)' if all_passed else '⏳ ТРЕБА БІЛЬШЕ ДАНИХ / КАЛІБРУВАННЯ (NO-GO)'}")
    lines.append("============================================================")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Запуск історичної симуляції та аналізу тіньового режиму.")
    parser.add_argument("--file", type=Path, help="Шлях до JSON-файлу експорту повідомлень")
    parser.add_argument("--channel", type=str, default="imported_channel", help="Назва каналу за замовчуванням")
    parser.add_argument("--ref-dir", type=str, default="data/reference", help="Шлях до теки довідників")
    parser.add_argument("--db", type=str, default="data/learning.db", help="Шлях до SQLite БД навчання")
    parser.add_argument("--evaluate", action="store_true", help="Сформувати оцінку Go/No-Go")
    parser.add_argument("--save", action="store_true", help="Зберегти прогнані повідомлення й цілі в БД навчання")

    args = parser.parse_args()

    repo = ReferenceRepository(args.ref_dir)
    if not repo.versions():
        print("Помилка: відсутні довідники в reference-dir. Виконайте: python -m src.learning.cli seed", file=sys.stderr)
        sys.exit(1)

    ref_cache = ActiveReferenceCache(repo)
    store = LearningStore(args.db) if args.save else None

    if args.file:
        if not args.file.exists():
            print(f"Файл {args.file} не знайдено.", file=sys.stderr)
            sys.exit(1)

        print(f"Завантаження повідомлень з {args.file}...")
        posts = parse_posts_from_json(args.file, default_channel=args.channel)
        print(f"Зчитано {len(posts)} повідомлень.")

        alerts, tracker = run_historical_replay(posts, ref_cache, store=store)
        print(f"Згенеровано {len(alerts)} сповіщень. Активних/виявлених цілей: {len(tracker._active_targets)}.")

        for alert in alerts:
            lvl_marker = "🔴" if alert.level == AlertLevel.LEVEL_2 else "🟠" if alert.level in (AlertLevel.LEVEL_3A, AlertLevel.LEVEL_3B) else "⚪️"
            print(f"  {lvl_marker} [{alert.level.value}] {alert.title} | ETA: {alert.eta_window_str or 'н/д'}")

        if args.evaluate:
            # Створюємо один узагальнений епізод симуляції
            episodes = [
                SimulationEpisode(
                    episode_id=args.file.stem,
                    description=f"Імпорт з {args.file.name}",
                    posts=posts,
                )
            ]
            simulator = ShadowModeSimulator(ref_cache)
            metrics, _ = simulator.run_simulation(episodes)
            print()
            print(format_scorecard_report(metrics))

    elif args.evaluate:
        # Звіт на основі вже збережених у базі цілей
        store = LearningStore(args.db)
        labeled = store.count_labeled()
        unlabeled = len(store.unlabeled_targets())
        print(f"Стан LearningStore ({args.db}):")
        print(f"  Розмічено цілей : {labeled}")
        print(f"  Нерозмічено     : {unlabeled}")
        if labeled < 20:
            print(f"  ⚠️ Для достовірної валідації потрібно щонайменше 20 розмічених цілей (зараз: {labeled}).")


if __name__ == "__main__":
    main()
