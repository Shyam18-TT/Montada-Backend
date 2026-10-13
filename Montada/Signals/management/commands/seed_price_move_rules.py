"""
Fill the price move notification tables with the built-in defaults.

Migration 0013 does this on `migrate`; run this command when the defaults changed after
the migration was applied (e.g. new excluded symbols), or to see what the tables hold.

Rules an admin created or edited are never changed. A seeded rule that is still untouched
is updated when the defaults now mute its symbol.

Usage:
  python manage.py seed_price_move_rules --dry-run   # show what would change
  python manage.py seed_price_move_rules
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from Signals.models import (
    AssetClass,
    Instrument,
    PriceMoveNotificationRule,
    PriceMoveNotificationSettings,
)
from Signals.price_move_config import bump_config_version, legacy_seed_rules


class Command(BaseCommand):
    help = "Create missing default price move notification settings/rules (safe to re-run)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show the changes without saving them.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        with transaction.atomic():
            settings_row, settings_created = PriceMoveNotificationSettings.objects.get_or_create(pk=1)
            existing_classes = {
                rule.asset_class_id: rule
                for rule in PriceMoveNotificationRule.objects.filter(scope="asset_class")
            }
            existing_symbols = {
                rule.symbol: rule
                for rule in PriceMoveNotificationRule.objects.filter(scope="symbol")
            }

            to_create = []
            to_mute = []
            for row in legacy_seed_rules(
                AssetClass.objects.values_list("id", "name"),
                Instrument.objects.values_list("symbol", "asset_class_id"),
            ):
                if row["scope"] == "asset_class":
                    if row["asset_class_id"] not in existing_classes:
                        to_create.append(PriceMoveNotificationRule(**row))
                    continue
                existing = existing_symbols.get(row["symbol"])
                if existing is None:
                    to_create.append(PriceMoveNotificationRule(**row))
                elif (
                    row["is_enabled"] is False
                    and existing.is_enabled is not False
                    and existing.updated_by_id is None
                    and (existing.note or "").startswith("Seeded:")
                ):
                    # Seeded as a threshold rule before the defaults muted it, never edited.
                    existing.is_enabled = False
                    existing.threshold_percent = None
                    existing.step_percent = None
                    existing.note = row["note"]
                    to_mute.append(existing)

            if dry_run:
                transaction.set_rollback(True)
            else:
                PriceMoveNotificationRule.objects.bulk_create(to_create)
                for rule in to_mute:
                    rule.save(update_fields=["is_enabled", "threshold_percent", "step_percent", "note", "updated_at"])
                # bulk_create sends no post_save signal, so tell running workers explicitly.
                bump_config_version()

        prefix = "[dry run] would " if dry_run else ""
        if settings_created:
            self.stdout.write("%screate the settings row with defaults." % prefix)
        for rule in to_create:
            target = rule.symbol or "asset class %s" % rule.asset_class_id
            state = "muted" if rule.is_enabled is False else "%s%%" % rule.threshold_percent
            self.stdout.write("%screate %s rule %s (%s)" % (prefix, rule.scope, target, state))
        for rule in to_mute:
            self.stdout.write("%smute seeded symbol rule %s" % (prefix, rule.symbol))

        self.stdout.write(
            self.style.SUCCESS(
                "%s%d rule(s) created, %d seeded rule(s) muted. Settings: enabled=%s, threshold=%s%%, "
                "step=%s, reset=%s%%, cooldown=%ss, interval=%ss, max batches=%s."
                % (
                    "[dry run] " if dry_run else "",
                    len(to_create),
                    len(to_mute),
                    settings_row.is_enabled,
                    settings_row.default_threshold_percent,
                    settings_row.default_step_percent or "same as threshold",
                    settings_row.reset_percent,
                    settings_row.cooldown_seconds,
                    settings_row.poll_interval_seconds,
                    settings_row.max_notification_batches,
                )
            )
        )
        total = PriceMoveNotificationRule.objects.count()
        muted = PriceMoveNotificationRule.objects.filter(is_enabled=False).count()
        self.stdout.write("Rules in table: %d (%d muted)." % (total, muted))
