"""Command line for the daily learning work.

Examples:
    python -m src.learning.cli seed
    python -m src.learning.cli status
    python -m src.learning.cli import-episodes data/episodes.csv
    python -m src.learning.cli unlabeled
    python -m src.learning.cli label T-2026-10-10-01 reached_target --arrived-at 2026-10-10T19:55:00+00:00
    python -m src.learning.cli discover-aliases
    python -m src.learning.cli aliases
    python -m src.learning.cli approve-alias "скалат" --new-point skalat --level 2
    python -m src.learning.cli retrain
    python -m src.learning.cli diff 3
    python -m src.learning.cli promote 3
    python -m src.learning.cli rollback
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime

from .aliases import discover_aliases
from .pipeline import LearningConfig, diff, retrain
from .reference import ReferenceRepository, seed_from_profile
from .store import LearningStore, parse_utc
from .text import format_place_title

DEFAULT_REF_DIR = os.environ.get("REFERENCE_DIR", "data/reference")
DEFAULT_DB = os.environ.get("LEARNING_DB", "data/learning.db")


def _dt(value: str | None) -> datetime | None:
    return parse_utc(value) if value else None


def cmd_seed(args, repo, store):
    if repo.versions():
        print("Версії вже існують, seed пропущено.")
        return
    ref = repo.save_new(seed_from_profile())
    repo.promote(ref.version)
    print(f"Створено й активовано v{ref.version}.")


def cmd_status(args, repo, store):
    active = repo.active_version()
    print(f"Активна версія: v{active}" if active else "Активної версії немає")
    for v in repo.versions():
        ref = repo.load(v)
        print(f"  v{v:04d}  {ref.status:9}  {ref.created_at}  parent=v{ref.parent_version}  змін: {len(ref.changelog)}")
    print(f"Розмічених цілей: {store.count_labeled()}, нерозмічених: {len(store.unlabeled_targets())}")
    print(f"Кандидатів у назви (pending): {len(store.alias_candidates(status='pending'))}")


def cmd_import(args, repo, store):
    """CSV: target_id,point_id,channel,posted_at_utc,outcome,arrived_at_utc
    Put outcome and arrived_at_utc in one row of the target only."""
    labels: dict[str, tuple[str, str]] = {}
    with open(args.csv, encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=1):
            store.add_mention(row["target_id"], row["point_id"], row.get("channel") or "unknown",
                              parse_utc(row["posted_at_utc"]), message_id=i)
            if row.get("outcome"):
                labels[row["target_id"]] = (row["outcome"], row.get("arrived_at_utc") or "")
    for target_id, (outcome, arrived) in labels.items():
        store.label_target(target_id, outcome, _dt(arrived))
    print(f"Імпортовано. Розмічено цілей: {len(labels)}")


def cmd_unlabeled(args, repo, store):
    rows = store.unlabeled_targets()
    if not rows:
        print("Усі цілі розмічені.")
    for r in rows:
        print(f"{r['id']}  {r['first_seen_utc']}  пункти: {r['points']}")


def cmd_label(args, repo, store):
    store.label_target(args.target_id, args.outcome, _dt(args.arrived_at), args.notes)
    print("Збережено.")


def cmd_discover(args, repo, store):
    rep = discover_aliases(store, repo.active())
    print(f"Проскановано постів: {rep.posts_scanned}, знахідок: {rep.candidates_seen}")


def cmd_aliases(args, repo, store):
    for c in store.alias_candidates(status=args.status, min_count=args.min_count):
        hint = f"схоже на {c.suggested_point_id}" if c.suggested_point_id else "новий пункт?"
        print(f"[{c.count:3}] «{c.phrase_norm}» ({', '.join(c.surface_forms)}) — {hint}; {c.status}")
        if c.example:
            print(f"       приклад: {c.example[:160]}")


def cmd_approve_alias(args, repo, store):
    if bool(args.point) == bool(args.new_point):
        sys.exit("Вкажіть рівно одне: --point ID (відомий пункт) або --new-point ID --level N")
    if args.point and args.point not in repo.active().points:
        sys.exit(f"Пункту {args.point} немає в активній версії")
    if args.new_point and not args.level:
        sys.exit("Для нового пункту потрібен --level 1|2|3")
    store.decide_alias(args.phrase, "approved", args.point or args.new_point, args.level)
    print("Схвалено. Потрапить у словник під час наступного retrain.")


def cmd_reject_alias(args, repo, store):
    store.decide_alias(args.phrase, "rejected")
    print("Відхилено.")


def cmd_retrain(args, repo, store):
    report = retrain(store, repo, LearningConfig(), auto_promote=args.auto_promote)
    print(f"Результат: {report.status} — {report.reason}")
    if report.version:
        print(f"Версія: v{report.version}")
    for line in report.changelog:
        print(f"  • {line}")
    if report.gates:
        print("Перевірки:", json.dumps(report.gates, ensure_ascii=False))
        h = report.holdout
        print(f"Перевірка на {h['targets']} найсвіжіших цілях: покриття "
              f"{h['active']['coverage']} (активна) -> {h['candidate']['coverage']} (нова); "
              f"середня ширина {h['active']['mean_width_min']} -> {h['candidate']['mean_width_min']} хв")


def cmd_diff(args, repo, store):
    new = repo.load(args.version)
    base = repo.load(args.against) if args.against else repo.load(new.parent_version or repo.active_version())
    print(f"v{base.version} -> v{new.version}")
    for line in diff(base, new) or ["(без змін)"]:
        print(line)


def cmd_promote(args, repo, store):
    repo.promote(args.version)
    print(f"v{args.version} активна.")


def cmd_reject(args, repo, store):
    repo.reject(args.version)
    print(f"v{args.version} відхилена.")


def cmd_rollback(args, repo, store):
    print(f"Відкат до v{repo.rollback()}.")


def cmd_set_distance(args, repo, store):
    active = repo.active()
    if args.point not in active.points:
        sys.exit(f"Пункту {args.point} немає")
    cand = active.clone_as_candidate()
    cand.points[args.point].distance_km = args.km
    cand.changelog.append(f"Відстань {args.point}: {active.points[args.point].distance_km} -> {args.km} км (вручну)")
    cand = repo.save_new(cand)
    print(f"Створено v{cand.version} (pending). Перегляньте `diff {cand.version}` і `promote {cand.version}`.")


def cmd_purge(args, repo, store):
    print(f"Видалено постів: {store.purge_posts(args.days)}")


def cmd_show(args, repo, store):
    ver = args.version or repo.active_version()
    if not ver:
        sys.exit("Немає доступних версій довідника")
    ref = repo.load(ver)
    status_label = "(активна)" if ver == repo.active_version() else f"({ref.status})"
    print(f"\n=== Довідник точок коридору та ETA v{ver} {status_label} ===")
    print(f"Швидкість БпЛА: {ref.speed_kmh[0]:.0f}–{ref.speed_kmh[1]:.0f} км/год | Лаг постів: {ref.post_lag_min[0]:.0f}–{ref.post_lag_min[1]:.0f} хв\n")
    print("-" * 90)
    print(f"{'Пункт коридору':<22} | {'Рівень':<8} | {'ETA (хв)':<12} | {'Метод':<22} | {'Випадків':<9} | {'P(ІФ)':<6}")
    print("-" * 90)
    sorted_points = sorted(ref.points.values(), key=lambda p: (p.level, -(p.eta_min or 0)))
    for p in sorted_points:
        name = format_place_title(p.names[0]) if p.names else p.id
        eta_str = f"{p.eta_min:.0f}–{p.eta_max:.0f}" if p.eta_min is not None and p.eta_max is not None else "—"
        p_if_str = f"{p.base_rate:.1%}" if p.base_rate is not None else "—"
        lvl_marker = "⚪️ Рівень 1" if p.level == 1 else "🔴 Рівень 2" if p.level == 2 else "🟠 Рівень 3"
        print(f"{name:<22} | {lvl_marker:<8} | {eta_str:<12} | {p.eta_method:<22} | {p.n_obs:<9} | {p_if_str:<6}")
    print("-" * 90 + "\n")



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="learning", description="Навчання довідників")
    p.add_argument("--ref-dir", default=DEFAULT_REF_DIR)
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("status").set_defaults(func=cmd_status)
    s = sub.add_parser("import-episodes"); s.add_argument("csv"); s.set_defaults(func=cmd_import)
    sub.add_parser("unlabeled").set_defaults(func=cmd_unlabeled)
    s = sub.add_parser("label")
    s.add_argument("target_id")
    s.add_argument("outcome", choices=["reached_target", "downed_en_route", "diverted", "unknown"])
    s.add_argument("--arrived-at")
    s.add_argument("--notes")
    s.set_defaults(func=cmd_label)
    sub.add_parser("discover-aliases").set_defaults(func=cmd_discover)
    s = sub.add_parser("aliases")
    s.add_argument("--status", default="pending")
    s.add_argument("--min-count", type=int, default=1)
    s.set_defaults(func=cmd_aliases)
    s = sub.add_parser("approve-alias")
    s.add_argument("phrase")
    s.add_argument("--point")
    s.add_argument("--new-point")
    s.add_argument("--level", type=int, choices=[1, 2, 3])
    s.set_defaults(func=cmd_approve_alias)
    s = sub.add_parser("reject-alias"); s.add_argument("phrase"); s.set_defaults(func=cmd_reject_alias)
    s = sub.add_parser("retrain")
    s.add_argument("--auto-promote", action="store_true",
                   help="Активувати нову версію одразу, якщо пройдено всі перевірки")
    s.set_defaults(func=cmd_retrain)
    s = sub.add_parser("diff"); s.add_argument("version", type=int); s.add_argument("--against", type=int)
    s.set_defaults(func=cmd_diff)
    s = sub.add_parser("promote"); s.add_argument("version", type=int); s.set_defaults(func=cmd_promote)
    s = sub.add_parser("reject"); s.add_argument("version", type=int); s.set_defaults(func=cmd_reject)
    sub.add_parser("rollback").set_defaults(func=cmd_rollback)
    s = sub.add_parser("set-distance"); s.add_argument("point"); s.add_argument("km", type=float)
    s.set_defaults(func=cmd_set_distance)
    s = sub.add_parser("purge-posts"); s.add_argument("--days", type=int, default=14)
    s.set_defaults(func=cmd_purge)
    s = sub.add_parser("show", help="Вивести таблицю точок коридору та ETA")
    s.add_argument("version", type=int, nargs="?", help="Номер версії довідника (за замовчуванням активна)")
    s.set_defaults(func=cmd_show)
    return p



def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    repo = ReferenceRepository(args.ref_dir)
    store = LearningStore(args.db)
    try:
        args.func(args, repo, store)
    except (LookupError, ValueError) as exc:
        sys.exit(f"Помилка: {exc}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
