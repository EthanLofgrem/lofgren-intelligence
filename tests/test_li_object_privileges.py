"""Q30: the forward migration that revokes default privileges on li_ objects.

The static checks here run everywhere; the behaviour (ACLs, denied nextval/setval/
SELECT as anon and authenticated, service_role unaffected) is proven on real
PostgreSQL by tests/pg/test_migrations_pg.py (PublicDefaultPrivilegeTest), which
runs in the postgres-proof CI job.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
NAME = "20261006090000_li_revoke_public_defaults.sql"


class RevokePublicDefaultsMigrationTests(unittest.TestCase):
    def sql(self) -> str:
        # The executable SQL only: the header comment explains the defaults it removes.
        text = (MIGRATIONS / NAME).read_text(encoding="utf-8").lower()
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))

    def test_it_is_a_forward_migration_after_every_earlier_one(self):
        names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
        self.assertIn(NAME, names)
        self.assertGreater(NAME, "20261006080000_li_research_job_finalize_cap.sql")
        self.assertEqual(len([n for n in names if n.endswith("_li_revoke_public_defaults.sql")]), 1)

    def test_it_sweeps_every_li_object_kind_from_public_anon_and_authenticated(self):
        sql = self.sql()
        self.assertIn("revoke all on sequence %i.%i from public, anon, authenticated", sql)
        self.assertIn("revoke all on table %i.%i from public, anon, authenticated", sql)
        self.assertIn("revoke all on function %i.%i(%s) from public, anon, authenticated", sql)
        self.assertIn("revoke usage on type %i.%i from public, anon, authenticated", sql)
        # Scoped to li_ objects in schema public, all relation kinds that carry privileges.
        self.assertEqual(sql.count("n.nspname = 'public'"), 3)
        self.assertEqual(sql.count("like 'li\\_%'"), 3)
        self.assertIn("c.relkind in ('s', 'r', 'p', 'v', 'm', 'f')", sql)

    def test_it_grants_nothing_to_api_roles_and_changes_no_default_privileges(self):
        sql = self.sql()
        grants = re.findall(r"'grant [^']*'", sql)
        self.assertEqual(grants, ["'grant usage on type %i.%i to service_role'"])
        self.assertNotRegex(sql, r"grant[^;']*\b(anon|authenticated|public)\b")
        self.assertNotIn("alter default privileges", sql)
        self.assertNotIn("create policy", sql)
        self.assertNotIn("security definer", sql)
        self.assertNotIn("create or replace function", sql)


if __name__ == "__main__":
    unittest.main()
